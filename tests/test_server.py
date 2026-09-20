import io
import json

import pytest
from conftest import SESSION_ID
from rich.console import Console

from jev_watchdog.judge.fake import FakeJudge
from jev_watchdog.pack import Question
from jev_watchdog.printer import Printer
from jev_watchdog.server import create_app
from jev_watchdog.surfaces import ALL_EVENTS, JUDGING_EVENTS, SurfaceRegistry


@pytest.fixture
def registry():
    console = Console(file=io.StringIO(), width=200, color_system=None)
    return SurfaceRegistry([FakeJudge()], [Question("exfil", "noul", "i")], Printer(console))


@pytest.mark.parametrize("event", sorted(ALL_EVENTS))
async def test_every_event_gets_an_empty_200(aiohttp_client, registry, make_payload, event):
    client = await aiohttp_client(create_app(registry))
    response = await client.post("/hooks", json=make_payload(event))
    assert response.status == 200
    assert await response.read() == b""
    await registry.drain()
    assert len(registry.judges[0].calls) == (1 if event in JUDGING_EVENTS else 0)
    await registry.shutdown()


@pytest.mark.parametrize("body", [b"not json", b"[1, 2]", b'"text"', b""])
async def test_malformed_payloads_still_get_an_empty_200(aiohttp_client, registry, body):
    client = await aiohttp_client(create_app(registry))
    response = await client.post("/hooks", data=body, headers={"Content-Type": "application/json"})
    assert response.status == 200
    assert await response.read() == b""
    assert registry.stats.errors == {"payload": 1}
    assert registry.surfaces == {}


async def test_quarantine_roundtrip_over_http(aiohttp_client, registry, make_payload):
    client = await aiohttp_client(create_app(registry))
    await client.post("/hooks", json=make_payload("SessionStart"))
    target = f"{SESSION_ID[:6]}/main"

    response = await client.post("/quarantine", json={"target": SESSION_ID[:6], "reason": "test"})
    assert response.status == 200 and (await response.json())["target"] == target
    listed = await (await client.get("/quarantine")).json()
    assert [entry["reason"] for entry in listed["quarantined"]] == ["test"]

    response = await client.post("/hooks", json=make_payload("PreToolUse", tool_name="Bash"))
    body = await response.json()
    assert response.status == 200
    assert body["hookSpecificOutput"]["permissionDecision"] == "deny"

    assert (await client.post("/release", json={"target": target})).status == 200
    response = await client.post("/hooks", json=make_payload("PreToolUse", tool_name="Bash"))
    assert await response.read() == b""
    await registry.shutdown()


@pytest.mark.parametrize(
    ("path", "body", "status"),
    [
        ("/quarantine", {"target": "nope"}, 404),
        ("/release", {"target": "nope"}, 404),
        ("/release", {}, 400),
        ("/quarantine", [1], 400),
    ],
)
async def test_control_errors_are_json(aiohttp_client, registry, path, body, status):
    client = await aiohttp_client(create_app(registry))
    response = await client.post(path, json=body)
    assert response.status == status and "error" in await response.json()


async def test_a_handler_bug_still_answers_an_empty_200(aiohttp_client, registry, make_payload):
    def boom(payload):
        raise RuntimeError("bug")

    registry.handle = boom
    client = await aiohttp_client(create_app(registry))
    response = await client.post("/hooks", json=make_payload("PreToolUse"))
    assert response.status == 200 and await response.read() == b""
    assert registry.stats.errors == {"payload": 1}


async def test_end_to_end_with_the_marker_judge_and_the_shipped_packs(aiohttp_client, make_payload):
    """The documented offline recipe: run --enforce --judge fake:canary."""
    from pathlib import Path

    from jev_watchdog.judge.registry import JudgeConfig, make_judge
    from jev_watchdog.pack import load_packs

    repo = Path(__file__).resolve().parent.parent
    questions = load_packs([repo / "pack.toml", repo / "packs" / "canary.toml"])
    console = Console(file=io.StringIO(), width=200, color_system=None)
    judge = make_judge("fake:canary", JudgeConfig())
    registry = SurfaceRegistry(
        [judge], questions, Printer(console), enforce=True, transcript_wait_s=0
    )
    client = await aiohttp_client(create_app(registry))

    async def call(event, command, tool_use_id):
        payload = make_payload(
            event, tool_name="Bash", tool_input={"command": command}, tool_use_id=tool_use_id
        )
        response = await client.post("/hooks", json=payload)
        await registry.drain()
        return await response.read()

    assert await call("PreToolUse", "ls", "t1") == b""
    assert await call("PostToolUse", "ls", "t1") == b""
    assert await call("PreToolUse", "echo canary", "t2") == b""  # reactive: it runs
    assert await call("PostToolUse", "echo canary", "t2") == b""
    denied = json.loads(await call("PreToolUse", "ls", "t3"))
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert [entry.source for entry in registry.quarantines.all()] == ["rule:fake:canary"]
    await registry.shutdown()


