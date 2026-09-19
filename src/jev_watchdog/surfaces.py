"""Surfaces: one per agent thread, each with its own queue, worker and statistics.

The registry is also where verdicts become decisions: every verdict is folded into the
decider, the deciding judge's trips quarantine the thread, and PreToolUse hooks of a
quarantined thread are answered with a deny.
"""

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from jev_watchdog.decide import Decider, Trip
from jev_watchdog.judge.base import Judge, JudgeError, JudgeRequest, Verdict
from jev_watchdog.pack import Question
from jev_watchdog.printer import Printer
from jev_watchdog.quarantine import Quarantine, Quarantines
from jev_watchdog.stats import GlobalStats, SurfaceStats
from jev_watchdog.transcript import (
    MAIN,
    SurfaceKey,
    conversation_lines,
    read_lines,
    resolve_transcript_path,
    surface_key,
)

LIFECYCLE_EVENTS = frozenset({"SessionStart", "SubagentStart", "SessionEnd"})
JUDGING_EVENTS = frozenset(
    {
        "UserPromptSubmit",
        "PostToolUse",
        "PostToolUseFailure",
        "PermissionDenied",
        "Stop",
        "SubagentStop",
    }
)
GATE_EVENTS = frozenset({"PreToolUse"})  # answered from the quarantine book, never judged
ALL_EVENTS = LIFECYCLE_EVENTS | JUDGING_EVENTS | GATE_EVENTS


