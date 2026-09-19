import io
import json
from datetime import datetime

from rich.console import Console

from jev_watchdog.judge.base import Answer, Verdict
from jev_watchdog.pack import Question
from jev_watchdog.printer import Printer
from jev_watchdog.stats import GlobalStats, SurfaceStats


def make_printer():
    out, log = io.StringIO(), io.StringIO()
    console = Console(file=out, width=200, color_system=None, force_terminal=False)
    printer = Printer(console, log, clock=lambda: datetime(2026, 9, 19, 15, 2, 11))
    return printer, out, log


def records(log: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in log.getvalue().splitlines()]


def test_event_line_and_log_record():
    printer, out, log = make_printer()
    payload = {"hook_event_name": "PostToolUse", "tool_name": "Bash", "session_id": "s"}
    printer.event("012345/main", payload)
    line = out.getvalue()
    assert "15:02:11" in line and "012345/main" in line
    assert "PostToolUse" in line and "Bash" in line
    assert records(log) == [
        {"ts": "2026-09-19T15:02:11", "kind": "event", "surface": "012345/main", "payload": payload}
    ]


def test_event_detail_per_event_type():
    printer, out, _ = make_printer()
    printer.event("x", {"hook_event_name": "UserPromptSubmit", "prompt": "fix the [failing] test " * 10})
    printer.event("x", {"hook_event_name": "SubagentStart", "agent_type": "Explore"})
    printer.event("x", {"hook_event_name": "SessionEnd", "reason": "clear"})
    text = out.getvalue()
    assert "fix the [failing] test" in text and "…" in text
    assert "Explore" in text and "clear" in text


def test_verdict_line_marks_flagged_answers():
    printer, out, log = make_printer()
    verdict = Verdict(
        {"exfil": Answer(0.95), "goal_drift": Answer(2.88, 0.9), "activity": Answer("off_task", 0.7)},
        latency_ms=612.4,
        input_tokens=4100,
        judge="jev-1.13.0",
    )
    printer.verdict("012345/main", verdict, flagged={"exfil", "activity"})
    line = out.getvalue()
    assert "verdict" in line and "612ms" in line and "4.1k tok" in line
    assert "exfil=0.95!" in line and "activity=off_task!" in line
    assert "goal_drift=2.88" in line and "goal_drift=2.88!" not in line
    record = records(log)[0]
    assert record["kind"] == "verdict" and record["flagged"] == ["activity", "exfil"]
    assert record["verdict"]["answers"]["exfil"]["value"] == 0.95


def test_error_line():
    printer, out, log = make_printer()
    printer.error("012345/main", "over_limit", "state exceeds [32k] tokens")
    assert "judge error over_limit: state exceeds [32k] tokens" in out.getvalue()
    assert records(log)[0] == {
        "ts": "2026-09-19T15:02:11",
        "kind": "error",
        "surface": "012345/main",
        "error_kind": "over_limit",
        "message": "state exceeds [32k] tokens",
    }


def test_summaries_render():
    printer, out, _ = make_printer()
    questions = [
        Question("exfil", "noul", "i", flag_threshold=0.7),
        Question("activity", "choice", "i", criteria={"ok": "", "stuck": ""}, flag_choices=("stuck",)),
    ]
    surface = SurfaceStats()
    surface.record_event("PostToolUse")
    surface.record_verdict(questions, Verdict({"exfil": Answer(0.9), "activity": Answer("stuck")}, 100, 10, "fake"))
    surface.record_error("timeout")
    stats = GlobalStats(surfaces=1)
    stats.record_event("PostToolUse")
    stats.record_verdict(Verdict({}, 100, 1_000_000, "fake"))
    stats.record_error("timeout")

    printer.surface_summary("012345/main", surface)
    printer.global_summary(stats, {"012345/main": surface})
    text = out.getvalue()
    assert "012345/main" in text and "exfil" in text and "stuck×1" in text
    assert "timeout×1" in text and "$0.0420" in text and "p50" in text


def test_works_without_a_log_file():
    out = io.StringIO()
    Printer(Console(file=out, width=120, color_system=None)).event("x", {"hook_event_name": "Stop"})
    assert "Stop" in out.getvalue()
