from datetime import datetime

from jev_watchdog.core.quarantine import Quarantine, Quarantines
from jev_watchdog.core.transcript import MAIN, SurfaceKey

MAIN_KEY, SUB, OTHER_SUB = SurfaceKey("s1", MAIN), SurfaceKey("s1", "abc"), SurfaceKey("s1", "xyz")
AT = datetime(2026, 9, 19, 12, 0, 0)


def q(key, label="s1/main") -> Quarantine:
    return Quarantine(key, label, "exfil=0.93 (evidence 0.48 ≥ 0.20)", "rule:jev", AT)


def test_a_quarantined_subagent_blocks_only_itself():
    book = Quarantines()
    assert book.add(q(SUB, "s1/abc"))
    assert book.blocking(SUB).key == SUB
    assert book.blocking(MAIN_KEY) is None and book.blocking(OTHER_SUB) is None


def test_a_quarantined_main_thread_blocks_the_whole_session():
    book = Quarantines()
    book.add(q(MAIN_KEY))
    assert book.blocking(SUB).key == MAIN_KEY
    assert book.blocking(SurfaceKey("s2", MAIN)) is None


def test_add_is_idempotent_and_release_returns_the_entry():
    book = Quarantines()
    assert book.add(q(SUB)) and not book.add(q(SUB))
    assert book.release(SUB).key == SUB
    assert book.release(SUB) is None and book.all() == []


def test_deny_body_tells_the_agent_to_stop():
    body = q(SUB, "s1/abc").deny_body()["hookSpecificOutput"]
    assert body["hookEventName"] == "PreToolUse" and body["permissionDecision"] == "deny"
    reason = body["permissionDecisionReason"]
    assert "exfil=0.93" in reason and "jev-watchdog release s1/abc" in reason
    assert "Do not retry" in reason


def test_as_dict():
    assert q(SUB, "s1/abc").as_dict() == {
        "target": "s1/abc", "session_id": "s1", "agent_id": "abc",
        "reason": "exfil=0.93 (evidence 0.48 ≥ 0.20)", "source": "rule:jev",
        "at": "2026-09-19T12:00:00",
    }  # fmt: skip
