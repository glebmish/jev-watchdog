"""Where a watchdog keeps what outlives a terminal: the attach socket, a service's logs."""

import os
from collections.abc import Mapping
from pathlib import Path


def state_dir(env: Mapping[str, str] = os.environ) -> Path:
    base = Path(env.get("XDG_STATE_HOME") or "")
    if not base.is_absolute():  # the XDG spec: a relative value is to be ignored
        base = Path.home() / ".local" / "state"
    return base / "jev-watchdog"


def socket_path(port: int, env: Mapping[str, str] = os.environ) -> Path:
    """One per port: whoever holds the port owns the socket, and `attach --port` finds it."""
    return state_dir(env) / f"attach-{port}.sock"


def private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)
    return path
