"""Replay transcript files through the judges as if they were happening live.

A case is `<name>.jsonl` (a Claude Code transcript) plus an optional `<name>.expect.toml`.
The transcript is cut into steps — one after every tool result, as PostToolUse would fire,
plus one at the end, as Stop would — and every prefix is judged in order through the normal
registry, so the statistics, streaks and output are the ones a live session would produce.
Expectations turn a case into a test of the judge: which questions must be flagged or clear
at which step.
"""

import json
import tempfile
import tomllib
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from rich.table import Table
from rich.text import Text

from jev_watchdog.pack import Question
from jev_watchdog.printer import Printer
from jev_watchdog.surfaces import SurfaceRegistry
from jev_watchdog.transcript import conversation_lines, read_lines


class ReplayError(ValueError):
    pass


@dataclass(frozen=True)
class Step:
    end: int  # the step judges lines[:end]
    event: str  # PostToolUse | Stop
    tool_name: str | None = None
    tool_input: dict | None = None


@dataclass(frozen=True)
class Expectation:
    step: int  # 1-based
    flagged: tuple[str, ...]
    clear: tuple[str, ...]


@dataclass(frozen=True)
class Case:
    name: str
    description: str
    lines: list[str]
    steps: list[Step]
    expectations: list[Expectation]


@dataclass(frozen=True)
class Finding:
    case: str
    judge: str
    step: int
    question: str
    kind: str  # ok | false_negative | false_positive | no_verdict


def steps_of(lines: list[str]) -> list[Step]:
    steps: list[Step] = []
    tool_name, tool_input = None, None
    for index, line in enumerate(lines, start=1):
        for block in _blocks(line):
            if block.get("type") == "tool_use":
                tool_name, tool_input = block.get("name"), block.get("input")
            elif block.get("type") == "tool_result":
                steps.append(Step(index, "PostToolUse", tool_name, tool_input))
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
    return Case(path.stem, data.get("description", ""), lines, steps, expectations)


def _expectation(path: Path, raw: dict, n_steps: int, known: set[str]) -> Expectation:
    step = raw.get("step")
    if not isinstance(step, int) or step == 0 or not -n_steps <= step <= n_steps:
        raise ReplayError(f"{path}: step must be 1..{n_steps} or -1..-{n_steps}, got {step!r}")
    flagged, clear = tuple(raw.get("flagged", ())), tuple(raw.get("clear", ()))
    if unknown := (set(flagged) | set(clear)) - known:
        raise ReplayError(f"{path}: unknown question ids {sorted(unknown)}")
    if both := set(flagged) & set(clear):
        raise ReplayError(f"{path}: {sorted(both)} listed as both flagged and clear")
    return Expectation(step if step > 0 else n_steps + step + 1, flagged, clear)


def check(case: Case, judge: str, flagged_by_step: dict[int, set[str]]) -> list[Finding]:
    findings = []
    for expectation in case.expectations:
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

    def record(surface, judge, job, verdict, flagged_ids) -> None:
        flagged[(job.event["session_id"], judge, job.event["replay_step"])] = flagged_ids

    registry.on_verdict = record
    with tempfile.TemporaryDirectory(prefix="jev-watchdog-replay-") as tmp:
        for case in cases:
            transcript = Path(tmp) / f"{case.name}.jsonl"
            for number, step in enumerate(case.steps, start=1):
                # handle() snapshots the file synchronously, so the next prefix can
                # overwrite it while this one is still queued.
                transcript.write_text("\n".join(case.lines[: step.end]) + "\n", encoding="utf-8")
                registry.handle(
                    {
                        "session_id": case.name,
                        "surface_label": f"{case.name}/main",
                        "transcript_path": str(transcript),
                        "hook_event_name": step.event,
                        "tool_name": step.tool_name,
                        "tool_input": step.tool_input,
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
            {
                step: ids
                for (name, who, step), ids in flagged.items()
                if (name, who) == (case.name, judge.name)
            },
        )
    ]
    _report(printer, findings)
    return findings


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
    kinds = Counter(finding.kind for finding in findings)
    printer.console.print(
        Text(
            f"expectations: {kinds['ok']} ok · {kinds['false_negative']} false negative"
            f" · {kinds['false_positive']} false positive · {kinds['no_verdict']} no verdict",
            style="bold",
        ),
        soft_wrap=True,
    )
