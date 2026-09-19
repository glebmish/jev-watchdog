import asyncio
import io

import pytest
from conftest import SESSION_ID, TRANSCRIPT_LINES, ScriptedJudge
from rich.console import Console

from jev_watchdog.judge.base import JudgeRequest, Verdict
from jev_watchdog.judge.fake import FakeJudge
from jev_watchdog.pack import Question
from jev_watchdog.printer import Printer
from jev_watchdog.surfaces import (
    ALL_EVENTS,
    GATE_EVENTS,
    JUDGING_EVENTS,
    LIFECYCLE_EVENTS,
    SurfaceRegistry,
    TargetError,
)
from jev_watchdog.transcript import MAIN, SurfaceKey

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
    registry.handle(make_payload())
    registry.handle(make_payload(agent_id="abc123", agent_type="Explore"))
    registry.handle(make_payload())
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


async def test_judges_only_judging_events_with_raw_transcript(make_payload, out):
    judge = FakeJudge()
    registry = make_registry(judge, out)
    registry.handle(make_payload("SessionStart"))
    registry.handle(make_payload("SubagentStart", agent_id="abc123", agent_type="Explore"))
    await registry.drain()
    assert judge.calls == []

    registry.handle(make_payload("PostToolUse", tool_name="Bash"))
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
    registry.handle(make_payload())
    registry.handle(make_payload())  # queued behind the first
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
        registry.handle(make_payload())
        registry.handle(make_payload(agent_id="abc123", agent_type="Explore"))
    await registry.drain()
    assert len(judge.calls) == 6
    assert judge.max_per_surface == 1
    assert judge.max_total == 2
    await registry.shutdown()


async def test_judge_error_is_counted_and_worker_survives(make_payload, out):
    judge = FakeJudge(fail_with="over_limit")
    registry = make_registry(judge, out)
    registry.handle(make_payload())
    await registry.drain()
    judge.fail_with = None
    registry.handle(make_payload())
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
    registry.handle(make_payload())
    registry.handle(make_payload())
    await registry.drain()
    surface = registry.surfaces[SurfaceKey(SESSION_ID, MAIN)]
    assert surface.stats.judges["fake"].errors == {"other": 1} and surface.stats.judgments == 1
    await registry.shutdown()


async def test_no_transcript_yet_is_registration_only(make_payload, out):
    # The first UserPromptSubmit of a session fires before Claude Code creates the file.
    judge = FakeJudge()
    registry = make_registry(judge, out)
    registry.handle(make_payload("UserPromptSubmit", transcript_path="/nonexistent/x.jsonl"))
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
    registry.handle(make_payload())
    await registry.drain()
    assert judge.calls == []
    assert registry.stats.errors == {}
    assert "registered only" in out.getvalue()
    await registry.shutdown()


async def test_unreadable_transcript_is_an_error_without_a_judge_call(make_payload, tmp_path, out):
    judge = FakeJudge()
    registry = make_registry(judge, out)
    registry.handle(make_payload(transcript_path=str(tmp_path)))  # a directory
    await registry.drain()
    assert judge.calls == []
    assert registry.stats.errors == {"transcript": 1}
    await registry.shutdown()


async def test_subagent_stop_updates_transcript_path(make_payload, tmp_path, out):
    elsewhere = tmp_path / "agent-abc123.jsonl"
    elsewhere.write_text('{"type":"user"}\n')
    registry = make_registry(FakeJudge(), out)
    registry.handle(make_payload("SubagentStart", agent_id="abc123", agent_type="Explore"))
    registry.handle(
        make_payload("SubagentStop", agent_id="abc123", agent_type="Explore",
                     agent_transcript_path=str(elsewhere))
    )  # fmt: skip
    await registry.drain()
    assert registry.surfaces[SurfaceKey(SESSION_ID, "abc123")].transcript_path == elsewhere
    assert registry.stats.judgments == 1
    await registry.shutdown()


async def test_session_end_summarises_and_surface_can_resume(make_payload, out):
    registry = make_registry(FakeJudge(), out)
    registry.handle(make_payload())
    registry.handle(make_payload("SessionEnd", reason="clear"))
    await registry.drain()
    await asyncio.sleep(0)  # let the worker exit after its sentinel
    surface = registry.surfaces[SurfaceKey(SESSION_ID, MAIN)]
    assert all(worker.done() for worker in surface.workers.values())
    assert "judgments 1" in out.getvalue()

    registry.handle(make_payload())  # session resumed
    await registry.drain()
    assert surface.stats.judgments == 2
    await registry.shutdown()


async def test_bad_payloads_never_raise(out):
    registry = make_registry(FakeJudge(), out)
    registry.handle({"hook_event_name": "PostToolUse"})  # no session_id
    registry.handle({"session_id": "s"})  # no event name
    registry.bad_payload("not json")
    assert registry.stats.errors == {"payload": 3}
    assert registry.surfaces == {}


async def test_summaries_are_keyed_by_label(make_payload, out):
    registry = make_registry(FakeJudge(), out)
    registry.handle(make_payload())
    await registry.drain()
    assert list(registry.summaries()) == ["012345/main"]
    await registry.shutdown()


async def test_every_judge_sees_every_job_and_is_tracked_separately(make_payload, out):
    fast, slow = FakeJudge(name="fast"), FakeJudge(latency_s=0.05, name="slow")
    registry = make_registry(fast, out, slow)
    registry.handle(make_payload())
    registry.handle(make_payload())
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
        registry.handle(make_payload())
    await asyncio.sleep(0.05)
    assert registry.stats.judge("fast").judgments == 3
    assert registry.stats.judge("slow").judgments == 0
    await registry.shutdown()


