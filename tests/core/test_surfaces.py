import asyncio
import io
import json
import threading
import time

import pytest
from conftest import SESSION_ID, TRANSCRIPT_LINES, ScriptedJudge
from rich.console import Console

from jev_watchdog.core.pack import Question
from jev_watchdog.core.surfaces import (
    ALL_EVENTS,
    GATE_EVENTS,
    JUDGING_EVENTS,
    LIFECYCLE_EVENTS,
    SurfaceRegistry,
    TargetError,
)
from jev_watchdog.core.transcript import MAIN, SurfaceKey
from jev_watchdog.display.printer import Printer
from jev_watchdog.judge.base import JudgeRequest, Verdict
from jev_watchdog.judge.fake import FakeJudge

QUESTIONS = [Question("exfil", "noul", "i", flag_threshold=0.7)]


@pytest.fixture
def out():
    return io.StringIO()


def make_registry(judge, out, *more_judges) -> SurfaceRegistry:
    console = Console(file=out, width=200, color_system=None)
    return SurfaceRegistry([judge, *more_judges], QUESTIONS, Printer(console))


def test_event_sets():
    assert not LIFECYCLE_EVENTS & JUDGING_EVENTS
    assert not GATE_EVENTS & (LIFECYCLE_EVENTS | JUDGING_EVENTS)
    assert len(ALL_EVENTS) == 10


async def test_one_surface_per_agent_thread(make_payload, subagent_transcript, out):
    registry = make_registry(FakeJudge(), out)
    await registry.handle(make_payload())
    await registry.handle(make_payload(agent_id="abc123", agent_type="Explore"))
    await registry.handle(make_payload())
    await registry.drain()
    assert set(registry.surfaces) == {
        SurfaceKey(SESSION_ID, MAIN),
        SurfaceKey(SESSION_ID, "abc123"),
    }
    assert registry.stats.surfaces == 2
    assert (
        registry.surfaces[SurfaceKey(SESSION_ID, "abc123")].transcript_path == subagent_transcript
    )
    await registry.shutdown()


async def test_the_judge_gets_each_line_without_the_harness_around_it(
    make_payload, transcript, out
):
    said = {"role": "assistant", "content": [{"type": "text", "text": "ok"}]}
    line = {"type": "assistant", "uuid": "77aa", "cwd": "/work", "message": {**said, "usage": {}}}
    transcript.write_text(json.dumps(line) + "\n", encoding="utf-8")
    judge = FakeJudge()
    registry = make_registry(judge, out)
    await registry.handle(make_payload("Stop"))
    await registry.drain()
    [sent] = judge.calls[0].transcript_lines
    assert json.loads(sent) == {"type": "assistant", "message": said}
    await registry.shutdown()


async def test_judges_only_judging_events_with_raw_transcript(make_payload, out):
    judge = FakeJudge()
    registry = make_registry(judge, out)
    await registry.handle(make_payload("SessionStart"))
    await registry.handle(make_payload("SubagentStart", agent_id="abc123", agent_type="Explore"))
    await registry.drain()
    assert judge.calls == []

    await registry.handle(make_payload("PostToolUse", tool_name="Bash"))
    await registry.drain()
    assert len(judge.calls) == 1
    request = judge.calls[0]
    assert request.transcript_lines == TRANSCRIPT_LINES
    assert request.surface == SurfaceKey(SESSION_ID, MAIN)
    assert request.event["tool_name"] == "Bash"
    assert registry.stats.judgments == 1
    assert registry.surfaces[request.surface].stats.judgments == 1
    assert " fake " in out.getvalue()
    await registry.shutdown()


async def test_transcript_is_snapshotted_at_receipt(make_payload, transcript, out):
    judge = FakeJudge(latency_s=0.02)
    registry = make_registry(judge, out)
    await registry.handle(make_payload())
    await registry.handle(make_payload())  # queued behind the first
    transcript.write_text(
        "\n".join([*TRANSCRIPT_LINES, '{"type":"assistant","message":"late"}']) + "\n"
    )
    await registry.drain()
    assert [len(call.transcript_lines) for call in judge.calls] == [2, 2]
    await registry.shutdown()


class ConcurrencyJudge(FakeJudge):
    def __init__(self):
        super().__init__(latency_s=0.02)
        self.active: dict[SurfaceKey, int] = {}
        self.max_per_surface = 0
        self.max_total = 0

    async def judge(self, req: JudgeRequest) -> Verdict:
        self.active[req.surface] = self.active.get(req.surface, 0) + 1
        self.max_per_surface = max(self.max_per_surface, self.active[req.surface])
        self.max_total = max(self.max_total, sum(self.active.values()))
        try:
            return await super().judge(req)
        finally:
            self.active[req.surface] -= 1


