"""Jev (TypeSafe System One) judge. The only module that imports typesafe_sdk."""

import time

import typesafe_sdk as ts

from jev_watchdog.judge.base import Answer, JudgeError, JudgeRequest, Verdict
from jev_watchdog.pack import Question


class JevJudge:
    name = "jev"

    def __init__(
        self,
        api_key: str | None = None,
        model: str = "jev-latest",
        timeout_s: float = 30.0,
        client: ts.AsyncTypeSafeClient | None = None,
    ) -> None:
        self._client = client or ts.AsyncTypeSafeClient(
            api_key=api_key, model=model, timeout=timeout_s
        )

    async def judge(self, req: JudgeRequest) -> Verdict:
        questions = {q.id: _to_sdk(q) for q in req.questions}
        started = time.perf_counter()
        try:
            response = await self._client.system_one(
                state=req.transcript_lines, questions=questions
            )
        except ts.TypeSafeError as exc:
            kind = _error_kind(exc)
            message = str(exc)
            if kind == "over_limit":
                sent = len(req.transcript_lines)
                message = f"transcript exceeds Jev's 32k-token state limit ({sent} lines sent)"
            raise JudgeError(kind, message) from exc
        latency_ms = (time.perf_counter() - started) * 1000
        raw = response.model_dump()
        return Verdict(
            answers={qid: _to_answer(answer) for qid, answer in raw["answers"].items()},
            latency_ms=latency_ms,
            input_tokens=(raw.get("usage") or {}).get("input_tokens"),
            judge=raw.get("model") or self.name,
            raw=raw,
        )

    async def aclose(self) -> None:
        await self._client.aclose()


def _to_sdk(question: Question) -> ts.Noul | ts.Score | ts.Choice:
    if question.kind == "noul":
        return ts.Noul(instructions=question.instructions)
    if question.kind == "score":
        return ts.Score(instructions=question.instructions, criteria=list(question.criteria))
    return ts.Choice(instructions=question.instructions, criteria=dict(question.criteria))


def _to_answer(answer: dict) -> Answer:
    if answer["type"] == "noul":
        return Answer(answer["noul"])
    probabilities = {str(key): value for key, value in (answer.get("probabilities") or {}).items()}
    return Answer(answer[answer["type"]], answer.get("confidence"), probabilities or None)


def _error_kind(exc: ts.TypeSafeError) -> str:
    if isinstance(exc, ts.TypeSafeAPITimeoutError):
        return "timeout"
    if isinstance(exc, ts.TypeSafeRateLimitError):
        return "rate_limited"
    if isinstance(exc, ts.TypeSafeAuthenticationError | ts.TypeSafePermissionDeniedError):
        return "auth"
    if "max_tokens_exceeded" in f"{exc} {getattr(exc, 'body', '')}":
        return "over_limit"
    return "other"
