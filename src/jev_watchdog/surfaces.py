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

from jev_watchdog.decide import TOOL_EVENTS, Decider, Trip
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
    tool_result_end,
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

# Claude Code writes the transcript asynchronously: a PostToolUse hook usually arrives before
# its tool call is in the file, and by the next hook a newer call is already "the most recent
# action". So a tool event is judged only once its result has reached the transcript.
TRANSCRIPT_WAIT_S = 2.0
TRANSCRIPT_POLL_S = 0.05


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
    context: str | None = None  # the session's context when the hook arrived


@dataclass(frozen=True)
class _Pending:
    """An event that cannot be dispatched yet: its transcript is behind, or an earlier event
    of its thread is still waiting and must stay ahead of it."""

    payload: dict | None  # None is the end-of-session sentinel
    received_at: float
    lines: list[str] | None  # the snapshot taken at receipt, when it was already complete
    context: str | None = None


@dataclass
class Surface:
    key: SurfaceKey
    agent_type: str | None
    cwd: str | None
    transcript_path: Path
    stats: SurfaceStats = field(default_factory=SurfaceStats)
    # One queue and worker per judge, keyed by judge name, so a slow judge never delays a
    # fast one. None is the end-of-session sentinel: print the summary and, if idle, stop.
    queues: dict[str, asyncio.Queue[Job | None]] = field(default_factory=dict)
    workers: dict[str, asyncio.Task] = field(default_factory=dict)
    # Events waiting for the transcript, dispatched in arrival order by one task.
    intake: asyncio.Queue[_Pending] = field(default_factory=asyncio.Queue)
    intake_worker: asyncio.Task | None = None
    # Held from a hook's arrival until it is admitted. The transcript is read in a thread,
    # and asyncio locks are fair, so a slow read cannot let a later event of the thread
    # overtake an earlier one.
    arrival: asyncio.Lock = field(default_factory=asyncio.Lock)
    backlog: int = 0
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
        transcript_wait_s: float = TRANSCRIPT_WAIT_S,
        context: str | None = None,
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
        self.transcript_wait_s = transcript_wait_s
        self.decider = Decider(questions)
        self.quarantines = Quarantines()
        # What the human told the watchdog about a session, by session id: every thread of
        # the session is judged with it. default_context stands in where nothing was said.
        self.default_context = context
        self.contexts: dict[str, str] = {}
        # Optional observer: on_verdict(surface, judge_name, job, verdict, flagged_ids, tripped)
        self.on_verdict: Callable[[Surface, str, Job, Verdict, set[str], bool], None] | None = None

    async def handle(self, payload: dict) -> dict | None:
        """Route one hook payload and return the JSON to answer it with, if any.

        Never raises. A gate event is answered without awaiting anything; a judging event
        returns once the transcript is snapshotted.
        """
        received_at = time.monotonic()
        event = payload.get("hook_event_name")
        required = (event, payload.get("session_id"), payload.get("transcript_path"))
        if not all(isinstance(value, str) and value for value in required):
            self.bad_payload("hook_event_name, session_id and transcript_path must be strings")
            return None
        if not isinstance(payload.get("agent_id"), str | None):
            self.bad_payload("agent_id must be a string")
            return None

        surface = self._surface_for(payload)
        surface.stats.record_event(event)
        self.stats.record_event(event)
        if event in GATE_EVENTS:
            return self._gate(surface, payload)  # printed only when rejected
        self.printer.event(surface.label, payload)

        if event == "SessionEnd":
            session = surface.key.session_id
            for other in [s for s in self.surfaces.values() if s.key.session_id == session]:
                async with other.arrival:
                    self._admit(other, _Pending(None, received_at, None))
        elif event in JUDGING_EVENTS:
            async with surface.arrival:
                await self._receive(surface, payload, received_at)
        return None

    async def _receive(self, surface: Surface, payload: dict, received_at: float) -> None:
        lines = await self._snapshot(surface)
        if lines is not None:
            own = _own_lines(lines, payload.get("tool_use_id"))
            behind = own is None and self._awaited(payload)
            context = self.contexts.get(surface.key.session_id, self.default_context)
            ready = None if behind else lines if own is None else own
            self._admit(surface, _Pending(payload, received_at, ready, context))

    def set_context(self, target: str, text: str) -> str:
        """Set (or, with blank text, clear) the context of the target's whole session."""
        surface = self._resolve(target)
        session = self.surfaces.get(SurfaceKey(surface.key.session_id, MAIN), surface)
        text = text.strip()
        if text:
            self.contexts[session.key.session_id] = text
            self.printer.note(session.label, f"context set: {text}")
        else:
            self.contexts.pop(session.key.session_id, None)
            self.printer.note(session.label, "context cleared")
        return session.label

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
        await asyncio.gather(*(surface.intake.join() for surface in self.surfaces.values()))
        queues = [q for surface in self.surfaces.values() for q in surface.queues.values()]
        await asyncio.gather(*(queue.join() for queue in queues))

    async def shutdown(self) -> None:
        workers = [
            worker
            for surface in self.surfaces.values()
            for worker in (*surface.workers.values(), surface.intake_worker)
            if worker is not None and not worker.done()
        ]
        for worker in workers:
            worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)

    async def _snapshot(self, surface: Surface) -> list[str] | None:
        """The thread's conversation right now; None (and reported) if it cannot be read."""
        path = surface.transcript_path
        try:
            # In a thread: the event loop also answers PreToolUse, and a large or slow file
            # must not make the deny of a quarantined thread time out.
            return await asyncio.to_thread(lambda: conversation_lines(read_lines(path)))
        except FileNotFoundError:
            return []  # session start: the hook fires before the transcript exists
        except OSError as exc:
            surface.stats.record_error("transcript")
            self.stats.record_error("transcript")
            self.printer.error(surface.label, "transcript", str(exc))
            return None

    def _awaited(self, payload: dict) -> bool:
        tool_use_id = payload.get("tool_use_id")
        return (
            self.transcript_wait_s > 0
            and payload.get("hook_event_name") in TOOL_EVENTS
            and isinstance(tool_use_id, str)
            and bool(tool_use_id)
        )

    def _admit(self, surface: Surface, pending: _Pending) -> None:
        ready = pending.payload is None or pending.lines is not None
        if ready and surface.backlog == 0:
            self._dispatch(surface, pending, pending.lines)
            return
        surface.backlog += 1
        surface.intake.put_nowait(pending)
        if surface.intake_worker is None or surface.intake_worker.done():
            surface.intake_worker = self._start(
                surface, self._take_in(surface), f"intake:{surface.label}"
            )

    async def _take_in(self, surface: Surface) -> None:
        while True:
            pending = await surface.intake.get()
            try:
                lines = pending.lines
                if pending.payload is not None and lines is None:
                    lines = await self._caught_up(surface, pending.payload["tool_use_id"])
                self._dispatch(surface, pending, lines)
            finally:
                surface.backlog -= 1
                surface.intake.task_done()

    async def _caught_up(self, surface: Surface, tool_use_id: str) -> list[str] | None:
        deadline = time.monotonic() + self.transcript_wait_s
        while True:
            lines = await self._snapshot(surface)
            own = None if lines is None else _own_lines(lines, tool_use_id)
            if lines is None or own is not None:
                return own
            if time.monotonic() >= deadline:
                waited = f"{self.transcript_wait_s * 1000:.0f}ms"
                self.printer.note(
                    surface.label, f"transcript still behind after {waited}, judging it as it is"
                )
                return lines
            await asyncio.sleep(TRANSCRIPT_POLL_S)

    def _dispatch(self, surface: Surface, pending: _Pending, lines: list[str] | None) -> None:
        if pending.payload is None:
            self._enqueue(surface, None)
        elif lines:
            job = Job(pending.payload, lines, pending.received_at, pending.context)
            self._enqueue(surface, job)
        elif lines is not None:
            self.printer.note(surface.label, "no conversation in transcript yet, registered only")

    def _gate(self, surface: Surface, payload: dict) -> dict | None:
        blocking = self.quarantines.blocking(surface.key)
        if blocking is None:
            return None
        self.stats.rejected += 1
        self.printer.rejected(surface.label, payload, blocking.reason)
        return blocking.deny_body()

    def _tripped(self, surface: Surface, judge: Judge, trip: Trip) -> None:
        source, reason = f"rule:{judge.name}", trip.describe()
        if not (self.enforce and judge is self.judges[0]):
            self.printer.quarantine(surface.label, judge.name, reason, enforced=False)
        elif self._add(surface, reason, source) is None:
            # Already held by hand: keep the trip on the entry, so whoever thinks about
            # releasing it sees that a rule wants it quarantined too.
            note = f"{source} also tripped: {reason}"
            self.quarantines.amend(surface.key, note)
            self.printer.note(surface.label, f"already quarantined; {note}")

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
                surface.workers[judge.name] = self._start(
                    surface, self._work(surface, judge, queue), f"{judge.name}:{surface.label}"
                )
            queue.put_nowait(job)

    def _start(self, surface: Surface, work, name: str) -> asyncio.Task:
        def report_death(task: asyncio.Task) -> None:
            # Nobody awaits these tasks, so without this a bug that ends one is silent and
            # the thread just stops being judged.
            if not task.cancelled() and task.exception() is not None:
                surface.stats.record_error("worker")
                self.stats.record_error("worker")
                self.printer.error(surface.label, "worker", f"{name} died: {task.exception()!r}")

        task = asyncio.create_task(work, name=name)
        task.add_done_callback(report_death)
        return task

    async def _work(self, surface: Surface, judge: Judge, queue: asyncio.Queue) -> None:
        while True:
            job = await queue.get()
            try:
                if job is None:
                    self.printer.surface_summary(surface.label, surface.stats, judge.name)
                    # A job put behind the mark while we were busy found this worker alive,
                    # so nobody else will take it. With the queue empty, the next job sees
                    # a finished worker and starts a new one.
                    if queue.empty():
                        return
                    continue
                await self._judge(surface, judge, job)
            finally:
                queue.task_done()

    async def _judge(self, surface: Surface, judge: Judge, job: Job) -> None:
        asked = (question.asked(job.context is not None) for question in self.questions)
        questions = [question for question in asked if question is not None]
        request = JudgeRequest(surface.key, job.event, job.transcript_lines, questions, job.context)
        try:
            verdict = await judge.judge(request)
            self._record(surface, judge, job, verdict)
        except JudgeError as exc:
            self._judge_error(surface, judge, exc.kind, exc.message)
        except Exception as exc:  # noqa: BLE001 - no bug, ours included, may kill the worker
            self._judge_error(surface, judge, "other", repr(exc))

    def _record(self, surface: Surface, judge: Judge, job: Job, verdict: Verdict) -> None:
        lag_ms = (time.monotonic() - job.received_at) * 1000
        flagged = surface.stats.judge(judge.name).record_verdict(self.questions, verdict)
        self.stats.judge(judge.name).record_verdict(verdict, lag_ms)
        step = job.event.get("replay_step")
        self.printer.verdict(surface.label, judge.name, verdict, flagged, step, job.context)
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


def _own_lines(lines: list[str], tool_use_id: object) -> list[str] | None:
    """The transcript as it was when this tool call ended; None while its result is missing.

    The snapshot is taken when the hook arrives, and with parallel tool calls the results of
    the others are in it by then. Judged whole, every one of those events would score the
    same last action, and the decider would fold that one score once under each id.
    """
    if not (isinstance(tool_use_id, str) and tool_use_id):
        return None
    end = tool_result_end(lines, tool_use_id)
    return None if end is None else lines[:end]


def _agent_matches(key: SurfaceKey, agent: str) -> bool:
    if agent == MAIN:
        return key.agent_id == MAIN
    return key.agent_id != MAIN and key.agent_id.removeprefix("agent-").startswith(agent)