async def test_serial_within_surface_concurrent_across(make_payload, subagent_transcript, out):
    judge = ConcurrencyJudge()
    registry = make_registry(judge, out)
    for _ in range(3):
        await registry.handle(make_payload())
        await registry.handle(make_payload(agent_id="abc123", agent_type="Explore"))
    await registry.drain()
    assert len(judge.calls) == 6
    assert judge.max_per_surface == 1
    assert judge.max_total == 2
    await registry.shutdown()


async def test_judge_error_is_counted_and_worker_survives(make_payload, out):
    judge = FakeJudge(fail_with="over_limit")
    registry = make_registry(judge, out)
    await registry.handle(make_payload())
    await registry.drain()
    judge.fail_with = None
    await registry.handle(make_payload())
    await registry.drain()
    surface = registry.surfaces[SurfaceKey(SESSION_ID, MAIN)]
    assert surface.stats.judges["fake"].errors == {"over_limit": 1}
    assert surface.stats.judgments == 1
    assert registry.stats.judges["fake"].errors == {"over_limit": 1}
    assert registry.stats.errors == {}
    assert "fake error over_limit" in out.getvalue()
    await registry.shutdown()


class BuggyJudge(FakeJudge):
    async def judge(self, req: JudgeRequest) -> Verdict:
        if not self.calls:
            self.calls.append(req)
            raise RuntimeError("boom")
        return await super().judge(req)


async def test_unexpected_judge_exception_does_not_kill_worker(make_payload, out):
    registry = make_registry(BuggyJudge(), out)
    await registry.handle(make_payload())
    await registry.handle(make_payload())
    await registry.drain()
    surface = registry.surfaces[SurfaceKey(SESSION_ID, MAIN)]
    assert surface.stats.judges["fake"].errors == {"other": 1} and surface.stats.judgments == 1
    await registry.shutdown()


async def test_no_transcript_yet_is_registration_only(make_payload, out):
    # The first UserPromptSubmit of a session fires before Claude Code creates the file.
    judge = FakeJudge()
    registry = make_registry(judge, out)
    await registry.handle(make_payload("UserPromptSubmit", transcript_path="/nonexistent/x.jsonl"))
    await registry.drain()
    assert judge.calls == []
    assert registry.stats.errors == {}
    assert set(registry.surfaces) == {SurfaceKey(SESSION_ID, MAIN)}
    assert "registered only" in out.getvalue()
    await registry.shutdown()


async def test_transcript_without_conversation_is_registration_only(make_payload, transcript, out):
    transcript.write_text('{"type":"attachment","attachment":{"type":"skill_listing"}}\n')
    judge = FakeJudge()
    registry = make_registry(judge, out)
    await registry.handle(make_payload())
    await registry.drain()
    assert judge.calls == []
    assert registry.stats.errors == {}
    assert "registered only" in out.getvalue()
    await registry.shutdown()


async def test_unreadable_transcript_is_an_error_without_a_judge_call(make_payload, tmp_path, out):
    judge = FakeJudge()
    registry = make_registry(judge, out)
    await registry.handle(make_payload(transcript_path=str(tmp_path)))  # a directory
    await registry.drain()
    assert judge.calls == []
    assert registry.stats.errors == {"transcript": 1}
    await registry.shutdown()


async def test_subagent_stop_updates_transcript_path(make_payload, tmp_path, out):
    elsewhere = tmp_path / "agent-abc123.jsonl"
    elsewhere.write_text('{"type":"user"}\n')
    registry = make_registry(FakeJudge(), out)
    await registry.handle(make_payload("SubagentStart", agent_id="abc123", agent_type="Explore"))
    await registry.handle(
        make_payload("SubagentStop", agent_id="abc123", agent_type="Explore",
                     agent_transcript_path=str(elsewhere))
    )  # fmt: skip
    await registry.drain()
    assert registry.surfaces[SurfaceKey(SESSION_ID, "abc123")].transcript_path == elsewhere
    assert registry.stats.judgments == 1
    await registry.shutdown()


async def test_session_end_summarises_and_surface_can_resume(make_payload, out):
    registry = make_registry(FakeJudge(), out)
    await registry.handle(make_payload())
    await registry.handle(make_payload("SessionEnd", reason="clear"))
    await registry.drain()
    await asyncio.sleep(0)  # let the worker exit after its sentinel
    surface = registry.surfaces[SurfaceKey(SESSION_ID, MAIN)]
    assert all(worker.done() for worker in surface.workers.values())
    assert "judgments 1" in out.getvalue()

    await registry.handle(make_payload())  # session resumed
    await registry.drain()
    assert surface.stats.judgments == 2
    await registry.shutdown()


async def test_bad_payloads_never_raise(out):
    registry = make_registry(FakeJudge(), out)
    await registry.handle({"hook_event_name": "PostToolUse"})  # no session_id
    await registry.handle({"session_id": "s"})  # no event name
    registry.bad_payload("not json")
    assert registry.stats.errors == {"payload": 3}
    assert registry.surfaces == {}


