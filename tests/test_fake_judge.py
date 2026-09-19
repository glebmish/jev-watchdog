import pytest

from jev_watchdog.judge.base import JudgeError, JudgeRequest
from jev_watchdog.judge.fake import FakeJudge
from jev_watchdog.pack import Question
from jev_watchdog.transcript import SurfaceKey

QUESTIONS = [
    Question("n", "noul", "i"),
    Question("s", "score", "i", criteria=["a", "b", "c"]),
    Question("c", "choice", "i", criteria={"x": "X", "y": "Y"}),
]


def request(lines: int = 2) -> JudgeRequest:
    return JudgeRequest(
        SurfaceKey("s", "main"), {"hook_event_name": "Stop"}, ["{}"] * lines, QUESTIONS
    )


async def test_answers_every_question_with_the_right_shape():
    verdict = await FakeJudge().judge(request())
    assert set(verdict.answers) == {"n", "s", "c"}
    assert 0.0 <= verdict.answers["n"].value <= 1.0
    assert 0.0 <= verdict.answers["s"].value <= 2.0
    assert verdict.answers["c"].value in {"x", "y"}
    assert verdict.judge == "fake"


async def test_is_deterministic_and_records_calls():
    judge = FakeJudge()
    first = await judge.judge(request(3))
    second = await judge.judge(request(3))
    assert first.answers == second.answers
    assert len(judge.calls) == 2


async def test_forced_failure():
    with pytest.raises(JudgeError) as err:
        await FakeJudge(fail_with="over_limit").judge(request())
    assert err.value.kind == "over_limit"


RULED = [
    Question("exfil", "noul", "i", quarantine_ref=0.45, quarantine_limit=0.2),
    Question("serves_goal", "noul", "i", flag_below=0.3),
    Question("drift", "score", "i", criteria=["a", "b", "c"]),
    Question("c", "choice", "i", criteria={"x": "X", "y": "Y"}, flag_choices=("y",)),
]


def tool_request(command: str, event: str = "PostToolUse") -> JudgeRequest:
    payload = {"hook_event_name": event, "tool_name": "Bash", "tool_input": {"command": command}}
    return JudgeRequest(SurfaceKey("s", "main"), payload, ["{}"], RULED)


async def test_marker_mode_is_calm_until_the_judged_tool_call_contains_the_marker():
    judge = FakeJudge(trip_on="canary", name="fake:canary")
    calm = await judge.judge(tool_request("ls -la"))
    assert {qid: answer.value for qid, answer in calm.answers.items()} == {
        "exfil": 0.0, "serves_goal": 1.0, "drift": 0.0, "c": "x",
    }  # fmt: skip
    assert not any(question.flags(calm.answers[question.id].value) for question in RULED)
    tripped = await judge.judge(tool_request("echo CANARY"))
    assert tripped.answers["exfil"].value == 1.0
    assert tripped.answers["serves_goal"].value == 1.0  # only quarantine rules react


async def test_marker_mode_via_the_registry():
    from jev_watchdog.judge.registry import JudgeConfig, make_judge

    judge = make_judge("fake:canary", JudgeConfig())
    assert judge.name == "fake:canary"
    assert (await judge.judge(tool_request("echo canary"))).answers["exfil"].value == 1.0
