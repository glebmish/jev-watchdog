"""Replay transcript files through the judges as if they were happening live.

A case is `<name>.jsonl` (a Claude Code transcript) plus an optional `<name>.expect.toml`.
The transcript is cut into steps — one after every tool result, as PostToolUse would fire,
plus one at the end, as Stop would — and every prefix is judged in order through the normal
registry, so the statistics, streaks and output are the ones a live session would produce.
Expectations turn a case into a test of the judge: which questions must be flagged or clear
at which step, and whether the judge's verdicts have quarantined the thread by that step.
A top-level `context = "..."` is what the human would have told the watchdog about the case.
"""

import json
import tempfile
import tomllib
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from rich.table import Table
from rich.text import Text

from jev_watchdog.core.pack import Question
from jev_watchdog.core.surfaces import SurfaceRegistry
from jev_watchdog.core.transcript import conversation_lines, read_lines
from jev_watchdog.display.printer import Printer


class ReplayError(ValueError):
    pass


@dataclass(frozen=True)
class Step:
    end: int  # the step judges lines[:end]
    event: str  # PostToolUse | Stop
    tool_name: str | None = None
    tool_input: dict | None = None
    tool_use_id: str | None = None  # the decider folds an action once


@dataclass(frozen=True)
class Expectation:
    step: int  # 1-based
    flagged: tuple[str, ...]
    clear: tuple[str, ...]
    quarantined: bool | None = None  # has the decider tripped by this step


@dataclass(frozen=True)
class Case:
    name: str
    description: str
    lines: list[str]
    steps: list[Step]
    expectations: list[Expectation]
    context: str | None = None  # overrides --context for this case


@dataclass(frozen=True)
class Finding:
    case: str
    judge: str
    step: int
    question: str
    kind: str  # ok | false_negative | false_positive | no_verdict


def steps_of(lines: list[str]) -> list[Step]:
    steps: list[Step] = []
    uses: dict[str | None, tuple[str | None, dict | None]] = {}  # parallel calls interleave
    for index, line in enumerate(lines, start=1):
        for block in _blocks(line):
            if block.get("type") == "tool_use":
                uses[block.get("id")] = (block.get("name"), block.get("input"))
            elif block.get("type") == "tool_result":
                tool_use_id = block.get("tool_use_id")
                tool_name, tool_input = uses.get(tool_use_id, (None, None))
                if steps and steps[-1].end == index:
                    steps.pop()  # results sharing a line share a prefix: one step, the last call
                steps.append(Step(index, "PostToolUse", tool_name, tool_input, tool_use_id))
    if lines and (not steps or steps[-1].end != len(lines)):
        steps.append(Step(len(lines), "Stop"))
    return steps


def _blocks(line: str) -> list[dict]:
    content = (json.loads(line).get("message") or {}).get("content")
    return (
        [block for block in content if isinstance(block, dict)] if isinstance(content, list) else []
    )


def load_case(path: Path, questions: list[Question]) -> Case:
    lines = conversation_lines(read_lines(path))
    steps = steps_of(lines)
    if not steps:
        raise ReplayError(f"{path}: no conversation lines to judge")
    expect_path = path.with_suffix(".expect.toml")
    data = tomllib.loads(expect_path.read_text(encoding="utf-8")) if expect_path.is_file() else {}
    known = {question.id for question in questions}
    expectations = [_expectation(path, raw, len(steps), known) for raw in data.get("expect", [])]
    context = data.get("context")
    if context is not None and not (isinstance(context, str) and context.strip()):
        raise ReplayError(f"{path}: context must be non-empty text, got {context!r}")
    return Case(
        path.stem,
        data.get("description", ""),
        lines,
        steps,
        expectations,
        context.strip() if context else None,
    )


def _expectation(path: Path, raw: dict, n_steps: int, known: set[str]) -> Expectation:
    step = raw.get("step")
    if not isinstance(step, int) or step == 0 or not -n_steps <= step <= n_steps:
        raise ReplayError(f"{path}: step must be 1..{n_steps} or -1..-{n_steps}, got {step!r}")
    flagged, clear = tuple(raw.get("flagged", ())), tuple(raw.get("clear", ()))
    if unknown := (set(flagged) | set(clear)) - known:
        raise ReplayError(f"{path}: unknown question ids {sorted(unknown)}")
    if both := set(flagged) & set(clear):
        raise ReplayError(f"{path}: {sorted(both)} listed as both flagged and clear")
    quarantined = raw.get("quarantined")
    if quarantined is not None and not isinstance(quarantined, bool):
        raise ReplayError(f"{path}: quarantined must be true or false, got {quarantined!r}")
    return Expectation(step if step > 0 else n_steps + step + 1, flagged, clear, quarantined)