async def test_summaries_are_keyed_by_label(make_payload, out):
    registry = make_registry(FakeJudge(), out)
    await registry.handle(make_payload())
    await registry.drain()
    assert list(registry.summaries()) == ["012345/main"]
    await registry.shutdown()


async def test_every_judge_sees_every_job_and_is_tracked_separately(make_payload, out):
    fast, slow = FakeJudge(name="fast"), FakeJudge(latency_s=0.05, name="slow")
    registry = make_registry(fast, out, slow)
    await registry.handle(make_payload())
    await registry.handle(make_payload())
    await registry.drain()
    assert len(fast.calls) == len(slow.calls) == 2
    assert fast.calls[0].transcript_lines is slow.calls[0].transcript_lines  # one snapshot
    stats = registry.stats
    assert list(stats.judges) == ["fast", "slow"]
    assert stats.judges["fast"].judgments == stats.judges["slow"].judgments == 2
    surface = registry.surfaces[SurfaceKey(SESSION_ID, MAIN)]
    assert list(surface.stats.judges) == ["fast", "slow"]
    assert " fast " in out.getvalue() and " slow " in out.getvalue()
    await registry.shutdown()


async def test_a_slow_judge_does_not_hold_back_a_fast_one(make_payload, out):
    fast, slow = FakeJudge(name="fast"), FakeJudge(latency_s=0.2, name="slow")
    registry = make_registry(fast, out, slow)
    for _ in range(3):
        await registry.handle(make_payload())
    await asyncio.sleep(0.05)
    assert registry.stats.judge("fast").judgments == 3
    assert registry.stats.judge("slow").judgments == 0
    await registry.shutdown()


async def test_lag_includes_time_queued_behind_earlier_events(make_payload, out):
    slow = FakeJudge(latency_s=0.05, name="slow")
    registry = make_registry(slow, out)
    for _ in range(3):
        await registry.handle(make_payload())
    await registry.drain()
    lags = list(registry.stats.judges["slow"].lags_ms)
    assert lags[0] >= 50 and lags[2] >= 150  # third job waited for the first two
    assert lags == sorted(lags)
    await registry.shutdown()


async def test_session_end_prints_one_summary_per_judge(make_payload, out):
    registry = make_registry(FakeJudge(name="fast"), out, FakeJudge(name="slow"))
    await registry.handle(make_payload())
    await registry.handle(make_payload("SessionEnd", reason="clear"))
    await registry.drain()
    assert "· fast · judgments 1" in out.getvalue()
    assert "· slow · judgments 1" in out.getvalue()
    await registry.shutdown()


def test_judge_names_must_be_unique(out):
    with pytest.raises(ValueError, match="duplicate judge"):
        make_registry(FakeJudge(), out, FakeJudge())


RULED = [Question("exfil", "noul", "i", flag_threshold=0.55, quarantine_ref=0.45,
                  quarantine_limit=0.2)]  # fmt: skip


def ruled_registry(out, *judges, enforce=True) -> SurfaceRegistry:
    console = Console(file=out, width=200, color_system=None)
    return SurfaceRegistry(
        list(judges), RULED, Printer(console), enforce=enforce, transcript_wait_s=0
    )


def deny_of(body) -> str:
    assert body["hookSpecificOutput"]["permissionDecision"] == "deny"
    return body["hookSpecificOutput"]["permissionDecisionReason"]


async def test_tool_calls_pass_until_a_verdict_trips_then_every_one_is_rejected(make_payload, out):
    registry = ruled_registry(out, ScriptedJudge([{"exfil": 0.93}]))
    assert await registry.handle(make_payload("PreToolUse", tool_name="Bash")) is None
    await registry.handle(make_payload("PostToolUse", tool_name="Bash", tool_use_id="t1"))
    await registry.drain()
    for tool_name in ("Bash", "Read", "mcp__x__y"):
        reason = deny_of(await registry.handle(make_payload("PreToolUse", tool_name=tool_name)))
        assert "exfil=0.93" in reason
    assert registry.stats.quarantines == 1 and registry.stats.rejected == 3
    assert "QUARANTINED" in out.getvalue() and "rejected" in out.getvalue()
    await registry.shutdown()


async def test_dry_run_reports_but_never_rejects(make_payload, out):
    registry = ruled_registry(out, ScriptedJudge([{"exfil": 0.93}]), enforce=False)
    await registry.handle(make_payload("PostToolUse", tool_use_id="t1"))
    await registry.drain()
    assert await registry.handle(make_payload("PreToolUse")) is None
    assert "would quarantine" in out.getvalue() and registry.quarantines.all() == []
    await registry.shutdown()


