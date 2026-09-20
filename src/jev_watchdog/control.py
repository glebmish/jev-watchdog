"""Talk to a running watchdog: the status / release / quarantine subcommands."""

import json
import urllib.error
import urllib.request


class Unreachable(Exception):
    pass


class NotAWatchdog(Exception):
    """Something answered, but not the way a watchdog does."""


# No proxies: urlopen would send this localhost call to http_proxy, and the proxy's failure
# would read as "no watchdog listening".
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def call(port: int, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=None if body is None else json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method=method,
    )
    try:
        with _opener.open(request, timeout=5) as response:
            return response.status, _answer(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, _answer(exc.read())
    except (urllib.error.URLError, OSError) as exc:
        raise Unreachable(str(exc)) from exc


def _answer(raw: bytes) -> dict:
    try:
        answer = json.loads(raw)
    except ValueError:  # another server's page, or aiohttp's own plain-text 404/405
        answer = None
    if not isinstance(answer, dict):
        raise NotAWatchdog(raw[:80].decode(errors="replace"))
    return answer
