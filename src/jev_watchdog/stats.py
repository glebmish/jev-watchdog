"""Read-only accumulators. Nothing here triggers an action."""

import math
from collections import Counter
from dataclasses import dataclass, field

from jev_watchdog.judge.base import Verdict
from jev_watchdog.pack import Question

EWMA_ALPHA = 0.3


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
class JudgeSurfaceStats:
    """One judge's view of one surface."""

    judgments: int = 0
    errors: Counter[str] = field(default_factory=Counter)
    questions: dict[str, NumericStat | ChoiceStat] = field(default_factory=dict)

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
class SurfaceStats:
    events: Counter[str] = field(default_factory=Counter)
    errors: Counter[str] = field(default_factory=Counter)  # not tied to a judge, e.g. transcript
    judges: dict[str, JudgeSurfaceStats] = field(default_factory=dict)

    def record_event(self, name: str) -> None:
        self.events[name] += 1

    def record_error(self, kind: str) -> None:
        self.errors[kind] += 1

    def judge(self, name: str) -> JudgeSurfaceStats:
        return self.judges.setdefault(name, JudgeSurfaceStats())

    @property
    def judgments(self) -> int:
        return sum(judge.judgments for judge in self.judges.values())


@dataclass
class JudgeStats:
    """One judge across all surfaces: what comparing backends is about."""

    judgments: int = 0
    errors: Counter[str] = field(default_factory=Counter)
    latencies_ms: list[float] = field(default_factory=list)
    # Lag is hook received -> verdict ready: latency plus time spent queued behind
    # earlier events of the same surface. It grows when a judge cannot keep up.
    lags_ms: list[float] = field(default_factory=list)
    input_tokens: int = 0
    cost_usd: float = 0.0

    def record_error(self, kind: str) -> None:
        self.errors[kind] += 1

    def record_verdict(self, verdict: Verdict, lag_ms: float) -> None:
        self.judgments += 1
        self.latencies_ms.append(verdict.latency_ms)
        self.lags_ms.append(lag_ms)
        self.input_tokens += verdict.input_tokens or 0
        self.cost_usd += verdict.cost_usd or 0.0

    def latency(self, p: float) -> float | None:
        return percentile(self.latencies_ms, p)

    def lag(self, p: float) -> float | None:
        return percentile(self.lags_ms, p)

    @property
    def mean_latency(self) -> float | None:
        return sum(self.latencies_ms) / len(self.latencies_ms) if self.latencies_ms else None


@dataclass
class GlobalStats:
    surfaces: int = 0
    events: Counter[str] = field(default_factory=Counter)
    errors: Counter[str] = field(default_factory=Counter)  # not tied to a judge
    judges: dict[str, JudgeStats] = field(default_factory=dict)

    def record_event(self, name: str) -> None:
        self.events[name] += 1

    def record_error(self, kind: str) -> None:
        self.errors[kind] += 1

    def judge(self, name: str) -> JudgeStats:
        return self.judges.setdefault(name, JudgeStats())

    @property
    def judgments(self) -> int:
        return sum(judge.judgments for judge in self.judges.values())

    @property
    def cost_usd(self) -> float:
        return sum(judge.cost_usd for judge in self.judges.values())


def percentile(values: list[float], p: float) -> float | None:
    """Nearest-rank percentile."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(math.ceil(p / 100 * len(ordered)), 1) - 1]
