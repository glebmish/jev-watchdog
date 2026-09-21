"""Judge-agnostic typed questions and the TOML pack loader."""

import tomllib
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

KINDS = ("noul", "score", "choice")


class PackError(ValueError):
    pass


@dataclass(frozen=True)
class Question:
    id: str
    kind: Literal["noul", "score", "choice"]
    instructions: str
    criteria: dict[str, str] | list[str] | None = None
    flag_threshold: float | None = None
    flag_below: float | None = None
    flag_choices: tuple[str, ...] = ()
    # Quarantine rule (decide.py): evidence += value - ref, quarantine at evidence >= limit.
    quarantine_ref: float | None = None
    quarantine_limit: float | None = None
    # Asked only when the human gave the session a context. A question about the context
    # still gets a confident answer when there is none (against_context scored 0.8 on drift).
    needs_context: bool = False
    # The wording for a session with a context. Without one the plain wording is asked, so
    # such requests are what they were before contexts existed, and text in the transcript
    # that poses as user_context is not what the question asks about (a spoofed line raised
    # serves_goal from 0.41 to 0.70 when the plain wording named user_context).
    context_instructions: str | None = None

    def asked(self, has_context: bool) -> Question | None:
        """The question as put to a judge, or None when it is not asked."""
        if not has_context:
            return None if self.needs_context else self
        if self.context_instructions is None:
            return self
        return replace(self, instructions=self.context_instructions)

    def flags(self, value: float | str) -> bool:
        if isinstance(value, str):
            return value in self.flag_choices
        if self.flag_threshold is not None:
            return value >= self.flag_threshold
        if self.flag_below is not None:
            return value <= self.flag_below
        return False


def load_pack(path: Path) -> list[Question]:
    with path.open("rb") as fh:
        data = tomllib.load(fh)
    tables = data.get("questions") or {}
    if not tables:
        raise PackError(f"{path}: no questions defined")
    return [_question(qid, table) for qid, table in tables.items()]


def load_packs(paths: list[Path]) -> list[Question]:
    """Several packs as one, in order; a question id may be defined only once."""
    questions: dict[str, Question] = {}
    for path in paths:
        for question in load_pack(path):
            if question.id in questions:
                raise PackError(f"{path}: question {question.id!r} is defined in both packs")
            questions[question.id] = question
    return list(questions.values())


def _question(qid: str, table: dict) -> Question:
    kind = table.get("kind")
    if kind not in KINDS:
        raise PackError(f"{qid}: kind must be one of {KINDS}, got {kind!r}")
    instructions = table.get("instructions")
    if not isinstance(instructions, str) or not instructions.strip():
        raise PackError(f"{qid}: instructions must be a non-empty string")

    criteria = table.get("criteria")
    if kind == "noul" and criteria is not None:
        raise PackError(f"{qid}: noul questions take no criteria")
    if kind == "score" and not (isinstance(criteria, list) and len(criteria) >= 2):
        raise PackError(f"{qid}: score criteria must be a list of at least 2 levels")
    if kind == "choice" and not (isinstance(criteria, dict) and len(criteria) >= 2):
        raise PackError(f"{qid}: choice criteria must be a table of at least 2 options")

    flag_threshold = table.get("flag_threshold")
    flag_below = table.get("flag_below")
    if flag_threshold is not None and flag_below is not None:
        raise PackError(f"{qid}: set at most one of flag_threshold / flag_below")
    if not all(value is None or _is_number(value) for value in (flag_threshold, flag_below)):
        raise PackError(f"{qid}: flag_threshold / flag_below must be a number")

    flag_choices = tuple(table.get("flag_choices", ()))
    if flag_choices and (kind != "choice" or not set(flag_choices) <= set(criteria)):
        raise PackError(f"{qid}: flag_choices must be a subset of the choice's options")

    quarantine_ref = table.get("quarantine_ref")
    quarantine_limit = table.get("quarantine_limit")
    if (quarantine_ref is None) != (quarantine_limit is None):
        raise PackError(f"{qid}: set both quarantine_ref and quarantine_limit, or neither")
    if quarantine_limit is not None:
        if not all(_is_number(value) for value in (quarantine_ref, quarantine_limit)):
            raise PackError(f"{qid}: quarantine_ref and quarantine_limit must be numbers")
        if quarantine_limit <= 0:
            raise PackError(f"{qid}: quarantine_limit must be positive")
        if kind == "choice" or flag_below is not None:
            raise PackError(f"{qid}: quarantine rules need a higher-is-worse noul or score")

    context_instructions = table.get("context_instructions")
    if context_instructions is not None and not (
        isinstance(context_instructions, str) and context_instructions.strip()
    ):
        raise PackError(f"{qid}: context_instructions must be a non-empty string")

    needs_context = table.get("needs_context", False)
    if not isinstance(needs_context, bool):
        raise PackError(f"{qid}: needs_context must be true or false")

    return Question(
        id=qid,
        kind=kind,
        instructions=instructions,
        criteria=criteria,
        flag_threshold=flag_threshold,
        flag_below=flag_below,
        flag_choices=flag_choices,
        quarantine_ref=quarantine_ref,
        quarantine_limit=quarantine_limit,
        needs_context=needs_context,
        context_instructions=context_instructions,
    )


def _is_number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)
