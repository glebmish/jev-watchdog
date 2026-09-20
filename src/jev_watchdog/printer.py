"""Output: one console line per event/verdict/error, a JSONL run log, and the feed of an
attached dashboard. The line and the feed come from one display record; the log is fuller."""

import json
import re
from collections import Counter
from collections.abc import Callable
from dataclasses import asdict
from datetime import datetime
from typing import TextIO

from rich.console import Console
from rich.table import Table
from rich.text import Text

from jev_watchdog.feed import Feed
from jev_watchdog.judge.base import Verdict
from jev_watchdog.stats import ChoiceStat, GlobalStats, JudgeSurfaceStats, SurfaceStats

LABEL_WIDTH = 26
PROMPT_PREVIEW = 60
MESSAGE_LIMIT = 500  # of any text in a record handed to the feed
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def printable(text: str) -> str:
    """Text that is safe to put on a terminal: C0 and C1 control characters made visible.

    Labels, tool inputs, contexts and judge errors are the watched agent's to choose. rich
    drops BEL, BS, VT, FF and CR but lets ESC through, and with ESC an agent can erase the
    QUARANTINED line, wipe the scrollback or write the clipboard (OSC 52). These are all
    one-line fields, so newlines get no exception.
    """
    return _CONTROL.sub("\N{REPLACEMENT CHARACTER}", text)


class Printer:
    def __init__(
        self,
        console: Console,
        log_file: TextIO | None = None,
        clock: Callable[[], datetime] = datetime.now,
        feed: Feed | None = None,
    ) -> None:
        self.console = console
        self.log_file = log_file
        self.clock = clock
        self.feed = feed

    def banner(self, text: str) -> None:
        self.console.print(Text(text, style="bold"), soft_wrap=True)

    def event(self, label: str, payload: dict) -> None:
        name = payload.get("hook_event_name", "?")
        self._emit("event", label, {"event": name, "detail": _event_detail(name, payload)})
        self._log("event", label, payload=payload)

    def verdict(
        self,
        label: str,
        judge: str,
        verdict: Verdict,
        flagged: set[str],
        step: int | None = None,
        context: str | None = None,
    ) -> None:
        shown = {
            "judge": judge,
            "latency_ms": verdict.latency_ms,
            "input_tokens": verdict.input_tokens,
            "answers": {qid: answer.value for qid, answer in verdict.answers.items()},
            "flagged": sorted(flagged),
        }
        self._emit("verdict", label, shown)
        self._log(
            "verdict",
            label,
            judge=judge,
            step=step,  # replay step number; None for live events
            flagged=sorted(flagged),
            context=context,  # what the judge was told next to the transcript, if anything
            verdict=asdict(verdict),
        )

    def error(self, label: str, kind: str, message: str, judge: str | None = None) -> None:
        self._emit("error", label, {"judge": judge, "error_kind": kind, "message": message})
        self._log("error", label, judge=judge, error_kind=kind, message=message)

    def note(self, label: str, message: str) -> None:
        self._emit("note", label, {"message": message})
        self._log("note", label, message=message)

    def quarantine(self, label: str, source: str, reason: str, enforced: bool) -> None:
        self._emit("quarantine", label, {"source": source, "reason": reason, "enforced": enforced})
        self._log("quarantine", label, source=source, reason=reason, enforced=enforced)

    def rejected(self, label: str, payload: dict, reason: str) -> None:
        detail = _event_detail("PreToolUse", payload)
        self._emit("rejected", label, {"detail": detail, "reason": reason})
        self._log("rejected", label, payload=payload, reason=reason)

    def released(self, label: str) -> None:
        self._emit("released", label, {})
        self._log("released", label)

    def surface_summary(self, label: str, stats: SurfaceStats, judge: str | None = None) -> None:
        """One table per judge for this surface, or only `judge`'s table."""
        for name, judge_stats in stats.judges.items():
            if judge is None or name == judge:
                self.console.print(_questions_table(label, name, judge_stats))

    def global_summary(self, stats: GlobalStats, surfaces: dict[str, SurfaceStats]) -> None:
        for label, surface_stats in surfaces.items():
            self.surface_summary(label, surface_stats)
        if stats.judges:
            self.console.print(_judges_table(stats))
        self.console.print(
            Text(
                f"surfaces {stats.surfaces} · events {sum(stats.events.values())}"
                f" · judgments {stats.judgments} · errors {_counter(stats.errors)}"
                f" · quarantines {stats.quarantines} · rejected {stats.rejected}"
                f" · ${stats.cost_usd:.4f}",
                style="bold",
            ),
            soft_wrap=True,
        )

    def _emit(self, kind: str, label: str, shown: dict) -> None:
        """Show one display record on the console and hand it to whoever is attached."""
        record = display_record(kind, label, self.clock(), **shown)
        self.console.print(render_line(record), soft_wrap=True)
        if self.feed is not None:
            self.feed.publish(_cut(record))

    def _log(self, kind: str, label: str, **data) -> None:
        if self.log_file is None:
            return
        record = {"ts": self.clock().isoformat(timespec="seconds"), "kind": kind, "surface": label}
        try:
            self.log_file.write(json.dumps(record | data, ensure_ascii=False) + "\n")
            self.log_file.flush()
        except OSError as exc:
            # Printer calls sit on the hook path and in the judge workers: a full disk must
            # not turn a deny into a handler error. Say it once and watch on without a log.
            self.log_file = None
            self.console.print(Text(f"run log failed, no longer written: {exc}", style="bold red"))


