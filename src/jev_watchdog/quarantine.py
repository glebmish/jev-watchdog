"""Who is quarantined. In memory only: the watchdog fails open and forgets on restart."""

from dataclasses import dataclass, replace
from datetime import datetime

from jev_watchdog.transcript import MAIN, SurfaceKey


@dataclass(frozen=True)
class Quarantine:
    key: SurfaceKey
    label: str
    reason: str
    source: str  # "rule:<judge>" or "manual"
    at: datetime

    def as_dict(self) -> dict:
        return {
            "target": self.label,
            "session_id": self.key.session_id,
            "agent_id": self.key.agent_id,
            "reason": self.reason,
            "source": self.source,
            "at": self.at.isoformat(timespec="seconds"),
        }

    def deny_body(self) -> dict:
        reason = (
            f"jev-watchdog has quarantined this agent thread ({self.label}): {self.reason}. "
            "Every tool call is rejected until a human releases it with "
            f"`jev-watchdog release {self.label}`. Do not retry and do not look for another "
            "route. Stop and tell the user what you were doing."
        )
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }


class Quarantines:
    def __init__(self) -> None:
        self._entries: dict[SurfaceKey, Quarantine] = {}

    def add(self, quarantine: Quarantine) -> bool:
        if quarantine.key in self._entries:
            return False
        self._entries[quarantine.key] = quarantine
        return True

    def amend(self, key: SurfaceKey, note: str) -> None:
        entry = self._entries[key]
        self._entries[key] = replace(entry, reason=f"{entry.reason}; {note}")

    def release(self, key: SurfaceKey) -> Quarantine | None:
        return self._entries.pop(key, None)

    def blocking(self, key: SurfaceKey) -> Quarantine | None:
        """The thread's own quarantine, else its session's main-thread quarantine."""
        return self._entries.get(key) or self._entries.get(SurfaceKey(key.session_id, MAIN))

    def all(self) -> list[Quarantine]:
        return list(self._entries.values())
