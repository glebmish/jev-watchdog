import asyncio
import io

import pytest
from conftest import SESSION_ID, TRANSCRIPT_LINES
from rich.console import Console

from jev_watchdog.judge.base import JudgeRequest, Verdict
from jev_watchdog.judge.fake import FakeJudge
from jev_watchdog.pack import Question
from jev_watchdog.printer import Printer
from jev_watchdog.surfaces import ALL_EVENTS, JUDGING_EVENTS, LIFECYCLE_EVENTS, SurfaceRegistry
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
    assert len(ALL_EVENTS) == 9


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