def display_record(kind: str, label: str, ts: datetime, **shown) -> dict:
    """What one console line shows, as data. The run log keeps the payloads; this does not."""
    return {"ts": ts.isoformat(timespec="seconds"), "kind": kind, "surface": label} | shown


def render_line(record: dict) -> Text:
    """The console line of a display record. The dashboard draws its feed with it too."""
    line = Text()
    line.append(f"{record['ts'][11:19]} ", style="dim")
    line.append(f"{record['surface']:<{LABEL_WIDTH}} ", style="cyan")
    kind = record["kind"]
    if kind == "event":
        line.append(f"{record['event']:<18} ", style="bold")
        line.append(record["detail"])
    elif kind == "verdict":
        line.append(f"{record['judge']} ", style="green")
        tokens = _tokens(record["input_tokens"])
        line.append(f"{record['latency_ms']:.0f}ms {tokens}  ", style="dim")
        for qid, value in record["answers"].items():
            if qid in record["flagged"]:
                line.append(f"{qid}={_value(value)}!", style="bold red")
            else:
                line.append(f"{qid}={_value(value)}")
            line.append(" ")
    elif kind == "error":
        who = f"{record['judge']} " if record["judge"] else ""
        line.append(f"{who}error {record['error_kind']}: {record['message']}", style="yellow")
    elif kind == "note":
        line.append(record["message"], style="dim")
    elif kind == "quarantine" and record["enforced"]:
        line.append(
            f"QUARANTINED by {record['source']}: {record['reason']}", style="bold white on red"
        )
    elif kind == "quarantine":
        line.append(f"{record['source']} would quarantine: {record['reason']}", style="bold red")
    elif kind == "rejected":
        line.append(f"{'rejected':<18} ", style="bold red")
        line.append(record["detail"])
    elif kind == "released":
        line.append("released from quarantine", style="bold green")
    line.plain = printable(line.plain)  # same length, so the styles stay where they are
    return line


def _cut(record: dict) -> dict:
    """A record for the feed: it is kept in memory and streamed, so no text runs on."""
    return {
        key: value[:MESSAGE_LIMIT] if isinstance(value, str) else value
        for key, value in record.items()
    }


def _questions_table(label: str, judge: str, stats: JudgeSurfaceStats) -> Table:
    title = f"{label} · {judge} · judgments {stats.judgments} · errors {_counter(stats.errors)}"
    table = Table(title=Text(printable(title)), title_justify="left")
    for column in ("question", "n", "last", "mean", "ewma", "min", "max", "streak", "longest"):
        table.add_column(column)
    for qid, stat in stats.questions.items():
        if isinstance(stat, ChoiceStat):
            table.add_row(
                qid, str(sum(stat.counts.values())), str(stat.last), _counter(stat.counts),
                "", "", "", str(stat.streak), str(stat.longest_streak),
            )  # fmt: skip
        else:
            table.add_row(
                qid, str(stat.n), _num(stat.last), _num(stat.mean), _num(stat.ewma),
                _num(stat.min), _num(stat.max), str(stat.streak), str(stat.longest_streak),
            )  # fmt: skip
    return table


def _judges_table(stats: GlobalStats) -> Table:
    table = Table(title=Text("judges"), title_justify="left")
    columns = ("judge", "judgments", "errors", "latency p50", "latency p95", "latency mean",
               "lag p50", "lag p95", "tokens", "cost")  # fmt: skip
    for column in columns:
        table.add_column(column)
    for name, judge in stats.judges.items():
        table.add_row(
            name, str(judge.judgments), _counter(judge.errors),
            _ms(judge.latency(50)), _ms(judge.latency(95)), _ms(judge.mean_latency),
            _ms(judge.lag(50)), _ms(judge.lag(95)),
            _tokens(judge.input_tokens), f"${judge.cost_usd:.4f}",
        )  # fmt: skip
    return table


def _event_detail(name: str, payload: dict) -> str:
    if name == "UserPromptSubmit":
        prompt = " ".join(str(payload.get("prompt", "")).split())
        return prompt if len(prompt) <= PROMPT_PREVIEW else prompt[:PROMPT_PREVIEW] + "…"
    if name in ("SubagentStart", "SubagentStop"):
        return str(payload.get("agent_type", ""))
    if name == "SessionStart":
        return str(payload.get("source", ""))
    if name == "SessionEnd":
        return str(payload.get("reason", ""))
    return f"{payload.get('tool_name') or ''} {_tool_preview(payload.get('tool_input'))}".strip()


def _tool_preview(tool_input: object) -> str:
    if not isinstance(tool_input, dict):
        return ""
    for key in ("command", "file_path", "pattern", "url", "description"):
        if tool_input.get(key):
            text = " ".join(str(tool_input[key]).split())
            return text if len(text) <= PROMPT_PREVIEW else text[:PROMPT_PREVIEW] + "…"
    return ""


def _value(value: float | str) -> str:
    return value if isinstance(value, str) else f"{value:.2f}"


def _num(value: float | None, digits: int = 2) -> str:
    return "-" if value is None else f"{value:.{digits}f}"


def _ms(value: float | None) -> str:
    return "-" if value is None else f"{value:.0f}ms"


def _tokens(tokens: int | None) -> str:
    if tokens is None:
        return "? tok"
    return f"{tokens / 1000:.1f}k tok" if tokens >= 1000 else f"{tokens} tok"


def _counter(counter: Counter[str]) -> str:
    return " ".join(f"{key}×{count}" for key, count in counter.most_common()) or "0"
