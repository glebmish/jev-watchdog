"""What the dashboard's charts are drawn from: the recent evidence of each agent thread.

`Decider` knows only the evidence of now. This keeps, per thread and judge, the last verdicts
with the evidence they left, as a share of each rule's limit (1.0 is where it quarantines), and
when the thread was quarantined, released or would have been quarantined. Bounded, in memory,
read-only for everything else: nothing here decides.
"""

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from jev_watchdog.core.pack import Question
from jev_watchdog.core.transcript import SurfaceKey

HISTORY = 500  # verdicts kept per thread and judge
MARKS = 100  # per thread


@dataclass(frozen=True)
class Point:
    ts: datetime
    shares: dict[str, float]  # evidence / limit after this verdict, by rule
    folded: bool  # a tool event: the only kind that moves the evidence (decide.TOOL_EVENTS)


@dataclass(frozen=True)
class Mark:
    ts: datetime
    kind: str  # "quarantine", "release", or "trip": a rule tripped without quarantining
    judge: str | None = None  # whose trip it was


@dataclass
class _Thread:
    points: dict[str, deque[Point]] = field(default_factory=dict)  # by judge
    marks: deque[Mark] = field(default_factory=lambda: deque(maxlen=MARKS))


class History:
    def __init__(self, rules: list[Question], size: int = HISTORY) -> None:
        self._limits = {rule.id: rule.quarantine_limit for rule in rules}
        self._size = size
        self._threads: dict[SurfaceKey, _Thread] = {}

    def record(
        self, key: SurfaceKey, judge: str, ts: datetime, evidence: dict[str, float], folded: bool
    ) -> None:
        shares = {
            rule: round(evidence.get(rule, 0.0) / limit, 6) for rule, limit in self._limits.items()
        }
        points = self._thread(key).points.setdefault(judge, deque(maxlen=self._size))
        points.append(Point(ts, shares, folded))

    def mark(self, key: SurfaceKey, ts: datetime, kind: str, judge: str | None = None) -> None:
        thread = self._thread(key)
        thread.marks.append(Mark(ts, kind, judge))
        if kind == "release":  # Decider.reset: every judge's evidence starts over
            for name in thread.points:
                self.record(key, name, ts, {}, folded=True)

    def series(self, key: SurfaceKey) -> dict:
        """One thread, every judge: what the evidence chart draws."""
        thread = self._threads.get(key) or _Thread()
        return {
            "judges": {
                judge: [{"ts": _iso(p.ts), "shares": p.shares, "folded": p.folded} for p in points]
                for judge, points in thread.points.items()
            },
            "marks": [
                {"ts": _iso(mark.ts), "kind": mark.kind, "judge": mark.judge}
                for mark in thread.marks
            ],
        }

    def timeline(
        self,
        threads: list[tuple[SurfaceKey, str]],
        judge: str,
        now: datetime,
        minutes: int,
        buckets: int,
    ) -> dict:
        """The last `minutes` of every thread in `buckets` cells, as one judge saw them.

        A cell is the worst share of a limit that any rule reached in it, None where nothing
        was judged.
        """
        start = now - timedelta(minutes=minutes)
        width = (now - start) / buckets

        def bucket(ts: datetime) -> int | None:
            return min(int((ts - start) / width), buckets - 1) if start <= ts <= now else None

        rows = []
        for key, label in threads:
            thread = self._threads.get(key) or _Thread()
            cells: list[float | None] = [None] * buckets
            for point in thread.points.get(judge, ()):
                if (index := bucket(point.ts)) is not None:
                    cells[index] = max([cells[index] or 0.0, *point.shares.values()])
            marks = {
                str(index): mark.kind
                for mark in thread.marks
                if mark.judge in (None, judge) and (index := bucket(mark.ts)) is not None
            }
            if marks or any(cell is not None for cell in cells):
                rows.append(
                    {"label": label, "session_id": key.session_id, "agent_id": key.agent_id,
                     "cells": cells, "marks": marks}
                )  # fmt: skip
        return {
            "start": _iso(start),
            "end": _iso(now),
            "bucket_s": round(width.total_seconds(), 1),
            "judge": judge,
            "threads": rows,
        }

    def forget(self, key: SurfaceKey) -> None:
        self._threads.pop(key, None)

    def _thread(self, key: SurfaceKey) -> _Thread:
        return self._threads.setdefault(key, _Thread())


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")
