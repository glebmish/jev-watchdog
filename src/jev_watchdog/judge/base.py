"""The judge boundary. Backends implement Judge; nothing else knows about them."""

from dataclasses import dataclass
from typing import Protocol

from jev_watchdog.pack import Question
from jev_watchdog.transcript import SurfaceKey

ERROR_KINDS = ("over_limit", "rate_limited", "auth", "timeout", "other")


@dataclass(frozen=True)
class Answer:
    value: float | str  # noul probability, score value, or chosen option
    confidence: float | None = None
    probabilities: dict[str, float] | None = None


@dataclass(frozen=True)
class Verdict:
    answers: dict[str, Answer]
    latency_ms: float
    input_tokens: int | None
    judge: str  # what actually answered, e.g. the versioned model id
    raw: dict | None = None
    cost_usd: float | None = None


@dataclass(frozen=True)
class JudgeRequest:
    surface: SurfaceKey
    event: dict
    transcript_lines: list[str]
    questions: list[Question]
    # What the human knows about the session and the agent does not, in their own words.
    context: str | None = None


class JudgeError(Exception):
    def __init__(self, kind: str, message: str) -> None:
        super().__init__(f"{kind}: {message}")
        self.kind = kind
        self.message = message


class Judge(Protocol):
    name: str

    async def judge(self, req: JudgeRequest) -> Verdict: ...

    async def aclose(self) -> None: ...


def request_state(req: JudgeRequest) -> list[str] | dict:
    """The transcript lines as they are; with a context, an object naming both parts.

    Without a context the state keeps its original shape, so the thresholds fitted on bare
    transcripts still apply.
    """
    if req.context is None:
        return req.transcript_lines
    return {"user_context": req.context, "transcript": req.transcript_lines}


def request_payload(req: JudgeRequest) -> dict:
    """A request as plain JSON, in the shape of Jev's HTTP body: state plus typed questions.

    Judges that take free-form input send exactly this, so every backend sees the same thing.
    """
    questions = {}
    for question in req.questions:
        questions[question.id] = {"type": question.kind, "instructions": question.instructions}
        if question.criteria is not None:
            questions[question.id]["criteria"] = question.criteria
    return {"state": request_state(req), "questions": questions}
