"""The hook endpoint, plus the control endpoints behind `jev-watchdog status|quarantine|release`.

Hooks are answered at once and judged in the background. The answer is an empty 200, except
for a PreToolUse of a quarantined thread, which gets a deny.
"""

import json
from collections.abc import Callable

from aiohttp import web

from jev_watchdog.quarantine import Quarantine
from jev_watchdog.surfaces import SurfaceRegistry, TargetError


def create_app(registry: SurfaceRegistry) -> web.Application:
    async def hooks(request: web.Request) -> web.Response:
        body = None
        try:
            payload = json.loads(await request.read())
        except ValueError as exc:
            registry.bad_payload(f"invalid JSON: {exc}")
        else:
            if isinstance(payload, dict):
                try:
                    body = registry.handle(payload)
                except Exception as exc:  # noqa: BLE001 - a bug here must not fail the agent's hook
                    registry.bad_payload(f"handler failed: {exc!r}")
            else:
                registry.bad_payload(f"expected a JSON object, got {type(payload).__name__}")
        return web.Response(status=200) if body is None else web.json_response(body)

    async def listing(request: web.Request) -> web.Response:
        entries = [entry.as_dict() for entry in registry.quarantines.all()]
        return web.json_response({"quarantined": entries})

    def control(action: Callable[[dict], Quarantine]):
        async def handler(request: web.Request) -> web.Response:
            try:
                body = json.loads(await request.read())
            except ValueError:
                body = None
            if not isinstance(body, dict) or not isinstance(body.get("target"), str):
                return web.json_response({"error": 'expected {"target": "..."}'}, status=400)
            try:
                entry = action(body)
            except TargetError as exc:
                return web.json_response({"error": exc.message}, status=exc.status)
            return web.json_response(entry.as_dict())

        return handler

    app = web.Application()
    app.router.add_post("/hooks", hooks)
    app.router.add_get("/quarantine", listing)
    app.router.add_post(
        "/quarantine",
        control(
            lambda body: registry.quarantine(body["target"], str(body.get("reason") or "manual"))
        ),
    )
    app.router.add_post("/release", control(lambda body: registry.release(body["target"])))
    return app