async def test_only_the_first_judge_decides(make_payload, out):
    calm, alarmed = ScriptedJudge([{"exfil": 0.1}], "calm"), ScriptedJudge([{"exfil": 1.0}], "loud")
    registry = ruled_registry(out, calm, alarmed)
    await registry.handle(make_payload("PostToolUse", tool_use_id="t1"))
    await registry.drain()
    assert await registry.handle(make_payload("PreToolUse")) is None
    assert "loud would quarantine" in out.getvalue()
    await registry.shutdown()


async def test_a_quarantined_subagent_does_not_block_its_parent(
    make_payload, subagent_transcript, out
):
    registry = ruled_registry(out, ScriptedJudge([{"exfil": 0.93}]))
    await registry.handle(make_payload("PostToolUse", agent_id="abc123", agent_type="Explore",
                                 tool_use_id="t1"))  # fmt: skip
    await registry.drain()
    assert await registry.handle(make_payload("PreToolUse", agent_id="abc123")) is not None
    assert await registry.handle(make_payload("PreToolUse")) is None
    await registry.shutdown()


async def test_manual_quarantine_of_main_blocks_subagents_and_release_lifts_it(
    make_payload, subagent_transcript, out
):
    registry = ruled_registry(out, ScriptedJudge([{"exfil": 0.0}]), enforce=False)
    await registry.handle(make_payload("SessionStart"))
    entry = registry.quarantine(SESSION_ID[:6], "testing")
    assert (entry.source, entry.label) == ("manual", f"{SESSION_ID[:6]}/main")
    assert "testing" in deny_of(
        await registry.handle(make_payload("PreToolUse", agent_id="abc123"))
    )
    assert registry.release(f"{SESSION_ID[:6]}/main") == entry
    assert await registry.handle(make_payload("PreToolUse", agent_id="abc123")) is None
    await registry.shutdown()


async def test_release_forgets_the_evidence(make_payload, out):
    registry = ruled_registry(out, ScriptedJudge([{"exfil": 0.93}, {"exfil": 0.5}]))
    await registry.handle(make_payload("PostToolUse", tool_use_id="t1"))
    await registry.drain()
    registry.release(SESSION_ID[:6])
    await registry.handle(make_payload("PostToolUse", tool_use_id="t2"))
    await registry.drain()
    assert await registry.handle(make_payload("PreToolUse")) is None  # 0.05 of evidence, not 0.53
    await registry.shutdown()


async def test_targets_must_match_exactly_one_known_surface(make_payload, subagent_transcript, out):
    registry = ruled_registry(out, ScriptedJudge([{"exfil": 0.0}]))
    await registry.handle(make_payload("SessionStart"))
    await registry.handle(
        make_payload("SubagentStart", agent_id="agent-abc123", agent_type="Explore")
    )
    assert registry.quarantine(f"{SESSION_ID[:6]}/abc1:Explore", "r").key.agent_id == "agent-abc123"
    for target, status in (("nope", 404), ("", 404), (f"{SESSION_ID[:6]}/zzz", 404)):
        with pytest.raises(TargetError) as error:
            registry.quarantine(target, "r")
        assert error.value.status == status
    with pytest.raises(TargetError) as error:
        registry.quarantine(f"{SESSION_ID[:6]}/abc1", "again")
    assert error.value.status == 409
    with pytest.raises(TargetError) as error:
        registry.release(SESSION_ID[:6])  # main is not quarantined
    assert error.value.status == 404
    await registry.shutdown()


async def test_the_gate_never_raises_on_bad_payloads(out):
    registry = ruled_registry(out, ScriptedJudge([{"exfil": 0.0}]))
    assert await registry.handle({"hook_event_name": "PreToolUse"}) is None
    assert registry.stats.errors == {"payload": 1}


async def test_an_ambiguous_target_is_a_conflict(make_payload, out):
    registry = ruled_registry(out, ScriptedJudge([{"exfil": 0.0}]))
    await registry.handle(make_payload("SessionStart"))
    await registry.handle(make_payload("SessionStart") | {"session_id": "0999"})
    with pytest.raises(TargetError) as error:
        registry.quarantine("0", "r")
    assert error.value.status == 409 and "ambiguous" in error.value.message


@pytest.mark.parametrize(
    "bad",
    [
        {"transcript_path": 123},
        {"agent_id": 7},
        {"session_id": ["a"]},
        {"hook_event_name": 5},
    ],
)
@pytest.mark.parametrize("event", ["PreToolUse", "PostToolUse"])
async def test_wrongly_typed_payload_fields_are_payload_errors(make_payload, out, event, bad):
    registry = ruled_registry(out, ScriptedJudge([{"exfil": 0.0}]))
    assert await registry.handle(make_payload(event) | bad) is None
    assert registry.stats.errors == {"payload": 1} and registry.surfaces == {}


