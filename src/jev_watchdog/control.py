"""Talk to a running watchdog: the status / release / quarantine subcommands."""

import json
import urllib.error
import urllib.request


class Unreachable(Exception):
    pass


def call(port: int, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=None if body is None else json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")
    except (urllib.error.URLError, OSError) as exc:
        raise Unreachable(str(exc)) from exc
