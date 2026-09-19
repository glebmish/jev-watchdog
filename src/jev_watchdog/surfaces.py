"""Surfaces: one per agent thread, each with its own queue, worker and statistics."""

import asyncio
import time
from dataclasses import dataclass, field
from pathlib import Path

from jev_watchdog.judge.base import Judge, JudgeError, JudgeRequest
from jev_watchdog.pack import Question
from jev_watchdog.printer import Printer
from jev_watchdog.stats import GlobalStats, SurfaceStats
from jev_watchdog.transcript import (
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
ALL_EVENTS = LIFECYCLE_EVENTS | JUDGING_EVENTS


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

    @property
    def label(self) -> str:
        return self.key.label(self.agent_type)


class SurfaceRegistry:
    def __init__(
        self,
        judges: list[Judge],
        questions: list[Question],
        printer: Printer,
        stats: GlobalStats | None = None,
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

    def handle(self, payload: dict) -> None:
        """Route one hook payload. Never raises; must run inside the event loop."""
        received_at = time.monotonic()
        event = payload.get("hook_event_name")
        if not event or not payload.get("session_id") or not payload.get("transcript_path"):
            self.bad_payload("missing hook_event_name, session_id or transcript_path")
            return

        surface = self._surface_for(payload)
        surface.stats.record_event(event)
        self.stats.record_event(event)
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
                return
            if lines:
                self._enqueue(surface, Job(payload, lines, received_at))
            else:
                self.printer.note(
                    surface.label, "no conversation in transcript yet, registered only"
                )

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

    def _surface_for(self, payload: dict) -> Surface:
        key = surface_key(payload)
        surface = self.surfaces.get(key)
        if surface is None:
            surface = Surface(
                key=key,
                agent_type=payload.get("agent_type"),
                cwd=payload.get("cwd"),
                transcript_path=resolve_transcript_path(payload),
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
            self.printer.verdict(surface.label, judge.name, verdict, flagged)

    def _judge_error(self, surface: Surface, judge: Judge, kind: str, message: str) -> None:
        surface.stats.judge(judge.name).record_error(kind)
        self.stats.judge(judge.name).record_error(kind)
        self.printer.error(surface.label, kind, message, judge=judge.name)
