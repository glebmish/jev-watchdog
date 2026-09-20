"""Talk to a running watchdog over its private socket: what `attach` and `run --tui` use.

The socket serves the whole app, so control goes the same way as /state and /events.
"""

import json
from collections.abc import AsyncIterator
from pathlib import Path

import aiohttp

BASE = "http://localhost"  # the Host of a socket request is not checked, but must be there
CONNECT_TIMEOUT_S = 5.0


class AttachError(Exception):
    """Nothing to talk to: no socket, nobody behind it, or the connection broke."""


class ControlError(Exception):
    """The watchdog said no; the message is its own."""


class AttachClient:
    def __init__(self, socket: Path) -> None:
        self.socket = socket
        self._session: aiohttp.ClientSession | None = None

    async def state(self) -> dict:
        return await self._call("GET", "/state")

    async def history(self, session_id: str, agent_id: str) -> dict:
        return await self._call(
            "GET", "/history", params={"session_id": session_id, "agent_id": agent_id}
        )

    async def timeline(self, minutes: int, buckets: int, judge: str | None = None) -> dict:
        params = {"minutes": str(minutes), "buckets": str(buckets)} | (
            {"judge": judge} if judge else {}
        )
        return await self._call("GET", "/timeline", params=params)

    async def quarantine(self, target: str, reason: str) -> dict:
        return await self._call("POST", "/quarantine", {"target": target, "reason": reason})

    async def release(self, target: str) -> dict:
        return await self._call("POST", "/release", {"target": target})

    async def context(self, target: str, text: str) -> dict:
        return await self._call("POST", "/context", {"target": target, "text": text})

    async def events(self, since: int = 0) -> AsyncIterator[tuple[str, dict]]:
        """("hello", {"boot"}) and then ("record", record), until the watchdog ends the stream."""
        # No total timeout: the stream is open for as long as the dashboard is.
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=CONNECT_TIMEOUT_S)
        try:
            async with self._http().get(
                f"{BASE}/events", params={"since": str(since)}, timeout=timeout
            ) as response:
                if response.status != 200:
                    raise AttachError(f"/events answered HTTP {response.status}")
                name, data = "message", None
                async for raw in response.content:
                    line = raw.decode().rstrip("\n")
                    if line.startswith("event:"):
                        name = line.removeprefix("event:").strip()
                    elif line.startswith("data:"):
                        data = json.loads(line.removeprefix("data:"))
                    elif not line and data is not None:
                        yield name, data
                        name, data = "message", None
        except (aiohttp.ClientError, OSError, ValueError) as exc:
            raise AttachError(str(exc) or type(exc).__name__) from exc

    async def aclose(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def _call(
        self, method: str, path: str, body: dict | None = None, params: dict | None = None
    ) -> dict:
        timeout = aiohttp.ClientTimeout(total=CONNECT_TIMEOUT_S)
        try:
            async with self._http().request(
                method, f"{BASE}{path}", json=body, params=params, timeout=timeout
            ) as response:
                answer = await response.json()
                if response.status != 200:
                    raise ControlError(str(answer.get("error", f"HTTP {response.status}")))
                return answer
        except (aiohttp.ClientError, OSError, ValueError, TimeoutError) as exc:
            raise AttachError(str(exc) or type(exc).__name__) from exc

    def _http(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            connector = aiohttp.UnixConnector(path=str(self.socket))
            self._session = aiohttp.ClientSession(connector=connector)
        return self._session
