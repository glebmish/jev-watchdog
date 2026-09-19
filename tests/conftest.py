import pytest

from jev_watchdog.judge.base import Answer, Verdict

SESSION_ID = "0123456789abcdef"
TRANSCRIPT_LINES = [
    '{"type":"user","message":"fix the test"}',
    '{"type":"assistant","message":"ok"}',
]
BOOKKEEPING_LINES = [
    '{"type":"queue-operation","operation":"enqueue"}',
    '{"type":"attachment","attachment":{"type":"skill_listing","content":"huge"}}',
]


@pytest.fixture
def transcript(tmp_path):
    path = tmp_path / f"{SESSION_ID}.jsonl"
    path.write_text("\n".join([*BOOKKEEPING_LINES, *TRANSCRIPT_LINES]) + "\n", encoding="utf-8")
    return path


@pytest.fixture
def make_payload(transcript):
    def _make(event: str = "PostToolUse", **extra) -> dict:
        return {
            "session_id": SESSION_ID,
            "transcript_path": str(transcript),
            "cwd": "/work",
            "hook_event_name": event,
            **extra,
        }

    return _make


@pytest.fixture
def subagent_transcript(transcript):
    path = transcript.with_suffix("") / "subagents" / "agent-abc123.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text('{"type":"user","message":"explore"}\n', encoding="utf-8")
    return path


class ScriptedJudge:
    """Answers the n-th judgment with values[n]; the last entry repeats."""

    def __init__(self, values: list[dict[str, float]], name: str = "scripted") -> None:
        self.name, self.values, self.calls = name, values, []

    async def judge(self, req) -> Verdict:
        self.calls.append(req)
        values = self.values[min(len(self.calls), len(self.values)) - 1]
        answers = {qid: Answer(value) for qid, value in values.items()}
        return Verdict(answers, latency_ms=0.0, input_tokens=0, judge=self.name)

    async def aclose(self) -> None:
        pass