async def test_a_rule_trip_on_a_manually_quarantined_thread_is_recorded(make_payload, out):
    registry = ruled_registry(out, ScriptedJudge([{"exfil": 0.93}]))
    await registry.handle(make_payload("SessionStart"))
    registry.quarantine(SESSION_ID[:6], "looks odd")
    await registry.handle(make_payload("PostToolUse", tool_use_id="t1"))
    await registry.drain()
    (entry,) = registry.quarantines.all()
    assert entry.source == "manual" and "looks odd" in entry.reason
    assert "rule:scripted also tripped: exfil=0.93" in entry.reason
    assert "would quarantine" not in out.getvalue()
    assert "also tripped" in out.getvalue()
    await registry.shutdown()


def behind_transcript(transcript, tool_use_id="t1"):
    """Leave the transcript as Claude Code has it when PostToolUse fires: the result of the
    tool call is not flushed yet. Returns a function that flushes it."""
    block = {"type": "tool_result", "tool_use_id": tool_use_id, "content": "ok"}
    line = json.dumps({"type": "user", "message": {"role": "user", "content": [block]}})

    def flush():
        with transcript.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    return line, flush


def waiting_registry(out, judge, wait_s) -> SurfaceRegistry:
    console = Console(file=out, width=200, color_system=None)
    return SurfaceRegistry([judge], QUESTIONS, Printer(console), transcript_wait_s=wait_s)


async def test_a_tool_event_is_judged_once_its_result_reaches_the_transcript(
    make_payload, transcript, out
):
    judge = FakeJudge()
    registry = waiting_registry(out, judge, wait_s=5)
    line, flush = behind_transcript(transcript)
    await registry.handle(make_payload("PostToolUse", tool_use_id="t1"))
    await asyncio.sleep(0.1)
    assert judge.calls == []  # still waiting for the transcript
    flush()
    await registry.drain()
    assert judge.calls[0].transcript_lines == [*TRANSCRIPT_LINES, line]
    assert "behind" not in out.getvalue()
    await registry.shutdown()


async def test_a_transcript_that_never_catches_up_is_judged_as_it_is(make_payload, transcript, out):
    judge = FakeJudge()
    registry = waiting_registry(out, judge, wait_s=0.1)
    await registry.handle(make_payload("PostToolUse", tool_use_id="t1"))
    await registry.drain()
    assert judge.calls[0].transcript_lines == TRANSCRIPT_LINES
    assert "transcript still behind after 100ms" in out.getvalue()
    await registry.shutdown()


async def test_waiting_keeps_the_order_of_a_threads_events(make_payload, transcript, out):
    judge = FakeJudge()
    registry = waiting_registry(out, judge, wait_s=5)
    _, flush = behind_transcript(transcript)
    await registry.handle(make_payload("PostToolUse", tool_use_id="t1"))
    await registry.handle(make_payload("Stop"))
    await registry.handle(make_payload("SessionEnd"))
    await asyncio.sleep(0.1)
    assert judge.calls == []  # Stop does not overtake the waiting tool event
    flush()
    await registry.drain()
    assert [call.event["hook_event_name"] for call in judge.calls] == ["PostToolUse", "Stop"]
    await registry.shutdown()


async def test_an_up_to_date_transcript_is_snapshotted_at_once(make_payload, transcript, out):
    judge = FakeJudge()
    registry = waiting_registry(out, judge, wait_s=5)
    line, flush = behind_transcript(transcript)
    flush()
    await registry.handle(make_payload("PostToolUse", tool_use_id="t1"))
    transcript.write_text("", encoding="utf-8")  # replay overwrites the file right after handle()
    await registry.drain()
    assert judge.calls[0].transcript_lines == [*TRANSCRIPT_LINES, line]
    await registry.shutdown()


async def test_context_reaches_every_thread_of_the_session(make_payload, subagent_transcript, out):
    judge = FakeJudge()
    registry = make_registry(judge, out)
    await registry.handle(make_payload("SessionStart"))
    assert registry.set_context(SESSION_ID[:6], "  staging deploys are expected  ") == (
        f"{SESSION_ID[:6]}/main"
    )
    await registry.handle(make_payload("PostToolUse", tool_name="Bash"))
    await registry.handle(make_payload("PostToolUse", agent_id="abc123", agent_type="Explore"))
    await registry.drain()
    assert [call.context for call in judge.calls] == ["staging deploys are expected"] * 2
    assert "context set: staging deploys are expected" in out.getvalue()

    registry.set_context(SESSION_ID[:6], "")
    await registry.handle(make_payload("Stop"))
    await registry.drain()
    assert judge.calls[-1].context is None
    assert "context cleared" in out.getvalue()
    await registry.shutdown()


async def test_default_context_applies_until_a_session_sets_its_own(make_payload, out):
    judge = FakeJudge()
    console = Console(file=out, width=200, color_system=None)
    registry = SurfaceRegistry([judge], QUESTIONS, Printer(console), context="everywhere")
    await registry.handle(make_payload("Stop"))
    await registry.drain()
    registry.set_context(SESSION_ID[:6], "just here")
    await registry.handle(make_payload("Stop"))
    await registry.drain()
    assert [call.context for call in judge.calls] == ["everywhere", "just here"]
    await registry.shutdown()


