"""The hook endpoint, and the control endpoints behind
`jev-watchdog status|quarantine|release|context`.

Hooks are answered at once and judged in the background. The answer is an empty 200, except
for a PreToolUse of a quarantined thread, which gets a deny.

Nothing here authenticates the caller, but a browser must not be one: any page the operator
has open can POST to 127.0.0.1, and a DNS-rebinding page can read the answers too.
"""

import json
from collections.abc import Callable

from aiohttp import web

from jev_watchdog.core.surfaces import SurfaceRegistry, TargetError

LOCAL_HOSTS = ("127.0.0.1", "localhost")
# A hook carries the whole tool input and output (a large Write, a long Bash log). Over
# aiohttp's 1 MiB default the 413 is a non-2xx, which Claude Code does not block on: the
# large tool calls of a quarantined thread would go through, and never be judged or logged.
MAX_BODY_BYTES = 64 * 2**20


def create_app(registry: SurfaceRegistry) -> web.Application:
    @web.middleware
    async def local_clients_only(request: web.Request, handler) -> web.StreamResponse:
        refusal = _refusal(request)
        if refusal is None:
            try:
                return await handler(request)
            except web.HTTPRequestEntityTooLarge:
                refusal = 413, f"body over {MAX_BODY_BYTES} bytes"
        status, error = refusal
        registry.bad_payload(f"refused {request.method} {request.path}: {error}")
        return web.json_response({"error": error}, status=status)

    async def hooks(request: web.Request) -> web.Response:
        body = None
        try:
            payload = json.loads(await request.read())
        except ValueError as exc:
            registry.bad_payload(f"invalid JSON: {exc}")
        else:
            if isinstance(payload, dict):
                try:
                    body = await registry.handle(payload)
                except Exception as exc:  # noqa: BLE001 - a bug here must not fail the agent's hook
                    registry.bad_payload(f"handler failed: {exc!r}")
            else:
                registry.bad_payload(f"expected a JSON object, got {type(payload).__name__}")
        return web.Response(status=200) if body is None else web.json_response(body)

    async def listing(request: web.Request) -> web.Response:
        entries = [entry.as_dict() for entry in registry.quarantines.all()]
        return web.json_response({"quarantined": entries})

    def control(action: Callable[[dict], dict], fields: tuple[str, ...] = ("target",)):
        async def handler(request: web.Request) -> web.Response:
            try:
                body = json.loads(await request.read())
            except ValueError:
                body = None
            if not isinstance(body, dict) or not all(isinstance(body.get(f), str) for f in fields):
                expected = json.dumps(dict.fromkeys(fields, "..."))
                return web.json_response({"error": f"expected {expected}"}, status=400)
            try:
                return web.json_response(action(body))
            except TargetError as exc:
                return web.json_response({"error": exc.message}, status=exc.status)

        return handler

    def quarantine(body: dict) -> dict:
        return registry.quarantine(body["target"], str(body.get("reason") or "manual")).as_dict()

    def context(body: dict) -> dict:
        label = registry.set_context(body["target"], body["text"])
        return {"target": label, "context": body["text"].strip()}

    app = web.Application(middlewares=[local_clients_only], client_max_size=MAX_BODY_BYTES)
    app.router.add_post("/hooks", hooks)
    app.router.add_get("/quarantine", listing)
    app.router.add_post("/quarantine", control(quarantine))
    app.router.add_post(
        "/release", control(lambda body: registry.release(body["target"]).as_dict())
    )
    app.router.add_post("/context", control(context, fields=("target", "text")))
    return app


def _refusal(request: web.Request) -> tuple[int, str] | None:
    """Why a request cannot be from a hook, curl or the CLI; None when it can."""
    # Browsers name the page on every cross-site POST; none of our clients sends an Origin.
    if "Origin" in request.headers:
        return 403, "requests from a web page are refused"
    # A rebinding page reaches us under its own name. The port is the one this connection
    # was accepted on, so it is right whatever --port was (and in tests, which pick one).
    # A unix socket's name is its path: there is no port, and no page can open a socket file.
    sockname = request.transport.get_extra_info("sockname")
    if not isinstance(sockname, str | bytes):
        port = sockname[1]
        hosts = {f"{host}:{port}" for host in LOCAL_HOSTS}
        hosts |= set(LOCAL_HOSTS) if port == 80 else set()
        if request.headers.get("Host") not in hosts:
            return 403, f"Host must be one of {sorted(hosts)}"
    # A page can POST text/plain or a form without a preflight, but not application/json.
    if request.method == "POST" and request.content_type != "application/json":
        return 415, "Content-Type must be application/json"
    return None
