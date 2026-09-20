"""Talk to a running watchdog: the control subcommands over its port, the dashboard over its
private socket. The socket serves everything the port does, plus what state.py adds."""

from pathlib import Path
from typing import Self

import aiohttp

TIMEOUT_S = 5.0


class Unreachable(Exception):
    """Nothing to talk to: nobody listens, or the connection broke."""


class NotAWatchdog(Unreachable):
    """Something answered, but not the way a watchdog does."""


class Refused(Exception):
    """The watchdog said no; the message is its own."""


class Client:
    def __init__(self, connector: aiohttp.BaseConnector, base: str) -> None:
        # aiohttp ignores http_proxy unless asked, so a localhost call never goes to a proxy.
        timeout = aiohttp.ClientTimeout(total=TIMEOUT_S)
        self._session = aiohttp.ClientSession(connector=connector, timeout=timeout)
        self._base = base

    @classmethod
    def on_port(cls, port: int) -> Self:
        return cls(aiohttp.TCPConnector(), f"http://127.0.0.1:{port}")

    @classmethod
    def on_socket(cls, socket: Path) -> Self:
        # The Host of a socket request is not checked (server._refusal), but must be there.
        return cls(aiohttp.UnixConnector(path=str(socket)), "http://localhost")

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_exc) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._session.close()

    # --- control: on the port and on the socket ---

    async def quarantined(self) -> list[dict]:
        return (await self._call("GET", "/quarantine"))["quarantined"]

    async def quarantine(self, target: str, reason: str) -> dict:
        return await self._call("POST", "/quarantine", {"target": target, "reason": reason})

    async def release(self, target: str) -> dict:
        return await self._call("POST", "/release", {"target": target})

    async def context(self, target: str, text: str) -> dict:
        return await self._call("POST", "/context", {"target": target, "text": text})

    # --- what the dashboard reads: on the socket only (state.add_routes) ---

    async def state(self) -> dict:
        return await self._call("GET", "/state")

    async def records(self, since: int = 0) -> dict:
        """{"boot", "records"}: the feed after record number `since`."""
        return await self._call("GET", "/records", params={"since": str(since)})

    async def history(self, session_id: str, agent_id: str) -> dict:
        params = {"session_id": session_id, "agent_id": agent_id}
        return await self._call("GET", "/history", params=params)

    async def timeline(self, minutes: int, buckets: int, judge: str | None = None) -> dict:
        params = {"minutes": str(minutes), "buckets": str(buckets)}
        return await self._call(
            "GET", "/timeline", params=params | ({"judge": judge} if judge else {})
        )

    async def _call(
        self, method: str, path: str, body: dict | None = None, params: dict | None = None
    ) -> dict:
        try:
            async with self._session.request(
                method, self._base + path, json=body, params=params
            ) as response:
                try:
                    answer = await response.json(content_type=None)
                except ValueError:  # another server's page, or aiohttp's own plain-text 404
                    answer = None
                if not isinstance(answer, dict):
                    raise NotAWatchdog((await response.text())[:80])
                if response.status != 200:
                    raise Refused(str(answer.get("error", f"HTTP {response.status}")))
                return answer
        except (aiohttp.ClientError, OSError, TimeoutError) as exc:
            raise Unreachable(str(exc) or type(exc).__name__) from exc