def test_context_needs_a_known_session(out):
    with pytest.raises(TargetError) as error:
        make_registry(FakeJudge(), out).set_context("nope", "x")
    assert error.value.status == 404


async def test_context_questions_are_asked_only_with_a_context(make_payload, out):
    judge = FakeJudge()
    questions = [
        Question("exfil", "noul", "i", flag_threshold=0.7, context_instructions="i, or context"),
        Question("against_context", "noul", "i", needs_context=True),
    ]
    registry = SurfaceRegistry(
        [judge], questions, Printer(Console(file=out, width=200, color_system=None))
    )
    await registry.handle(make_payload("Stop"))
    await registry.drain()
    registry.set_context(SESSION_ID[:6], "offline only")
    await registry.handle(make_payload("Stop"))
    await registry.drain()
    assert [[(q.id, q.instructions) for q in call.questions] for call in judge.calls] == [
        [("exfil", "i")],
        [("exfil", "i, or context"), ("against_context", "i")],
    ]
    await registry.shutdown()


async def test_reading_a_transcript_does_not_hold_up_other_hooks(make_payload, out, monkeypatch):
    """A slow read (a huge file, a network mount) used to stall every hook, the deny included."""
    reading, go_on = threading.Event(), threading.Event()

    def slow_read(path):
        reading.set()
        go_on.wait(5)
        return TRANSCRIPT_LINES

    monkeypatch.setattr("jev_watchdog.core.surfaces.read_lines", slow_read)
    judge = FakeJudge()
    registry = make_registry(judge, out)
    await registry.handle(make_payload("SessionStart"))
    registry.quarantine(SESSION_ID[:6], "testing")
    stop = asyncio.create_task(registry.handle(make_payload("Stop")))
    await asyncio.to_thread(reading.wait, 5)
    assert deny_of(await registry.handle(make_payload("PreToolUse"))) is not None
    go_on.set()
    await stop
    await registry.drain()
    assert len(judge.calls) == 1
    await registry.shutdown()


async def test_a_slow_read_does_not_let_a_later_event_overtake(make_payload, out, monkeypatch):
    delays = iter([0.1, 0.0])

    def uneven_read(path):
        time.sleep(next(delays))
        return TRANSCRIPT_LINES

    monkeypatch.setattr("jev_watchdog.core.surfaces.read_lines", uneven_read)
    judge = FakeJudge()
    registry = make_registry(judge, out)
    await asyncio.gather(
        registry.handle(make_payload("UserPromptSubmit")), registry.handle(make_payload("Stop"))
    )
    await registry.drain()
    assert [call.event["hook_event_name"] for call in judge.calls] == ["UserPromptSubmit", "Stop"]
    await registry.shutdown()


async def test_a_transcript_path_that_is_not_a_file_is_an_error(make_payload, tmp_path, out):
    import os

    fifo = tmp_path / "pipe.jsonl"
    os.mkfifo(fifo)
    judge = FakeJudge()
    registry = make_registry(judge, out)
    await asyncio.wait_for(registry.handle(make_payload(transcript_path=str(fifo))), 2)
    await registry.drain()
    assert judge.calls == [] and registry.stats.errors == {"transcript": 1}
    assert "not a regular file" in out.getvalue()
    await registry.shutdown()


def parallel_calls(transcript) -> list[str]:
    """Two tool calls made in one turn: both uses are written, then both results."""

    def line(role, block):
        return json.dumps({"type": role, "message": {"role": role, "content": [block]}})

    lines = [
        *TRANSCRIPT_LINES,
        line("assistant", {"type": "tool_use", "id": "a", "name": "Read", "input": {}}),
        line("assistant", {"type": "tool_use", "id": "b", "name": "Bash", "input": {}}),
        line("user", {"type": "tool_result", "tool_use_id": "a", "content": "ok"}),
        line("user", {"type": "tool_result", "tool_use_id": "b", "content": "denied"}),
    ]
    transcript.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return lines


async def test_a_tool_event_is_judged_on_the_transcript_up_to_its_own_result(
    make_payload, transcript, out
):
    lines = parallel_calls(transcript)
    judge = FakeJudge()
    registry = make_registry(judge, out)
    await registry.handle(make_payload("PostToolUse", tool_use_id="a"))
    await registry.handle(make_payload("PostToolUse", tool_use_id="b"))
    await registry.handle(make_payload("Stop"))
    await registry.drain()
    assert [call.transcript_lines for call in judge.calls] == [lines[:5], lines, lines]
    await registry.shutdown()