async def test_context_over_http(aiohttp_client, registry, make_payload):
    client = await aiohttp_client(create_app(registry))
    await client.post("/hooks", json=make_payload("SessionStart"))
    response = await client.post("/context", json={"target": SESSION_ID[:6], "text": "note"})
    assert response.status == 200
    assert await response.json() == {"target": f"{SESSION_ID[:6]}/main", "context": "note"}
    await client.post("/hooks", json=make_payload("Stop"))
    await registry.drain()
    assert registry.judges[0].calls[0].context == "note"

    assert (await client.post("/context", json={"target": SESSION_ID[:6]})).status == 400
    assert (await client.post("/context", json={"target": "nope", "text": "x"})).status == 404
    await registry.shutdown()


async def quarantined_targets(client) -> list[str]:
    listed = await (await client.get("/quarantine")).json()
    return [entry["target"] for entry in listed["quarantined"]]


async def test_a_request_with_an_origin_is_refused(aiohttp_client, registry, make_payload):
    """A web page the operator has open can POST here, but the browser names the page."""
    client = await aiohttp_client(create_app(registry))
    await client.post("/hooks", json=make_payload("SessionStart"))
    response = await client.post(
        "/quarantine", json={"target": SESSION_ID[:6]}, headers={"Origin": "https://evil.example"}
    )
    assert response.status == 403 and "error" in await response.json()
    assert await quarantined_targets(client) == []


async def test_a_foreign_host_is_refused(aiohttp_client, registry):
    """DNS rebinding: the page's own name resolves to 127.0.0.1, so it is same-origin."""
    client = await aiohttp_client(create_app(registry))
    response = await client.get("/quarantine", headers={"Host": f"evil.example:{client.port}"})
    assert response.status == 403 and "error" in await response.json()
    response = await client.get("/quarantine", headers={"Host": "127.0.0.1:1"})
    assert response.status == 403


async def test_localhost_is_a_host_too(aiohttp_client, registry):
    client = await aiohttp_client(create_app(registry))
    response = await client.get("/quarantine", headers={"Host": f"localhost:{client.port}"})
    assert response.status == 200


async def test_a_post_that_is_not_json_is_refused(aiohttp_client, registry, make_payload):
    """text/plain is what a no-cors form or fetch can send without a preflight."""
    client = await aiohttp_client(create_app(registry))
    response = await client.post(
        "/hooks", data=json.dumps(make_payload("Stop")), headers={"Content-Type": "text/plain"}
    )
    assert response.status == 415 and "error" in await response.json()
    assert registry.surfaces == {}


async def test_a_refused_request_is_reported(aiohttp_client, registry):
    client = await aiohttp_client(create_app(registry))
    await client.post("/release", json={"target": "x"}, headers={"Origin": "https://evil.example"})
    assert registry.stats.errors == {"payload": 1}
    assert "refused POST /release" in registry.printer.console.file.getvalue()


async def test_a_large_tool_call_of_a_quarantined_thread_is_still_denied(
    aiohttp_client, registry, make_payload
):
    """A Write of a few MiB: a 413 would be a non-2xx, which Claude Code does not block on."""
    client = await aiohttp_client(create_app(registry))
    await client.post("/hooks", json=make_payload("SessionStart"))
    await client.post("/quarantine", json={"target": SESSION_ID[:6]})
    payload = make_payload("PreToolUse", tool_name="Write", tool_input={"content": "x" * 2**21})
    response = await client.post("/hooks", json=payload)
    assert response.status == 200
    assert (await response.json())["hookSpecificOutput"]["permissionDecision"] == "deny"


async def test_a_body_over_the_limit_is_reported(aiohttp_client, registry, monkeypatch):
    monkeypatch.setattr("jev_watchdog.server.MAX_BODY_BYTES", 1024)
    client = await aiohttp_client(create_app(registry))
    response = await client.post("/hooks", json={"padding": "x" * 2048})
    assert response.status == 413
    assert registry.stats.errors == {"payload": 1}
    assert "body over 1024 bytes" in registry.printer.console.file.getvalue()