class TargetError(Exception):
    """A quarantine/release target that cannot be acted on; status is the HTTP answer."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass(frozen=True)
class Job:
    event: dict
    transcript_lines: list[str]
    received_at: float  # time.monotonic() when the hook arrived


@dataclass
class Surface:
    key: SurfaceKey
    agent_type: str | None
    cwd: str | None
    transcript_path: Path
    stats: SurfaceStats = field(default_factory=SurfaceStats)
    # One queue and worker per judge, keyed by judge name, so a slow judge never delays a
    # fast one. None is the end-of-session sentinel: print the summary and stop the worker.
    queues: dict[str, asyncio.Queue[Job | None]] = field(default_factory=dict)
    workers: dict[str, asyncio.Task] = field(default_factory=dict)
    label_override: str | None = None  # replayed cases are named, not truncated ids

    @property
    def label(self) -> str:
        return self.label_override or self.key.label(self.agent_type)


class SurfaceRegistry:
    def __init__(
        self,
        judges: list[Judge],
        questions: list[Question],
        printer: Printer,
        stats: GlobalStats | None = None,
        enforce: bool = False,
    ) -> None:
        names = [judge.name for judge in judges]
        if len(set(names)) != len(names):
            raise ValueError(f"duplicate judge names: {names}")
        self.judges = judges
        self.questions = questions
        self.printer = printer
        self.stats = stats or GlobalStats()
        for judge in judges:
            self.stats.judge(judge.name)  # table rows in the order given
        self.surfaces: dict[SurfaceKey, Surface] = {}
        # Every judge's verdicts are folded, so a dry run and side-by-side judges report what
        # they would do. Only the first judge's trips quarantine, and only when enforcing.
        self.enforce = enforce
        self.decider = Decider(questions)
        self.quarantines = Quarantines()
        # Optional observer: on_verdict(surface, judge_name, job, verdict, flagged_ids, tripped)
        self.on_verdict: Callable[[Surface, str, Job, Verdict, set[str], bool], None] | None = None

    def handle(self, payload: dict) -> dict | None:
        """Route one hook payload and return the JSON to answer it with, if any.

        Never raises; must run inside the event loop.
        """
        received_at = time.monotonic()
        event = payload.get("hook_event_name")
        if not event or not payload.get("session_id") or not payload.get("transcript_path"):
            self.bad_payload("missing hook_event_name, session_id or transcript_path")
            return None

        surface = self._surface_for(payload)
        surface.stats.record_event(event)
        self.stats.record_event(event)
        if event in GATE_EVENTS:
            return self._gate(surface, payload)  # printed only when rejected
        self.printer.event(surface.label, payload)

        if event == "SessionEnd":
            for other in self.surfaces.values():
                if other.key.session_id == surface.key.session_id:
                    self._enqueue(other, None)
        elif event in JUDGING_EVENTS:
            try:
                lines = conversation_lines(read_lines(surface.transcript_path))
            except FileNotFoundError:
                lines = []  # session start: the hook fires before the transcript exists
            except OSError as exc:
                surface.stats.record_error("transcript")
                self.stats.record_error("transcript")
                self.printer.error(surface.label, "transcript", str(exc))
                return None
            if lines:
                self._enqueue(surface, Job(payload, lines, received_at))
            else:
                self.printer.note(
                    surface.label, "no conversation in transcript yet, registered only"
                )
        return None

    def quarantine(self, target: str, reason: str) -> Quarantine:
        """Quarantine by hand. Holds whether or not rules are enforced."""
        surface = self._resolve(target)
        entry = self._add(surface, reason, "manual")
        if entry is None:
            raise TargetError(409, f"{surface.label} is already quarantined")
        return entry

    def release(self, target: str) -> Quarantine:
        surface = self._resolve(target)
        entry = self.quarantines.release(surface.key)
        if entry is None:
            raise TargetError(404, f"{surface.label} is not quarantined")
        self.decider.reset(surface.key)  # or the old evidence would quarantine it again
        self.printer.released(surface.label)
        return entry

    def bad_payload(self, message: str) -> None:
        self.stats.record_error("payload")
        self.printer.error("-", "payload", message)

    def summaries(self) -> dict[str, SurfaceStats]:
        return {surface.label: surface.stats for surface in self.surfaces.values()}

    async def drain(self) -> None:
        queues = [q for surface in self.surfaces.values() for q in surface.queues.values()]
        await asyncio.gather(*(queue.join() for queue in queues))

    async def shutdown(self) -> None:
        workers = [
            worker
            for surface in self.surfaces.values()
            for worker in surface.workers.values()
            if not worker.done()
        ]
        for worker in workers:
            worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)

    def _gate(self, surface: Surface, payload: dict) -> dict | None:
        blocking = self.quarantines.blocking(surface.key)
        if blocking is None:
            return None
        self.stats.rejected += 1
        self.printer.rejected(surface.label, payload, blocking.reason)
        return blocking.deny_body()

    def _tripped(self, surface: Surface, judge: Judge, trip: Trip) -> None:
        decides = self.enforce and judge is self.judges[0]
        if not (decides and self._add(surface, trip.describe(), f"rule:{judge.name}")):
            self.printer.quarantine(surface.label, judge.name, trip.describe(), enforced=False)

    def _add(self, surface: Surface, reason: str, source: str) -> Quarantine | None:
        entry = Quarantine(surface.key, surface.label, reason, source, self.printer.clock())
        if not self.quarantines.add(entry):
            return None
        self.stats.quarantines += 1
        self.printer.quarantine(surface.label, source, reason, enforced=True)
        return entry

    def _resolve(self, target: str) -> Surface:
        """A target is what the console shows: <session prefix>[/<agent prefix>[:type]]."""
        session, _, agent = target.strip().partition("/")
        agent = agent.partition(":")[0] or MAIN
        matches = [
            surface
            for key, surface in self.surfaces.items()
            if session and key.session_id.startswith(session) and _agent_matches(key, agent)
        ]
        if not matches:
            raise TargetError(404, f"no agent thread matches {target!r}")
        if len(matches) > 1:
            labels = ", ".join(sorted(surface.label for surface in matches))
            raise TargetError(409, f"{target!r} is ambiguous: {labels}")
        return matches[0]

    def _surface_for(self, payload: dict) -> Surface:
        key = surface_key(payload)
        surface = self.surfaces.get(key)
        if surface is None:
            surface = Surface(
                key=key,
                agent_type=payload.get("agent_type"),
                cwd=payload.get("cwd"),
                transcript_path=resolve_transcript_path(payload),
                label_override=payload.get("surface_label"),
            )
            self.surfaces[key] = surface
            self.stats.surfaces += 1
        elif payload.get("agent_transcript_path"):
            surface.transcript_path = resolve_transcript_path(payload)
        return surface

    def _enqueue(self, surface: Surface, job: Job | None) -> None:
        for judge in self.judges:
            queue = surface.queues.setdefault(judge.name, asyncio.Queue())
            worker = surface.workers.get(judge.name)
            if worker is None or worker.done():
                surface.workers[judge.name] = asyncio.create_task(
                    self._work(surface, judge, queue), name=f"{judge.name}:{surface.label}"
                )
            queue.put_nowait(job)

    async def _work(self, surface: Surface, judge: Judge, queue: asyncio.Queue) -> None:
        while True:
            job = await queue.get()
            try:
                if job is None:
                    self.printer.surface_summary(surface.label, surface.stats, judge.name)
                    return
                await self._judge(surface, judge, job)
            finally:
                queue.task_done()

    async def _judge(self, surface: Surface, judge: Judge, job: Job) -> None:
        request = JudgeRequest(surface.key, job.event, job.transcript_lines, self.questions)
        try:
            verdict = await judge.judge(request)
        except JudgeError as exc:
            self._judge_error(surface, judge, exc.kind, exc.message)
        except Exception as exc:  # noqa: BLE001 - a judge bug must not kill the worker
            self._judge_error(surface, judge, "other", repr(exc))
        else:
            lag_ms = (time.monotonic() - job.received_at) * 1000
            flagged = surface.stats.judge(judge.name).record_verdict(self.questions, verdict)
            self.stats.judge(judge.name).record_verdict(verdict, lag_ms)
            step = job.event.get("replay_step")
            self.printer.verdict(surface.label, judge.name, verdict, flagged, step)
            trip = self.decider.fold(surface.key, judge.name, job.event, verdict)
            if trip is not None:
                self._tripped(surface, judge, trip)
            if self.on_verdict:
                tripped = self.decider.tripped(surface.key, judge.name) is not None
                self.on_verdict(surface, judge.name, job, verdict, flagged, tripped)

    def _judge_error(self, surface: Surface, judge: Judge, kind: str, message: str) -> None:
        surface.stats.judge(judge.name).record_error(kind)
        self.stats.judge(judge.name).record_error(kind)
        self.printer.error(surface.label, kind, message, judge=judge.name)


def _agent_matches(key: SurfaceKey, agent: str) -> bool:
    if agent == MAIN:
        return key.agent_id == MAIN
    return key.agent_id != MAIN and key.agent_id.removeprefix("agent-").startswith(agent)
