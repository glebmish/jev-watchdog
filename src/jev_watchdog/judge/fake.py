"""Deterministic judge for tests and offline runs (--judge fake)."""

import asyncio
import hashlib

from jev_watchdog.judge.base import Answer, JudgeError, JudgeRequest, Verdict
from jev_watchdog.pack import Question


class FakeJudge:
    name = "fake"

    def __init__(self, latency_s: float = 0.0, fail_with: str | None = None) -> None:
        self.latency_s = latency_s
        self.fail_with = fail_with
        self.calls: list[JudgeRequest] = []

    async def judge(self, req: JudgeRequest) -> Verdict:
        self.calls.append(req)
        if self.latency_s:
            await asyncio.sleep(self.latency_s)
        if self.fail_with:
            raise JudgeError(self.fail_with, "forced failure")
        lines = req.transcript_lines
        return Verdict(
            answers={q.id: _answer(q, len(lines)) for q in req.questions},
            latency_ms=self.latency_s * 1000,
            input_tokens=sum(len(line) for line in lines) // 4,
            judge=self.name,
        )

    async def aclose(self) -> None:
        pass


def _unit(seed: str) -> float:
    return int(hashlib.sha256(seed.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF


def _answer(question: Question, n_lines: int) -> Answer:
    u = _unit(f"{question.id}:{n_lines}")
    if question.kind == "noul":
        return Answer(round(u, 2))
    if question.kind == "score":
        return Answer(round(u * (len(question.criteria) - 1), 2), confidence=0.5)
    options = list(question.criteria)
    return Answer(options[min(int(u * len(options)), len(options) - 1)], confidence=0.5)
