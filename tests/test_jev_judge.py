import httpx2
import pytest
import typesafe_sdk as ts

from jev_watchdog.judge.base import JudgeError, JudgeRequest
from jev_watchdog.judge.jev import JevJudge
from jev_watchdog.judge.registry import JUDGES, JudgeConfig, make_judge
from jev_watchdog.pack import Question
from jev_watchdog.transcript import SurfaceKey

QUESTIONS = [
    Question("exfil", "noul", "sends data out"),
    Question("drift", "score", "distance", criteria=["on task", "off task"]),
    Question(
        "activity", "choice", "doing what", criteria={"exploring": "reading", "stuck": "looping"}
    ),
]
LINES = ['{"type":"user"}', '{"type":"assistant"}']
RAW = {
    "model": "jev-1.13.0",
    "usage": {"input_tokens": 400, "output_tokens": 67},
    "answers": {
        "exfil": {"type": "noul", "noul": 0.07},
        "drift": {"type": "score", "score": 0.42, "confidence": 0.36,
                  "legend": {0: "on task", 1: "off task"}, "probabilities": {0: 0.6, 1: 0.4}},
        "activity": {"type": "choice", "choice": "exploring", "confidence": 0.49,
                     "probabilities": {"exploring": 0.74, "stuck": 0.26}},
    },
}  # fmt: skip


class FakeResponse:
    def model_dump(self):
        return RAW


class FakeClient:
    def __init__(self, error: Exception | None = None):
        self.error = error
        self.calls: list[dict] = []
        self.closed = False

    async def system_one(self, *, state, questions):
        self.calls.append({"state": state, "questions": questions})
        if self.error:
            raise self.error
        return FakeResponse()

    async def aclose(self):
        self.closed = True


def request() -> JudgeRequest:
    return JudgeRequest(SurfaceKey("s", "main"), {"hook_event_name": "Stop"}, LINES, QUESTIONS)


async def test_sends_raw_lines_and_maps_questions():
    client = FakeClient()
    await JevJudge(client=client).judge(request())
    call = client.calls[0]
    assert call["state"] == LINES
    assert isinstance(call["questions"]["exfil"], ts.Noul)
    assert call["questions"]["exfil"].instructions == "sends data out"
    assert isinstance(call["questions"]["drift"], ts.Score)
    assert list(call["questions"]["drift"].criteria) == ["on task", "off task"]
    assert isinstance(call["questions"]["activity"], ts.Choice)
    assert dict(call["questions"]["activity"].criteria) == {
        "exploring": "reading",
        "stuck": "looping",
    }


async def test_maps_response_to_verdict():
    verdict = await JevJudge(client=FakeClient()).judge(request())
    assert verdict.judge == "jev-1.13.0" and verdict.input_tokens == 400
    assert verdict.latency_ms >= 0 and verdict.raw == RAW
    assert verdict.answers["exfil"].value == 0.07 and verdict.answers["exfil"].confidence is None
    assert verdict.answers["drift"].value == 0.42
    assert verdict.answers["drift"].probabilities == {"0": 0.6, "1": 0.4}
    assert verdict.answers["activity"].value == "exploring"
    assert verdict.answers["activity"].confidence == 0.49


def status_error(cls, status: int, text: str):
    return cls(status, {"detail": text}, httpx2.Headers(), f"POST /v1/systemone: {status} {text}")


@pytest.mark.parametrize(
    "error, kind",
    [
        (
            status_error(ts.TypeSafeBadRequestError, 400, '{"error_type":"max_tokens_exceeded"}'),
            "over_limit",
        ),
        (status_error(ts.TypeSafeBadRequestError, 400, "something else"), "other"),
        (status_error(ts.TypeSafeRateLimitError, 429, "slow down"), "rate_limited"),
        (status_error(ts.TypeSafeAuthenticationError, 401, "bad key"), "auth"),
        (status_error(ts.TypeSafePermissionDeniedError, 403, "no"), "auth"),
        (ts.TypeSafeAPITimeoutError(30.0), "timeout"),
        (ts.TypeSafeAPIConnectionError("refused"), "other"),
    ],
)
async def test_maps_sdk_errors_to_judge_errors(error, kind):
    with pytest.raises(JudgeError) as err:
        await JevJudge(client=FakeClient(error)).judge(request())
    assert err.value.kind == kind


async def test_aclose_closes_the_client():
    client = FakeClient()
    await JevJudge(client=client).aclose()
    assert client.closed


def test_registry_builds_judges_from_specs():
    assert set(JUDGES) == {"jev", "claude", "codex", "fake"}
    assert make_judge("fake", JudgeConfig()).name == "fake"
    assert make_judge("jev", JudgeConfig(api_key="apikey_test")).name == "jev"
    assert (
        make_judge("jev:jev-preview", JudgeConfig(api_key="apikey_test")).name == "jev:jev-preview"
    )
    assert make_judge("claude", JudgeConfig()).name == "claude:claude-opus-5"
    assert make_judge("claude:claude-haiku-4-5", JudgeConfig()).name == "claude:claude-haiku-4-5"
    with pytest.raises(ValueError, match="unknown judge"):
        make_judge("nope", JudgeConfig())
    with pytest.raises(ValueError, match="unknown judge"):
        make_judge("nope:model", JudgeConfig())


async def test_verdict_carries_the_cost_of_the_call():
    verdict = await JevJudge(client=FakeClient()).judge(request())
    assert verdict.cost_usd == pytest.approx(400 / 1_000_000 * 0.042)


async def test_over_limit_message_is_concise():
    error = status_error(ts.TypeSafeBadRequestError, 400, '{"error_type":"max_tokens_exceeded"}')
    with pytest.raises(JudgeError) as err:
        await JevJudge(client=FakeClient(error)).judge(request())
    assert err.value.message == "transcript exceeds Jev's 32k-token state limit (2 lines sent)"


async def test_context_travels_in_the_state_next_to_the_unmodified_lines():
    client = FakeClient()
    req = JudgeRequest(
        SurfaceKey("s", "main"), {"hook_event_name": "Stop"}, LINES, QUESTIONS, "prod is fine"
    )
    await JevJudge(client=client).judge(req)
    assert client.calls[0]["state"] == {"user_context": "prod is fine", "transcript": LINES}
