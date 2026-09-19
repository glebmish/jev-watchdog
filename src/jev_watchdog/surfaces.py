"""Surfaces: one per agent thread, each with its own queue, worker and statistics."""

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

from jev_watchdog.judge.base import Judge, JudgeError, JudgeRequest
from jev_watchdog.pack import Question
from jev_watchdog.printer import Printer
from jev_watchdog.stats import GlobalStats, SurfaceStats
from jev_watchdog.transcript import SurfaceKey, read_lines, resolve_transcript_path, surface_key

LIFECYCLE_EVENTS = frozenset({"SessionStart", "SubagentStart", "SessionEnd"})
JUDGING_EVENTS = frozenset(
    {"UserPromptSubmit", "PostToolUse", "PostToolUseFailure", "PermissionDenied", "Stop", "SubagentStop"}
)
ALL_EVENTS = LIFECYCLE_EVENTS | JUDGING_EVENTS


@dataclass(frozen=True)
class Job:
    event: dict
    transcript_lines: list[str]


@dataclass
class Surface:
    key: SurfaceKey
    agent_type: str | None
    cwd: str | None
    transcript_path: Path
    stats: SurfaceStats = field(default_factory=SurfaceStats)
    # None is the end-of-session sentinel: print the summary and stop the worker.
    queue: asyncio.Queue[Job | None] = field(default_factory=asyncio.Queue)
    worker: asyncio.Task | None = None

    @property
    def label(self) -> str:
        return self.key.label(self.agent_type)


class SurfaceRegistry:
    def __init__(
        self,
        judge: Judge,
        questions: list[Question],
        printer: Printer,
        stats: GlobalStats | None = None,
    ) -> None:
        self.judge = judge
        self.questions = questions
        self.printer = printer
        self.stats = stats or GlobalStats()
        self.surfaces: dict[SurfaceKey, Surface] = {}

    def handle(self, payload: dict) -> None:
        """Route one hook payload. Never raises; must run inside the event loop."""
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
                lines = read_lines(surface.transcript_path)
            except OSError as exc:
                self._error(surface, "transcript", str(exc))
                return
            self._enqueue(surface, Job(payload, lines))

    def bad_payload(self, message: str) -> None:
        self.stats.record_error("payload")
        self.printer.error("-", "payload", message)

    def summaries(self) -> dict[str, SurfaceStats]:
        return {surface.label: surface.stats for surface in self.surfaces.values()}

    async def drain(self) -> None:
        await asyncio.gather(*(surface.queue.join() for surface in self.surfaces.values()))

    async def shutdown(self) -> None:
        workers = [s.worker for s in self.surfaces.values() if s.worker and not s.worker.done()]
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
        if surface.worker is None or surface.worker.done():
            surface.worker = asyncio.create_task(self._work(surface), name=f"judge:{surface.label}")
        surface.queue.put_nowait(job)

    async def _work(self, surface: Surface) -> None:
        while True:
            job = await surface.queue.get()
            try:
                if job is None:
                    self.printer.surface_summary(surface.label, surface.stats)
                    return
                await self._judge(surface, job)
            finally:
                surface.queue.task_done()

    async def _judge(self, surface: Surface, job: Job) -> None:
        request = JudgeRequest(surface.key, job.event, job.transcript_lines, self.questions)
        try:
            verdict = await self.judge.judge(request)
        except JudgeError as exc:
            self._error(surface, exc.kind, exc.message)
        except Exception as exc:  # a judge bug must not kill the surface's worker
            self._error(surface, "other", repr(exc))
        else:
            flagged = surface.stats.record_verdict(self.questions, verdict)
            self.stats.record_verdict(verdict)
            self.printer.verdict(surface.label, verdict, flagged)

    def _error(self, surface: Surface, kind: str, message: str) -> None:
        surface.stats.record_error(kind)
        self.stats.record_error(kind)
        self.printer.error(surface.label, kind, message)