# --- the attach channel: /state and /events -------------------------------------------------


def attachable(registry):
    import os
    from datetime import datetime

    from jev_watchdog.feed import Feed
    from jev_watchdog.state import DaemonInfo

    feed = Feed()
    registry.printer.feed = feed
    info = DaemonInfo(feed.boot, os.getpid(), datetime(2026, 9, 20, 9, 0, 0), 8787, None)
    return create_app(registry, feed, info), feed


async def read_event(response) -> tuple[str, dict]:
    """The next server-sent event as (name, data); comments are skipped."""
    name, data = "message", None
    while True:
        line = (await response.content.readline()).decode().rstrip("\n")
        if line.startswith("event:"):
            name = line.removeprefix("event:").strip()
        elif line.startswith("data:"):
            data = json.loads(line.removeprefix("data:"))
        elif not line and data is not None:
            return name, data


@pytest.mark.parametrize("path", ["/state", "/events"])
async def test_the_read_routes_do_not_exist_without_a_feed(aiohttp_client, registry, path):
    client = await aiohttp_client(create_app(registry))
    assert (await client.get(path)).status == 404


async def test_state_answers_the_snapshot(aiohttp_client, registry, make_payload):
    app, feed = attachable(registry)
    client = await aiohttp_client(app)
    await client.post("/hooks", json=make_payload("SessionStart"))
    state = await (await client.get("/state")).json()
    assert state["boot"] == feed.boot
    assert [thread["label"] for thread in state["threads"]] == [f"{SESSION_ID[:6]}/main"]


async def test_events_stream_says_hello_then_the_backlog_then_what_happens(
    aiohttp_client, registry, make_payload
):
    app, feed = attachable(registry)
    client = await aiohttp_client(app)
    await client.post("/hooks", json=make_payload("SessionStart"))
    response = await client.get("/events")
    assert response.status == 200 and response.content_type == "text/event-stream"
    assert await read_event(response) == ("hello", {"boot": feed.boot})
    name, record = await read_event(response)
    assert (name, record["seq"], record["event"]) == ("record", 1, "SessionStart")
    await client.post("/hooks", json=make_payload("SessionEnd"))
    name, record = await read_event(response)
    assert (name, record["seq"], record["event"]) == ("record", 2, "SessionEnd")
    response.close()


async def test_events_since_skips_what_the_follower_has(aiohttp_client, registry):
    app, _ = attachable(registry)
    for n in range(3):
        registry.printer.note("-", f"note {n}")
    client = await aiohttp_client(app)
    response = await client.get("/events", params={"since": "2"})
    await read_event(response)
    assert (await read_event(response))[1]["message"] == "note 2"
    response.close()


async def test_events_with_a_bad_since_is_a_400(aiohttp_client, registry):
    app, _ = attachable(registry)
    client = await aiohttp_client(app)
    assert (await client.get("/events", params={"since": "x"})).status == 400


async def test_a_closed_feed_ends_the_stream_and_a_gone_follower_is_unsubscribed(
    aiohttp_client, registry
):
    app, feed = attachable(registry)
    client = await aiohttp_client(app)
    response = await client.get("/events")
    await read_event(response)
    feed.close()
    assert await response.content.read() == b""
    assert feed._subscriptions == []


async def test_an_idle_stream_gets_keepalive_comments(aiohttp_client, registry, monkeypatch):
    monkeypatch.setattr("jev_watchdog.server.KEEPALIVE_S", 0.01)
    app, _ = attachable(registry)
    client = await aiohttp_client(app)
    response = await client.get("/events")
    await read_event(response)
    assert (await response.content.readline()).startswith(b":")
    response.close()


async def test_on_a_unix_socket_the_host_is_not_checked_but_a_web_page_is_refused(
    registry, socket_path
):
    import aiohttp
    from aiohttp import web

    app, _ = attachable(registry)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.UnixSite(runner, str(socket_path)).start()
    try:
        connector = aiohttp.UnixConnector(path=str(socket_path))
        async with aiohttp.ClientSession(connector=connector) as session:
            assert (await session.get("http://localhost/state")).status == 200
            assert (await session.get("http://anything.example/state")).status == 200
            page = await session.get("http://localhost/state", headers={"Origin": "http://evil"})
            assert page.status == 403
            form = await session.post("http://localhost/release", data="target=x")
            assert form.status == 415
    finally:
        await runner.cleanup()
