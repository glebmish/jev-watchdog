import pytest

SESSION_ID = "0123456789abcdef"
TRANSCRIPT_LINES = ['{"type":"user","message":"fix the test"}', '{"type":"assistant","message":"ok"}']


@pytest.fixture
def transcript(tmp_path):
    path = tmp_path / f"{SESSION_ID}.jsonl"
    path.write_text("\n".join(TRANSCRIPT_LINES) + "\n", encoding="utf-8")
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