class LastResultJudge(ScriptedJudge):
    """Scores the most recent action, as the questions ask: 0.72 for b's result, else 0."""

    async def judge(self, req):
        self.values = [{"exfil": 0.72 if '"denied"' in req.transcript_lines[-1] else 0.0}]
        return await super().judge(req)


async def test_parallel_tool_calls_are_not_folded_twice(make_payload, transcript, out):
    parallel_calls(transcript)
    judge = LastResultJudge([])
    questions = [Question("exfil", "noul", "i", quarantine_ref=0.6, quarantine_limit=0.2)]
    registry = SurfaceRegistry([judge], questions, Printer(Console(file=out)), enforce=True)
    await registry.handle(make_payload("PostToolUse", tool_use_id="a"))
    await registry.handle(make_payload("PostToolUse", tool_use_id="b"))
    await registry.drain()
    evidence = registry.decider.evidence(SurfaceKey(SESSION_ID, MAIN), judge.name)
    assert evidence == {"exfil": pytest.approx(0.12)}
    assert registry.quarantines.all() == []
    await registry.shutdown()


async def test_a_waited_for_result_cuts_the_transcript_too(make_payload, transcript, out):
    judge = FakeJudge()
    registry = waiting_registry(out, judge, wait_s=5)
    line, flush = behind_transcript(transcript)
    await registry.handle(make_payload("PostToolUse", tool_use_id="t1"))
    flush()
    with transcript.open("a", encoding="utf-8") as fh:
        fh.write('{"type":"assistant","message":"the next thing"}\n')
    await registry.drain()
    assert judge.calls[0].transcript_lines == [*TRANSCRIPT_LINES, line]
    await registry.shutdown()


async def test_a_pack_value_of_the_wrong_type_does_not_kill_the_worker(make_payload, out):
    """It used to: the rest of the queue was never judged and replay's drain() never returned."""
    judge = ScriptedJudge([{"exfil": 0.9}])
    questions = [Question("exfil", "noul", "i", flag_threshold="0.7")]
    registry = SurfaceRegistry([judge], questions, Printer(Console(file=out, width=200)))
    await registry.handle(make_payload("Stop"))
    await registry.handle(make_payload("Stop"))
    await asyncio.wait_for(registry.drain(), 2)
    assert len(judge.calls) == 2
    assert registry.stats.judge("scripted").errors == {"other": 2}
    assert "error other: TypeError" in out.getvalue()
    await registry.shutdown()


async def test_a_failing_observer_does_not_kill_the_worker(make_payload, out):
    def observer(*args):
        raise RuntimeError("observer bug")

    judge = FakeJudge()
    registry = make_registry(judge, out)
    registry.on_verdict = observer
    await registry.handle(make_payload("Stop"))
    await registry.handle(make_payload("Stop"))
    await asyncio.wait_for(registry.drain(), 2)
    assert len(judge.calls) == 2
    assert "error other: RuntimeError('observer bug')" in out.getvalue()
    await registry.shutdown()


async def test_a_worker_that_dies_is_reported(make_payload, out, monkeypatch):
    registry = make_registry(FakeJudge(), out)

    def broken_summary(*args):
        raise RuntimeError("summary bug")

    monkeypatch.setattr(registry.printer, "surface_summary", broken_summary)
    await registry.handle(make_payload("Stop"))
    await registry.handle(make_payload("SessionEnd"))
    await asyncio.wait_for(registry.drain(), 2)
    await asyncio.sleep(0)  # done callbacks run on the next turn of the loop
    assert registry.stats.errors == {"worker": 1}
    assert "fake:012345/main died: RuntimeError('summary bug')" in out.getvalue()


async def test_an_intake_worker_that_dies_is_reported(make_payload, transcript, out, monkeypatch):
    registry = waiting_registry(out, FakeJudge(), wait_s=5)

    async def broken_wait(surface, tool_use_id):
        raise RuntimeError("wait bug")

    monkeypatch.setattr(registry, "_caught_up", broken_wait)
    await registry.handle(make_payload("PostToolUse", tool_use_id="t1"))
    await asyncio.wait_for(registry.drain(), 2)
    await asyncio.sleep(0)
    assert registry.stats.errors == {"worker": 1}
    assert "intake:012345/main died: RuntimeError('wait bug')" in out.getvalue()


async def test_events_after_a_session_end_are_still_judged(make_payload, out):
    """A resumed session: its job sat behind the end-of-session mark the worker stopped at."""
    judge = FakeJudge(latency_s=0.02)
    registry = make_registry(judge, out)
    await registry.handle(make_payload("Stop"))
    await registry.handle(make_payload("SessionEnd"))
    await registry.handle(make_payload("Stop"))
    await asyncio.wait_for(registry.drain(), 2)
    assert len(judge.calls) == 2
    await registry.shutdown()


# --- forgetting: a service runs for weeks -------------------------------------------------


