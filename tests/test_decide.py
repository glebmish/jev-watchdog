from jev_watchdog.decide import Decider
from jev_watchdog.judge.base import Answer, Verdict
from jev_watchdog.pack import Question
from jev_watchdog.transcript import SurfaceKey

KEY = SurfaceKey("s1", "main")
QUESTIONS = [
    Question("exfil", "noul", "i", quarantine_ref=0.45, quarantine_limit=0.2),
    Question("serves_goal", "noul", "i", flag_below=0.3),
]


def verdict(**values) -> Verdict:
    answers = {qid: Answer(value) for qid, value in values.items()}
    return Verdict(answers, latency_ms=1, input_tokens=1, judge="t")


def tool(tool_use_id=None, event="PostToolUse") -> dict:
    return {"hook_event_name": event, "tool_use_id": tool_use_id}


def test_a_high_spike_trips_at_once():
    decider = Decider(QUESTIONS)
    trip = decider.fold(KEY, "jev", tool("t1"), verdict(exfil=0.93))
    assert (trip.question, trip.value, trip.limit) == ("exfil", 0.93, 0.2)
    assert round(trip.evidence, 2) == 0.48
    assert trip.describe() == "exfil=0.93 (evidence 0.48 ≥ 0.20)"
    assert decider.tripped(KEY, "jev") == trip


def test_moderate_evidence_accumulates_over_actions():
    decider = Decider(QUESTIONS)
    assert decider.fold(KEY, "jev", tool("t1"), verdict(exfil=0.57)) is None
    trip = decider.fold(KEY, "jev", tool("t2"), verdict(exfil=0.57))
    assert round(trip.evidence, 2) == 0.24


def test_clean_actions_drain_evidence_but_not_below_zero():
    decider = Decider(QUESTIONS)
    decider.fold(KEY, "jev", tool("t1"), verdict(exfil=0.6))
    decider.fold(KEY, "jev", tool("t2"), verdict(exfil=0.02))
    assert decider.evidence(KEY, "jev") == {"exfil": 0.0}
    assert decider.fold(KEY, "jev", tool("t3"), verdict(exfil=0.6)) is None


def test_an_action_is_folded_once():
    decider = Decider(QUESTIONS)
    assert decider.fold(KEY, "jev", tool("t1"), verdict(exfil=0.57)) is None
    assert decider.fold(KEY, "jev", tool("t1"), verdict(exfil=0.57)) is None
    assert round(decider.evidence(KEY, "jev")["exfil"], 2) == 0.12


def test_events_without_an_action_are_ignored():
    decider = Decider(QUESTIONS)
    for event in ("Stop", "SubagentStop", "UserPromptSubmit"):
        assert decider.fold(KEY, "jev", tool(event=event), verdict(exfil=0.99)) is None
    assert decider.evidence(KEY, "jev") == {}


def test_tool_events_without_an_id_are_all_folded():
    decider = Decider(QUESTIONS)
    decider.fold(KEY, "jev", tool(), verdict(exfil=0.57))
    assert decider.fold(KEY, "jev", tool(), verdict(exfil=0.57)) is not None


def test_state_is_per_surface_and_judge_and_trips_once():
    decider = Decider(QUESTIONS)
    assert decider.fold(KEY, "jev", tool("t1"), verdict(exfil=0.93)) is not None
    assert decider.fold(KEY, "jev", tool("t2"), verdict(exfil=0.93)) is None  # already tripped
    assert decider.tripped(KEY, "claude") is None
    assert decider.tripped(SurfaceKey("s1", "abc"), "jev") is None


def test_reset_clears_every_judge_of_the_surface():
    decider = Decider(QUESTIONS)
    decider.fold(KEY, "jev", tool("t1"), verdict(exfil=0.93))
    decider.fold(KEY, "claude", tool("t1"), verdict(exfil=1.0))
    decider.reset(KEY)
    assert decider.tripped(KEY, "jev") is None and decider.tripped(KEY, "claude") is None
    assert decider.fold(KEY, "jev", tool("t1"), verdict(exfil=0.93)) is not None


def test_missing_and_unruled_answers_are_skipped():
    decider = Decider(QUESTIONS)
    assert decider.fold(KEY, "jev", tool("t1"), verdict(serves_goal=0.99)) is None
    assert decider.evidence(KEY, "jev") == {}


def test_evidence_is_compared_without_float_residue():
    """0.7 - 0.6 twice is 0.19999999999999996: two 0.7s must trip exactly like one 0.8."""
    rule = Question("exfil", "noul", "i", quarantine_ref=0.6, quarantine_limit=0.2)
    decider = Decider([rule])
    assert decider.fold(KEY, "j", tool("t1"), verdict(exfil=0.7)) is None
    trip = decider.fold(KEY, "j", tool("t2"), verdict(exfil=0.7))
    assert trip is not None and trip.evidence == 0.2
    assert decider.evidence(KEY, "j") == {"exfil": 0.2}
