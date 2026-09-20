"""The decision layer: accumulate verdicts per agent thread and decide to quarantine it.

One CUSUM per question that opts in with quarantine_ref / quarantine_limit:
evidence = max(0, evidence + value - ref), tripping when evidence >= limit. A high spike
trips at once, moderate values have to persist, and clean actions drain the evidence.
Pure logic: nothing here performs I/O or knows what a quarantine does.
"""

from dataclasses import dataclass, field

from jev_watchdog.judge.base import Verdict
from jev_watchdog.pack import Question
from jev_watchdog.transcript import SurfaceKey

# Verdicts on other events re-judge the same last action and would count it twice.
TOOL_EVENTS = frozenset({"PostToolUse", "PostToolUseFailure", "PermissionDenied"})
EVIDENCE_DIGITS = 9


@dataclass(frozen=True)
class Trip:
    question: str
    value: float
    evidence: float
    limit: float

    def describe(self) -> str:
        return f"{self.question}={self.value:.2f} (evidence {self.evidence:.2f} ≥ {self.limit:.2f})"


@dataclass
class _State:
    evidence: dict[str, float] = field(default_factory=dict)
    folded: set[str] = field(default_factory=set)  # tool_use_ids
    trip: Trip | None = None


class Decider:
    def __init__(self, questions: list[Question]) -> None:
        self.rules = [q for q in questions if q.quarantine_limit is not None]
        self._states: dict[tuple[SurfaceKey, str], _State] = {}

    def fold(self, key: SurfaceKey, judge: str, event: dict, verdict: Verdict) -> Trip | None:
        """Fold one verdict in; return a Trip only on the call that trips."""
        if event.get("hook_event_name") not in TOOL_EVENTS:
            return None
        state = self._states.setdefault((key, judge), _State())
        tool_use_id = event.get("tool_use_id")
        if state.trip is not None or (tool_use_id and tool_use_id in state.folded):
            return None
        if tool_use_id:
            state.folded.add(tool_use_id)
        for rule in self.rules:
            answer = verdict.answers.get(rule.id)
            if answer is None or isinstance(answer.value, str):
                continue
            evidence = max(
                0.0, state.evidence.get(rule.id, 0.0) + answer.value - rule.quarantine_ref
            )
            # Judges that answer in tenths land exactly on a limit, and 0.7 - 0.6 twice is
            # 0.19999999999999996: without this, two 0.7s pass where one 0.8 trips.
            evidence = round(evidence, EVIDENCE_DIGITS)
            state.evidence[rule.id] = evidence
            if evidence >= rule.quarantine_limit and state.trip is None:
                state.trip = Trip(rule.id, answer.value, evidence, rule.quarantine_limit)
        return state.trip

    def tripped(self, key: SurfaceKey, judge: str) -> Trip | None:
        state = self._states.get((key, judge))
        return state.trip if state else None

    def evidence(self, key: SurfaceKey, judge: str) -> dict[str, float]:
        state = self._states.get((key, judge))
        return dict(state.evidence) if state else {}

    def reset(self, key: SurfaceKey) -> None:
        for state_key in [k for k in self._states if k[0] == key]:
            del self._states[state_key]
