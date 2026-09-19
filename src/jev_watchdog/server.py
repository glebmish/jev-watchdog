"""The hook endpoint. Observe-only: every request gets an empty 200."""

import json

from aiohttp import web

from jev_watchdog.surfaces import SurfaceRegistry


def create_app(registry: SurfaceRegistry) -> web.Application:
    async def hooks(request: web.Request) -> web.Response:
        try:
            payload = json.loads(await request.read())
        except ValueError as exc:
            registry.bad_payload(f"invalid JSON: {exc}")
        else:
            if isinstance(payload, dict):
                registry.handle(payload)
            else:
                registry.bad_payload(f"expected a JSON object, got {type(payload).__name__}")
        return web.Response(status=200)

    app = web.Application()
    app.router.add_post("/hooks", hooks)
    return app
