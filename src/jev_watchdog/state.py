"""The watchdog as one JSON document: what an attached dashboard draws besides the feed.

Built from the live objects in one synchronous pass, so it is consistent. Nothing here is
kept: the dashboard asks again rather than recompute statistics on its side.
"""

from dataclasses import dataclass
from datetime import datetime

from jev_watchdog.stats import ChoiceStat, JudgeSurfaceStats, NumericStat
from jev_watchdog.surfaces import Surface, SurfaceRegistry

# The registry never forgets a thread; a dashboard has no use for last month's.
MAX_THREADS = 200


@dataclass(frozen=True)
class DaemonInfo:
    boot: str  # Feed.boot: changes when the watchdog restarts
    pid: int
    started_at: datetime
    port: int
    log_path: str | None


def snapshot(registry: SurfaceRegistry, info: DaemonInfo, now: datetime) -> dict:
    stats = registry.stats
    recent = sorted(
        registry.surfaces.values(), key=lambda s: s.last_seen or datetime.min, reverse=True
    )
    return {
        "boot": info.boot,
        "pid": info.pid,
        "started_at": _iso(info.started_at),
        "now": _iso(now),
        "port": info.port,
        "log": info.log_path,
        "mode": {
            "enforce": registry.enforce,
            "rules": [rule.id for rule in registry.decider.rules],
            "decider": registry.judges[0].name,
        },
        "judges": [
            {
                "name": name,
                "judgments": judge.judgments,
                "errors": dict(judge.errors),
                "latency_p50": judge.latency(50),
                "latency_p95": judge.latency(95),
                "lag_p50": judge.lag(50),
                "lag_p95": judge.lag(95),
                "input_tokens": judge.input_tokens,
                "cost_usd": judge.cost_usd,
            }
            for name, judge in stats.judges.items()
        ],
        "totals": {
            "surfaces": stats.surfaces,
            "events": sum(stats.events.values()),
            "judgments": stats.judgments,
            "errors": dict(stats.errors),
            "quarantines": stats.quarantines,
            "rejected": stats.rejected,
            "cost_usd": stats.cost_usd,
        },
        "threads": [_thread(registry, surface) for surface in recent[:MAX_THREADS]],
    }


def _thread(registry: SurfaceRegistry, surface: Surface) -> dict:
    blocking = registry.quarantines.blocking(surface.key)
    return {
        "label": surface.label,
        "session_id": surface.key.session_id,
        "agent_id": surface.key.agent_id,
        "agent_type": surface.agent_type,
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
        "errors": dict(stats.errors),
        "tripped": registry.decider.tripped(surface.key, judge) is not None,
        "evidence": {
            rule.id: {"value": evidence.get(rule.id, 0.0), "limit": rule.quarantine_limit}
            for rule in registry.decider.rules
        },
        "questions": {qid: _question(stat) for qid, stat in stats.questions.items()},
    }


def _question(stat: NumericStat | ChoiceStat) -> dict:
    if isinstance(stat, ChoiceStat):
        return {
            "kind": "choice",
            "counts": dict(stat.counts),
            "last": stat.last,
            "streak": stat.streak,
            "longest": stat.longest_streak,
        }
    return {
        "kind": "numeric",
        "n": stat.n,
        "last": stat.last,
        "mean": stat.mean,
        "ewma": stat.ewma,
        "min": stat.min,
        "max": stat.max,
        "streak": stat.streak,
        "longest": stat.longest_streak,
    }


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")
