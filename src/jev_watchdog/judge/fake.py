"""Deterministic judge for tests and offline runs.

--judge fake answers pseudo-randomly from the transcript length. --judge fake:WORD is the
marker mode: nothing is flagged until the judged tool call's input contains WORD, and then
every question with a quarantine rule answers 1.0 — an agent asked to run `echo WORD` trips
the rules on demand, which is what an end-to-end test of quarantine needs.
"""

import asyncio
import hashlib
import json

from jev_watchdog.core.pack import Question
from jev_watchdog.judge.base import Answer, JudgeError, JudgeRequest, Verdict


class FakeJudge:
    def __init__(
        self,
        latency_s: float = 0.0,
        fail_with: str | None = None,
        name: str = "fake",
        trip_on: str | None = None,
    ) -> None:
        self.name = name
        self.latency_s = latency_s
        self.fail_with = fail_with
        self.trip_on = trip_on
        self.calls: list[JudgeRequest] = []

    async def judge(self, req: JudgeRequest) -> Verdict:
        self.calls.append(req)
        if self.latency_s:
            await asyncio.sleep(self.latency_s)
        if self.fail_with:
            raise JudgeError(self.fail_with, "forced failure")
        lines = req.transcript_lines
        if self.trip_on is None:
            answers = {q.id: _answer(q, len(lines)) for q in req.questions}
        else:
            tool_input = json.dumps(req.event.get("tool_input"), ensure_ascii=False)
            marked = self.trip_on.lower() in tool_input.lower()
            answers = {q.id: _marker_answer(q, marked) for q in req.questions}
        return Verdict(
            answers=answers,
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


def _marker_answer(question: Question, marked: bool) -> Answer:
    """The answer that flags nothing, or 1.0 for a quarantine rule once the marker is seen."""
    if question.kind == "choice":
        calm = [option for option in question.criteria if option not in question.flag_choices]
        return Answer((calm or list(question.criteria))[0], confidence=1.0)
    if question.quarantine_limit is not None:
        top = 1.0 if question.kind == "noul" else float(len(question.criteria) - 1)
        return Answer(top if marked else 0.0)
    return Answer(1.0 if question.flag_below is not None else 0.0)