QUARANTINE = "quarantine"  # reported next to the question ids


def check(
    case: Case,
    judge: str,
    flagged_by_step: dict[int, set[str]],
    tripped_by_step: dict[int, bool] | None = None,
) -> list[Finding]:
    findings = []
    for expectation in case.expectations:
        if expectation.quarantined is not None:
            tripped = (tripped_by_step or {}).get(expectation.step)
            if tripped is None:
                kind = "no_verdict"
            elif tripped == expectation.quarantined:
                kind = "ok"
            else:
                kind = "false_negative" if expectation.quarantined else "false_positive"
            findings.append(Finding(case.name, judge, expectation.step, QUARANTINE, kind))
        actual = flagged_by_step.get(expectation.step)
        for question in (*expectation.flagged, *expectation.clear):
            if actual is None:
                kind = "no_verdict"
            elif question in expectation.flagged:
                kind = "ok" if question in actual else "false_negative"
            else:
                kind = "false_positive" if question in actual else "ok"
            findings.append(Finding(case.name, judge, expectation.step, question, kind))
    return findings


async def run_cases(
    cases: list[Case], registry: SurfaceRegistry, printer: Printer
) -> list[Finding]:
    flagged: dict[tuple[str, str, int], set[str]] = {}
    tripped_at: dict[tuple[str, str, int], bool] = {}

    def record(surface, judge, job, verdict, flagged_ids, tripped) -> None:
        at = (job.event["session_id"], judge, job.event["replay_step"])
        flagged[at], tripped_at[at] = flagged_ids, tripped

    registry.on_verdict = record
    with tempfile.TemporaryDirectory(prefix="jev-watchdog-replay-") as tmp:
        for case in cases:
            transcript = Path(tmp) / f"{case.name}.jsonl"
            if case.context is None:
                registry.contexts.pop(case.name, None)
            else:
                registry.contexts[case.name] = case.context
            for number, step in enumerate(case.steps, start=1):
                # handle() returns with the file snapshotted, so the next prefix can
                # overwrite it while this one is still queued.
                transcript.write_text("\n".join(case.lines[: step.end]) + "\n", encoding="utf-8")
                await registry.handle(
                    {
                        "session_id": case.name,
                        "surface_label": f"{case.name}/main",
                        "transcript_path": str(transcript),
                        "hook_event_name": step.event,
                        "tool_name": step.tool_name,
                        "tool_input": step.tool_input,
                        "tool_use_id": step.tool_use_id,
                        "replay_step": number,
                    }
                )
        await registry.drain()

    findings = [
        finding
        for case in cases
        for judge in registry.judges
        for finding in check(
            case,
            judge.name,
            _steps_of(flagged, case.name, judge.name),
            _steps_of(tripped_at, case.name, judge.name),
        )
    ]
    _report(printer, findings)
    return findings


def _steps_of(by_key: dict[tuple[str, str, int], object], case: str, judge: str) -> dict:
    return {
        step: value for (name, who, step), value in by_key.items() if (name, who) == (case, judge)
    }


def _report(printer: Printer, findings: list[Finding]) -> None:
    wrong = [finding for finding in findings if finding.kind != "ok"]
    if wrong:
        table = Table(title=Text("expectations not met"), title_justify="left")
        for column in ("case", "judge", "step", "question", "result"):
            table.add_column(column)
        for finding in wrong:
            table.add_row(
                finding.case, finding.judge, str(finding.step), finding.question,
                finding.kind.replace("_", " "),
            )  # fmt: skip
        printer.console.print(table)
    printer.console.print(Text(f"expectations: {_tally(findings)}", style="bold"), soft_wrap=True)
    judges = list(dict.fromkeys(finding.judge for finding in findings))
    if len(judges) > 1:
        for judge in judges:
            mine = [finding for finding in findings if finding.judge == judge]
            printer.console.print(Text(f"  {judge}: {_tally(mine)}"), soft_wrap=True)


def _tally(findings: list[Finding]) -> str:
    kinds = Counter(finding.kind for finding in findings)
    return (
        f"{kinds['ok']} ok · {kinds['false_negative']} false negative"
        f" · {kinds['false_positive']} false positive · {kinds['no_verdict']} no verdict"
    )
