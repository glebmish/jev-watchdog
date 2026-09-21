"""What an attached dashboard reads, and the routes it reads it from.

/state is the watchdog as one JSON document, built from the live objects in one synchronous
pass. /records is the feed after a record number. /history and /timeline are what the charts
draw. Nothing is kept here: the dashboard asks again rather than compute on its side.

These routes show what every watched session is doing, so `add_routes` is called only for the
app on the private socket (serve.py), never for the one on the port.
"""

from datetime import datetime

from aiohttp import web

from jev_watchdog.core.stats import ChoiceStat, JudgeSurfaceStats, NumericStat
from jev_watchdog.core.surfaces import Surface, SurfaceRegistry
from jev_watchdog.core.transcript import SurfaceKey
from jev_watchdog.display.feed import Feed

# The watchdog keeps a swarm whole; a dashboard is sent the threads heard from last, and every
# quarantined one. This bounds a poll, not what is remembered (surfaces.THREAD_TTL does that).
MAX_SHOWN = 200
RECENT_JUDGMENTS = 200  # of each judge's latencies and lags, for the dashboard's chart
MAX_TIMELINE_MINUTES = 7 * 24 * 60
MAX_TIMELINE_BUCKETS = 400


def add_routes(app: web.Application, registry: SurfaceRegistry, feed: Feed) -> None:
    async def state(request: web.Request) -> web.Response:
        return web.json_response(snapshot(registry, feed.boot, registry.printer.clock()))

    async def records(request: web.Request) -> web.Response:
        since = _number(request, "since", 0)
        return web.json_response({"boot": feed.boot, "records": feed.since(since)})

    async def history(request: web.Request) -> web.Response:
        key = SurfaceKey(request.query.get("session_id", ""), request.query.get("agent_id", ""))
        if key not in registry.surfaces:
            return web.json_response({"error": "no such agent thread"}, status=404)
        return web.json_response(registry.history.series(key))

    async def timeline(request: web.Request) -> web.Response:
        minutes = min(max(_number(request, "minutes", 60), 1), MAX_TIMELINE_MINUTES)
        buckets = min(max(_number(request, "buckets", 60), 1), MAX_TIMELINE_BUCKETS)
        judge = request.query.get("judge") or registry.judges[0].name
        threads = [(surface.key, surface.label) for surface in shown(registry)]
        now = registry.printer.clock()
        return web.json_response(registry.history.timeline(threads, judge, now, minutes, buckets))

    app.router.add_get("/state", state)
    app.router.add_get("/records", records)
    app.router.add_get("/history", history)
    app.router.add_get("/timeline", timeline)


def _number(request: web.Request, name: str, default: int) -> int:
    try:
        return int(request.query.get(name, default))
    except ValueError:
        raise web.HTTPBadRequest(text=f"{name} must be a number") from None


def snapshot(registry: SurfaceRegistry, boot: str, now: datetime) -> dict:
    stats = registry.stats
    return {
        "boot": boot,  # Feed.boot: changes when the watchdog restarts
        "started_at": _iso(registry.started_at),
        "now": _iso(now),
        "mode": {
            "enforce": registry.enforce,
            "rules": [rule.id for rule in registry.decider.rules],
            "decider": registry.judges[0].name,
        },
        "judges": [
            {
                "name": name,
                "judgments": judge.judgments,
                "errors": judge.errors,
                "recent_latency_ms": list(judge.latencies_ms)[-RECENT_JUDGMENTS:],
                "recent_lag_ms": list(judge.lags_ms)[-RECENT_JUDGMENTS:],
            }
            for name, judge in stats.judges.items()
        ],
        "totals": {
            "surfaces": stats.surfaces,
            "events": sum(stats.events.values()),
            "judgments": stats.judgments,
            "errors": stats.errors,
            "quarantines": stats.quarantines,
            "rejected": stats.rejected,
            "cost_usd": stats.cost_usd,
        },
        "threads": [_thread(registry, surface) for surface in shown(registry)],
    }


def shown(registry: SurfaceRegistry) -> list[Surface]:
    """In the registry's order, the least recently heard of first."""
    surfaces = list(registry.surfaces.values())
    held = [s for s in surfaces[:-MAX_SHOWN] if s.key in registry.quarantines]
    return held + surfaces[-MAX_SHOWN:]


def _thread(registry: SurfaceRegistry, surface: Surface) -> dict:
    blocking = registry.quarantines.blocking(surface.key)
    return {
        "label": surface.label,
        "session_id": surface.key.session_id,
        "agent_id": surface.key.agent_id,
        "cwd": surface.cwd,
        "last_event": surface.last_event,
        "last_seen": None if surface.last_seen is None else _iso(surface.last_seen),
        "events": sum(surface.stats.events.values()),
        "judgments": surface.stats.judgments,
        "context": registry.contexts.get(surface.key.session_id, registry.default_context),
        # The entry that rejects this thread's tool calls: its own, or its main thread's.
        "quarantine": None if blocking is None else blocking.as_dict(),
        "judges": {
            judge.name: _judge_view(registry, surface, judge.name) for judge in registry.judges
        },
    }


def _judge_view(registry: SurfaceRegistry, surface: Surface, judge: str) -> dict:
    stats = surface.stats.judges.get(judge) or JudgeSurfaceStats()
    evidence = registry.decider.evidence(surface.key, judge)
    return {
        "judgments": stats.judgments,
        "errors": stats.errors,
        "tripped": registry.decider.tripped(surface.key, judge) is not None,
        "evidence": {
            rule.id: {"value": evidence.get(rule.id, 0.0), "limit": rule.quarantine_limit}
            for rule in registry.decider.rules
        },
        "questions": {qid: _question(stat) for qid, stat in stats.questions.items()},
    }


def _question(stat: NumericStat | ChoiceStat) -> dict:
    # Not dataclasses.asdict: it rebuilds a Counter from its items, as {(choice, n): 1}.
    kind = "choice" if isinstance(stat, ChoiceStat) else "numeric"
    return {"kind": kind} | vars(stat)


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")