class Clock:
    """A clock the test moves by hand."""

    def __init__(self) -> None:
        from datetime import datetime

        self.now = datetime(2026, 9, 20, 9, 0, 0)

    def __call__(self):
        return self.now

    def pass_hours(self, hours: float) -> None:
        from datetime import timedelta

        self.now += timedelta(hours=hours)


def aging_registry(out, judge, **options) -> tuple[SurfaceRegistry, Clock]:
    clock = Clock()
    printer = Printer(Console(file=out, width=200, color_system=None), clock=clock)
    return SurfaceRegistry([judge], RULED, printer, transcript_wait_s=0, **options), clock


async def test_a_thread_not_heard_from_for_a_day_is_forgotten(make_payload, out):
    registry, clock = aging_registry(out, ScriptedJudge([{"exfil": 0.55}]))
    await registry.handle(make_payload("PostToolUse", session_id="aaaa-1", tool_use_id="t"))
    await registry.drain()
    registry.set_context("aaaa", "known")
    first = SurfaceKey("aaaa-1", MAIN)
    workers = list(registry.surfaces[first].workers.values())
    assert registry.decider.evidence(first, "scripted") == {"exfil": 0.1}

    clock.pass_hours(23)
    await registry.handle(make_payload("SessionStart", session_id="bbbb-2"))
    assert first in registry.surfaces  # not yet

    clock.pass_hours(2)
    await registry.handle(make_payload("SessionStart", session_id="cccc-3"))
    assert [key.session_id for key in registry.surfaces] == ["bbbb-2", "cccc-3"]
    assert registry.decider.evidence(first, "scripted") == {}  # or a resumed thread inherits it
    assert registry.history.series(first) == {"judges": {}, "marks": []}
    assert registry.contexts == {}
    await asyncio.sleep(0)
    assert all(worker.cancelled() for worker in workers)  # parked on its queue for good
    with pytest.raises(TargetError, match="no agent thread matches"):
        registry.release("aaaa")
    assert registry.stats.surfaces == 3  # a count of what was seen, not of what is kept
    await registry.shutdown()


async def test_a_swarm_is_kept_whole_however_large(make_payload, out):
    registry, _ = aging_registry(out, FakeJudge())
    for n in range(1000):
        await registry.handle(make_payload("SessionStart", session_id=f"{n:04d}-agent"))
    assert len(registry.surfaces) == 1000
    await registry.shutdown()


async def test_a_thread_that_is_heard_from_again_stays(make_payload, out):
    registry, clock = aging_registry(out, FakeJudge())
    await registry.handle(make_payload("SessionStart", session_id="aaaa-1"))
    await registry.handle(make_payload("SessionStart", session_id="bbbb-2"))
    clock.pass_hours(20)
    await registry.handle(make_payload("Stop", session_id="aaaa-1"))
    clock.pass_hours(20)
    await registry.handle(make_payload("SessionStart", session_id="cccc-3"))
    assert [key.session_id for key in registry.surfaces] == ["aaaa-1", "cccc-3"]
    await registry.shutdown()


async def test_a_quarantined_thread_is_never_forgotten(make_payload, out):
    """It has to be there to be released, and to be seen in the dashboard."""
    registry, clock = aging_registry(out, FakeJudge())
    await registry.handle(make_payload("SessionStart", session_id="aaaa-1"))
    await registry.handle(make_payload("SessionStart", session_id="bbbb-2"))
    registry.quarantine("aaaa", "held")
    clock.pass_hours(100)
    await registry.handle(make_payload("SessionStart", session_id="cccc-3"))
    assert [key.session_id for key in registry.surfaces] == ["aaaa-1", "cccc-3"]
    assert registry.release("aaaa").reason == "held"
    await registry.shutdown()


async def test_a_session_keeps_its_context_while_any_of_its_threads_is_kept(
    make_payload, subagent_transcript, out
):
    registry, clock = aging_registry(out, FakeJudge())
    await registry.handle(make_payload("SessionStart"))
    registry.set_context(SESSION_ID[:6], "known")
    clock.pass_hours(20)
    await registry.handle(make_payload("SubagentStart", agent_id="abc123"))
    clock.pass_hours(20)
    await registry.handle(make_payload("SessionStart", session_id="zzzz-9"))  # forgets main
    assert SurfaceKey(SESSION_ID, MAIN) not in registry.surfaces
    assert registry.contexts == {SESSION_ID: "known"}  # the subagent is still judged with it
    await registry.shutdown()


async def test_the_ttl_can_be_set(make_payload, out):
    from datetime import timedelta

    registry, clock = aging_registry(out, FakeJudge(), thread_ttl=timedelta(minutes=5))
    await registry.handle(make_payload("SessionStart", session_id="aaaa-1"))
    clock.pass_hours(0.1)
    await registry.handle(make_payload("SessionStart", session_id="bbbb-2"))
    assert [key.session_id for key in registry.surfaces] == ["bbbb-2"]
    await registry.shutdown()
