from pathlib import Path

from jev_watchdog.transcript import (
    MAIN,
    SurfaceKey,
    conversation_lines,
    has_tool_result,
    read_lines,
    resolve_transcript_path,
    surface_key,
)

MAIN_PAYLOAD = {
    "session_id": "0123456789abcdef",
    "transcript_path": "/tmp/proj/0123456789abcdef.jsonl",
    "hook_event_name": "PostToolUse",
}
SUB_PAYLOAD = {**MAIN_PAYLOAD, "agent_id": "a30d775b3a621d99c", "agent_type": "Explore"}


def test_main_thread_key_and_path():
    assert surface_key(MAIN_PAYLOAD) == SurfaceKey("0123456789abcdef", MAIN)
    assert resolve_transcript_path(MAIN_PAYLOAD) == Path("/tmp/proj/0123456789abcdef.jsonl")


def test_subagent_key():
    assert surface_key(SUB_PAYLOAD) == SurfaceKey("0123456789abcdef", "a30d775b3a621d99c")


def test_subagent_path_is_derived_from_main_transcript():
    assert resolve_transcript_path(SUB_PAYLOAD) == Path(
        "/tmp/proj/0123456789abcdef/subagents/agent-a30d775b3a621d99c.jsonl"
    )


def test_explicit_agent_transcript_path_wins():
    payload = {**SUB_PAYLOAD, "agent_transcript_path": "/elsewhere/agent-x.jsonl"}
    assert resolve_transcript_path(payload) == Path("/elsewhere/agent-x.jsonl")


def test_agent_prefix_is_not_doubled():
    payload = {**SUB_PAYLOAD, "agent_id": "agent-abc123"}
    assert resolve_transcript_path(payload).name == "agent-abc123.jsonl"


def test_tilde_is_expanded():
    payload = {**MAIN_PAYLOAD, "transcript_path": "~/x.jsonl"}
    assert resolve_transcript_path(payload) == Path.home() / "x.jsonl"


def test_labels():
    assert SurfaceKey("0123456789abcdef", MAIN).label() == "012345/main"
    assert SurfaceKey("0123456789abcdef", "a30d775b3a").label("Explore") == "012345/a30d77:Explore"
    assert SurfaceKey("0123456789abcdef", "a30d775b3a").label() == "012345/a30d77"


def test_read_lines_keeps_lines_identical(tmp_path):
    # U+2028 is legal unescaped inside a JSON string; str.splitlines() would split on it.
    first = '{"type":"user","text":"a b"}'
    second = '{"type":"assistant"}'
    path = tmp_path / "t.jsonl"
    path.write_text(f"{first}\n\n{second}\n", encoding="utf-8")
    assert read_lines(path) == [first, second]


def test_conversation_lines_keeps_only_user_and_assistant_lines_unmodified():
    user = '{"type":"user","message":{"role":"user","content":"fix  the test"}}'
    assistant = '{"type": "assistant", "message": {"content": [{"type": "tool_use"}]}}'
    tool_result = '{"type":"user","message":{"content":[{"type":"tool_result","content":"ok"}]}}'
    lines = [
        '{"type":"attachment","attachment":{"type":"skill_listing","content":"huge"}}',
        '{"type":"queue-operation","operation":"enqueue"}',
        user,
        '{"type":"user","isMeta":true,"message":{"content":"Base directory for this skill"}}',
        assistant,
        '{"type":"system","subtype":"turn_duration"}',
        tool_result,
        '{"type":"file-history-snapshot"}',
        '{"type":"assistant","message":{"content":"half-flushed li',
        '["not", "an", "object"]',
    ]
    assert conversation_lines(lines) == [user, assistant, tool_result]


def test_conversation_lines_of_nothing_is_nothing():
    assert conversation_lines([]) == []


def test_has_tool_result_finds_the_result_block_of_a_tool_use():
    use = '{"type":"assistant","message":{"content":[{"type":"tool_use","id":"t1","name":"Bash"}]}}'
    result = '{"type":"user","message":{"content":[{"type":"tool_result","tool_use_id":"t1"}]}}'
    assert not has_tool_result([use], "t1")  # the call alone is not the executed action
    assert has_tool_result([use, result], "t1")
    assert not has_tool_result([use, result], "t2")
    assert not has_tool_result(['{"type":"user","message":"t1 mentioned in text"}', "{bad"], "t1")
