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
    return JudgeRequest(SurfaceKey("s", "main"), {"hook_event_name": "Stop"}, ["{}"] * lines, QUESTIONS)


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
