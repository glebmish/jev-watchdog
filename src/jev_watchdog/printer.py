"""Foreground output: one console line per event/verdict/error, plus a JSONL run log."""

import json
from collections import Counter
from collections.abc import Callable
from dataclasses import asdict
from datetime import datetime
from typing import TextIO

from rich.console import Console
from rich.table import Table
from rich.text import Text

from jev_watchdog.judge.base import Answer, Verdict
from jev_watchdog.stats import ChoiceStat, GlobalStats, JudgeSurfaceStats, SurfaceStats

LABEL_WIDTH = 26
PROMPT_PREVIEW = 60


class Printer:
    def __init__(
        self,
        console: Console,
        log_file: TextIO | None = None,
        clock: Callable[[], datetime] = datetime.now,
    ) -> None:
        self.console = console
        self.log_file = log_file
        self.clock = clock

    def banner(self, text: str) -> None:
        self.console.print(Text(text, style="bold"), soft_wrap=True)

    def event(self, label: str, payload: dict) -> None:
        name = payload.get("hook_event_name", "?")
        line = self._prefix(label)
        line.append(f"{name:<18} ", style="bold")
        line.append(_event_detail(name, payload))
        self.console.print(line, soft_wrap=True)
        self._log("event", label, payload=payload)

    def verdict(self, label: str, judge: str, verdict: Verdict, flagged: set[str]) -> None:
        line = self._prefix(label)
        line.append(f"{judge} ", style="green")
        line.append(f"{verdict.latency_ms:.0f}ms {_tokens(verdict.input_tokens)}  ", style="dim")
        for qid, answer in verdict.answers.items():
            if qid in flagged:
                line.append(f"{qid}={_value(answer)}!", style="bold red")
            else:
                line.append(f"{qid}={_value(answer)}")
            line.append(" ")
        self.console.print(line, soft_wrap=True)
        self._log("verdict", label, judge=judge, flagged=sorted(flagged), verdict=asdict(verdict))

    def error(self, label: str, kind: str, message: str, judge: str | None = None) -> None:
        line = self._prefix(label)
        who = f"{judge} " if judge else ""
        line.append(f"{who}error {kind}: {message}", style="yellow")
        self.console.print(line, soft_wrap=True)
        self._log("error", label, judge=judge, error_kind=kind, message=message)

    def note(self, label: str, message: str) -> None:
        line = self._prefix(label)
        line.append(message, style="dim")
        self.console.print(line, soft_wrap=True)
        self._log("note", label, message=message)

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
                f" · ${stats.cost_usd:.4f}",
                style="bold",
            ),
            soft_wrap=True,
        )

    def _prefix(self, label: str) -> Text:
        line = Text()
        line.append(f"{self.clock():%H:%M:%S} ", style="dim")
        line.append(f"{label:<{LABEL_WIDTH}} ", style="cyan")
        return line

    def _log(self, kind: str, label: str, **data) -> None:
        if self.log_file is None:
            return
        record = {"ts": self.clock().isoformat(timespec="seconds"), "kind": kind, "surface": label}
        self.log_file.write(json.dumps(record | data, ensure_ascii=False) + "\n")
        self.log_file.flush()


def _questions_table(label: str, judge: str, stats: JudgeSurfaceStats) -> Table:
    title = f"{label} · {judge} · judgments {stats.judgments} · errors {_counter(stats.errors)}"
    table = Table(title=Text(title), title_justify="left")
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
    return str(payload.get("tool_name", ""))


def _value(answer: Answer) -> str:
    return answer.value if isinstance(answer.value, str) else f"{answer.value:.2f}"


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
