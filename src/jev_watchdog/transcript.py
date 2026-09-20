"""Surface identity and transcript access for Claude Code hook payloads."""

import json
import os
import stat
from pathlib import Path
from typing import NamedTuple

MAIN = "main"
CONVERSATION_TYPES = frozenset({"user", "assistant"})
# The path comes from the hook payload, so it is whatever the agent's side says it is.
# Far above any real transcript; it only keeps /dev/zero or a runaway file out of memory.
MAX_TRANSCRIPT_BYTES = 256 * 2**20


class TranscriptError(OSError):
    """A transcript path that will not be read: not a regular file, or too large."""


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
    # O_NONBLOCK: opening a FIFO nobody writes to would otherwise never return.
    with open(os.open(path, os.O_RDONLY | os.O_NONBLOCK), "rb") as fh:
        if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
            raise TranscriptError(f"{path} is not a regular file")
        data = fh.read(MAX_TRANSCRIPT_BYTES + 1)
    if len(data) > MAX_TRANSCRIPT_BYTES:
        raise TranscriptError(f"{path} is over {MAX_TRANSCRIPT_BYTES} bytes")
    # Claude Code may be mid-write: a character cut in half spoils its own line, which is
    # not valid JSON yet and is dropped anyway, not the whole read.
    text = data.decode("utf-8", errors="replace")
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


def has_tool_result(lines: list[str], tool_use_id: str) -> bool:
    """Whether the transcript already holds the result of this tool call.

    Claude Code writes the transcript asynchronously, so a PostToolUse hook can arrive before
    the call it is about has reached the file.
    """
    for line in reversed(lines):
        if tool_use_id not in line:
            continue
        try:
            content = (json.loads(line).get("message") or {}).get("content")
        except ValueError, AttributeError:
            continue
        if isinstance(content, list) and any(
            isinstance(block, dict)
            and block.get("type") == "tool_result"
            and block.get("tool_use_id") == tool_use_id
            for block in content
        ):
            return True
    return False
