"""Surface identity and transcript access for Claude Code hook payloads."""

import json
from pathlib import Path
from typing import NamedTuple

MAIN = "main"
CONVERSATION_TYPES = frozenset({"user", "assistant"})


class SurfaceKey(NamedTuple):
    """One agent thread: the session's main thread or one subagent."""

    session_id: str
    agent_id: str

    def label(self, agent_type: str | None = None) -> str:
        session = self.session_id[:6]
        if self.agent_id == MAIN:
            return f"{session}/{MAIN}"
        agent = self.agent_id.removeprefix("agent-")[:6]
        return f"{session}/{agent}:{agent_type}" if agent_type else f"{session}/{agent}"


def surface_key(payload: dict) -> SurfaceKey:
    return SurfaceKey(payload["session_id"], payload.get("agent_id") or MAIN)


def resolve_transcript_path(payload: dict) -> Path:
    main = Path(payload["transcript_path"]).expanduser()
    agent_id = payload.get("agent_id")
    if not agent_id:
        return main
    explicit = payload.get("agent_transcript_path")
    if explicit:
        return Path(explicit).expanduser()
    name = agent_id if agent_id.startswith("agent-") else f"agent-{agent_id}"
    return main.with_suffix("") / "subagents" / f"{name}.jsonl"


def read_lines(path: Path) -> list[str]:
    """Return the transcript's JSONL lines unmodified, skipping blank lines."""
    text = path.read_text(encoding="utf-8")
    return [line for line in text.split("\n") if line.strip()]


def conversation_lines(lines: list[str]) -> list[str]:
    """Keep only what the user and the agent said and did, each line unmodified.

    Everything else in a Claude Code transcript is harness bookkeeping (attachments such
    as skill listings and prompt snapshots, queue operations, system notes, injected
    isMeta messages). It is most of a young transcript and says nothing about behaviour.
    """
    return [line for line in lines if _is_conversation(line)]


def _is_conversation(line: str) -> bool:
    try:
        entry = json.loads(line)
    except ValueError:  # e.g. a half-flushed last line
        return False
    return (
        isinstance(entry, dict)
        and entry.get("type") in CONVERSATION_TYPES
        and not entry.get("isMeta")
    )
