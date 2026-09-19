"""Read-only accumulators. Nothing here triggers an action."""

import math
from collections import Counter
from dataclasses import dataclass, field

from jev_watchdog.judge.base import Verdict
from jev_watchdog.pack import Question

EWMA_ALPHA = 0.3
PRICE_PER_MTOK_USD = 0.042


@dataclass
class NumericStat:
    n: int = 0
    last: float | None = None
    mean: float = 0.0
    min: float | None = None
    max: float | None = None
    ewma: float | None = None
    streak: int = 0
    longest_streak: int = 0

    def add(self, value: float, flagged: bool) -> None:
        self.n += 1
        self.last = value
        self.mean += (value - self.mean) / self.n
        self.min = value if self.min is None else min(self.min, value)
        self.max = value if self.max is None else max(self.max, value)
        self.ewma = (
            value if self.ewma is None else EWMA_ALPHA * value + (1 - EWMA_ALPHA) * self.ewma
        )
        _bump_streak(self, flagged)


@dataclass
class ChoiceStat:
    counts: Counter[str] = field(default_factory=Counter)
    last: str | None = None
    streak: int = 0
    longest_streak: int = 0

    def add(self, value: str, flagged: bool) -> None:
        self.counts[value] += 1
        self.last = value
        _bump_streak(self, flagged)


def _bump_streak(stat: NumericStat | ChoiceStat, flagged: bool) -> None:
    stat.streak = stat.streak + 1 if flagged else 0
    stat.longest_streak = max(stat.longest_streak, stat.streak)


@dataclass
class SurfaceStats:
    events: Counter[str] = field(default_factory=Counter)
    judgments: int = 0
    errors: Counter[str] = field(default_factory=Counter)
    questions: dict[str, NumericStat | ChoiceStat] = field(default_factory=dict)

    def record_event(self, name: str) -> None:
        self.events[name] += 1

    def record_error(self, kind: str) -> None:
        self.errors[kind] += 1

    def record_verdict(self, questions: list[Question], verdict: Verdict) -> set[str]:
        """Fold a verdict into the accumulators; return the ids of flagged questions."""
        self.judgments += 1
        flagged: set[str] = set()
        for question in questions:
            answer = verdict.answers.get(question.id)
            if answer is None:
                continue
            is_flagged = question.flags(answer.value)
            if is_flagged:
                flagged.add(question.id)
            default = ChoiceStat() if question.kind == "choice" else NumericStat()
            self.questions.setdefault(question.id, default).add(answer.value, is_flagged)
        return flagged


@dataclass
class GlobalStats:
    surfaces: int = 0
    events: Counter[str] = field(default_factory=Counter)
    judgments: int = 0
    errors: Counter[str] = field(default_factory=Counter)
    latencies_ms: list[float] = field(default_factory=list)
    input_tokens: int = 0

    def record_event(self, name: str) -> None:
        self.events[name] += 1

    def record_error(self, kind: str) -> None:
        self.errors[kind] += 1

    def record_verdict(self, verdict: Verdict) -> None:
        self.judgments += 1
        self.latencies_ms.append(verdict.latency_ms)
        self.input_tokens += verdict.input_tokens or 0

    def latency_percentile(self, p: float) -> float | None:
        """Nearest-rank percentile of judge latency in ms."""
        if not self.latencies_ms:
            return None
        ordered = sorted(self.latencies_ms)
        return ordered[max(math.ceil(p / 100 * len(ordered)), 1) - 1]

    @property
    def cost_usd(self) -> float:
        return self.input_tokens / 1_000_000 * PRICE_PER_MTOK_USD
