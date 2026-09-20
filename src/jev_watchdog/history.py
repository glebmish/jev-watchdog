"""What the dashboard's charts are drawn from: the recent evidence of each agent thread.

`Decider` knows only the evidence of now. This keeps, per thread and judge, the last verdicts
with the evidence they left, and when the thread was quarantined, released or would have been
quarantined. Bounded, in memory, read-only for everything else: nothing here decides.
"""

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta

from jev_watchdog.pack import Question
from jev_watchdog.transcript import SurfaceKey

HISTORY = 500  # verdicts kept per thread and judge
MARKS = 100  # per thread


@dataclass(frozen=True)
class Point:
    ts: datetime
    evidence: dict[str, float]  # after this verdict, by rule
    flagged: int
    folded: bool  # a tool event: the only kind that moves the evidence (decide.TOOL_EVENTS)


@dataclass(frozen=True)
class Mark:
    ts: datetime
    kind: str  # "quarantine", "release", or "trip": a rule tripped without quarantining
    judge: str | None = None  # whose trip it was


class History:
    def __init__(self, rules: list[Question], size: int = HISTORY) -> None:
        self.limits = {rule.id: rule.quarantine_limit for rule in rules}
        self._size = size
        self._points: dict[SurfaceKey, dict[str, deque[Point]]] = {}
        self._marks: dict[SurfaceKey, deque[Mark]] = {}

    def record(
        self,
        key: SurfaceKey,
        judge: str,
        ts: datetime,
        evidence: dict[str, float],
        flagged: int,
        folded: bool,
    ) -> None:
        points = self._points.setdefault(key, {}).setdefault(judge, deque(maxlen=self._size))
        points.append(Point(ts, dict(evidence), flagged, folded))

    def mark(self, key: SurfaceKey, ts: datetime, kind: str, judge: str | None = None) -> None:
        self._marks.setdefault(key, deque(maxlen=MARKS)).append(Mark(ts, kind, judge))
        if kind == "release":  # Decider.reset: every judge's evidence starts over
            for points in self._points.get(key, {}).values():
                points.append(Point(ts, {}, 0, True))

    def series(self, key: SurfaceKey) -> dict:
        """One thread, every judge: what the evidence chart draws."""
        return {
            "limits": dict(self.limits),
            "judges": {
                judge: [_point(point) for point in points]
                for judge, points in self._points.get(key, {}).items()
            },
            "marks": [
                {"ts": _iso(mark.ts), "kind": mark.kind, "judge": mark.judge}
                for mark in self._marks.get(key, ())
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

        A cell is the worst share of a limit that any rule's evidence reached in it (1.0 is
        the limit), None where nothing was judged.
        """
        start = now - timedelta(minutes=minutes)
        width = (now - start) / buckets

        def bucket(ts: datetime) -> int | None:
            return min(int((ts - start) / width), buckets - 1) if start <= ts <= now else None

        rows = []
        for key, label in threads:
            cells: list[float | None] = [None] * buckets
            for point in self._points.get(key, {}).get(judge, ()):
                index = bucket(point.ts)
                if index is not None:
                    # In order: an empty evidence is a release, and replaces what it reset.
                    worst = max(self._share(point), cells[index] or 0.0)
                    cells[index] = worst if point.evidence else 0.0
            marks = {}
            for mark in self._marks.get(key, ()):
                index = bucket(mark.ts)
                if index is not None and mark.judge in (None, judge):
                    marks[str(index)] = mark.kind
            if marks or any(cell is not None for cell in cells):
                rows.append(
                    {"label": label, "session_id": key.session_id, "agent_id": key.agent_id,
                     "cells": cells, "marks": marks}
                )  # fmt: skip
        return {
            "start": _iso(start),
            "end": _iso(now),
            "bucket_s": int(width.total_seconds()),
            "judge": judge,
            "threads": rows,
        }

    def _share(self, point: Point) -> float:
        shares = [value / self.limits[rule] for rule, value in point.evidence.items()
                  if self.limits.get(rule)]  # fmt: skip
        return round(max(shares, default=0.0), 6)


def _point(point: Point) -> dict:
    return {
        "ts": _iso(point.ts),
        "evidence": point.evidence,
        "flagged": point.flagged,
        "folded": point.folded,
    }


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")