async def test_lag_includes_time_queued_behind_earlier_events(make_payload, out):
    slow = FakeJudge(latency_s=0.05, name="slow")
    registry = make_registry(slow, out)
    for _ in range(3):
        registry.handle(make_payload())
    await registry.drain()
    lags = registry.stats.judges["slow"].lags_ms
    assert lags[0] >= 50 and lags[2] >= 150  # third job waited for the first two
    assert lags == sorted(lags)
    await registry.shutdown()


async def test_session_end_prints_one_summary_per_judge(make_payload, out):
    registry = make_registry(FakeJudge(name="fast"), out, FakeJudge(name="slow"))
    registry.handle(make_payload())
    registry.handle(make_payload("SessionEnd", reason="clear"))
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
    return SurfaceRegistry(list(judges), RULED, Printer(console), enforce=enforce)


def deny_of(body) -> str:
    assert body["hookSpecificOutput"]["permissionDecision"] == "deny"
    return body["hookSpecificOutput"]["permissionDecisionReason"]


async def test_tool_calls_pass_until_a_verdict_trips_then_every_one_is_rejected(make_payload, out):
    registry = ruled_registry(out, ScriptedJudge([{"exfil": 0.93}]))
    assert registry.handle(make_payload("PreToolUse", tool_name="Bash")) is None
    registry.handle(make_payload("PostToolUse", tool_name="Bash", tool_use_id="t1"))
    await registry.drain()
    for tool_name in ("Bash", "Read", "mcp__x__y"):
        reason = deny_of(registry.handle(make_payload("PreToolUse", tool_name=tool_name)))
        assert "exfil=0.93" in reason
    assert registry.stats.quarantines == 1 and registry.stats.rejected == 3
    assert "QUARANTINED" in out.getvalue() and "rejected" in out.getvalue()
    await registry.shutdown()


async def test_dry_run_reports_but_never_rejects(make_payload, out):
    registry = ruled_registry(out, ScriptedJudge([{"exfil": 0.93}]), enforce=False)
    registry.handle(make_payload("PostToolUse", tool_use_id="t1"))
    await registry.drain()
    assert registry.handle(make_payload("PreToolUse")) is None
    assert "would quarantine" in out.getvalue() and registry.quarantines.all() == []
    await registry.shutdown()


async def test_only_the_first_judge_decides(make_payload, out):
    calm, alarmed = ScriptedJudge([{"exfil": 0.1}], "calm"), ScriptedJudge([{"exfil": 1.0}], "loud")
    registry = ruled_registry(out, calm, alarmed)
    registry.handle(make_payload("PostToolUse", tool_use_id="t1"))
    await registry.drain()
    assert registry.handle(make_payload("PreToolUse")) is None
    assert "loud would quarantine" in out.getvalue()
    await registry.shutdown()


async def test_a_quarantined_subagent_does_not_block_its_parent(
    make_payload, subagent_transcript, out
):
    registry = ruled_registry(out, ScriptedJudge([{"exfil": 0.93}]))
    registry.handle(make_payload("PostToolUse", agent_id="abc123", agent_type="Explore",
                                 tool_use_id="t1"))  # fmt: skip
    await registry.drain()
    assert registry.handle(make_payload("PreToolUse", agent_id="abc123")) is not None
    assert registry.handle(make_payload("PreToolUse")) is None
    await registry.shutdown()


async def test_manual_quarantine_of_main_blocks_subagents_and_release_lifts_it(
    make_payload, subagent_transcript, out
):
    registry = ruled_registry(out, ScriptedJudge([{"exfil": 0.0}]), enforce=False)
    registry.handle(make_payload("SessionStart"))
    entry = registry.quarantine(SESSION_ID[:6], "testing")
    assert (entry.source, entry.label) == ("manual", f"{SESSION_ID[:6]}/main")
    assert "testing" in deny_of(registry.handle(make_payload("PreToolUse", agent_id="abc123")))
    assert registry.release(f"{SESSION_ID[:6]}/main") == entry
    assert registry.handle(make_payload("PreToolUse", agent_id="abc123")) is None
    await registry.shutdown()


async def test_release_forgets_the_evidence(make_payload, out):
    registry = ruled_registry(out, ScriptedJudge([{"exfil": 0.93}, {"exfil": 0.5}]))
    registry.handle(make_payload("PostToolUse", tool_use_id="t1"))
    await registry.drain()
    registry.release(SESSION_ID[:6])
    registry.handle(make_payload("PostToolUse", tool_use_id="t2"))
    await registry.drain()
    assert registry.handle(make_payload("PreToolUse")) is None  # 0.05 of evidence, not 0.53
    await registry.shutdown()


async def test_targets_must_match_exactly_one_known_surface(make_payload, subagent_transcript, out):
    registry = ruled_registry(out, ScriptedJudge([{"exfil": 0.0}]))
    registry.handle(make_payload("SessionStart"))
    registry.handle(make_payload("SubagentStart", agent_id="agent-abc123", agent_type="Explore"))
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
    assert registry.handle({"hook_event_name": "PreToolUse"}) is None
    assert registry.stats.errors == {"payload": 1}


async def test_an_ambiguous_target_is_a_conflict(make_payload, out):
    registry = ruled_registry(out, ScriptedJudge([{"exfil": 0.0}]))
    registry.handle(make_payload("SessionStart"))
    registry.handle(make_payload("SessionStart") | {"session_id": "0999"})
    with pytest.raises(TargetError) as error:
        registry.quarantine("0", "r")
    assert error.value.status == 409 and "ambiguous" in error.value.message
