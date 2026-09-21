import io
import json

import pytest
from conftest import SESSION_ID
from rich.console import Console

from jev_watchdog.core.pack import Question
from jev_watchdog.core.surfaces import ALL_EVENTS, JUDGING_EVENTS, SurfaceRegistry
from jev_watchdog.daemon.server import create_app
from jev_watchdog.display.printer import Printer
from jev_watchdog.judge.fake import FakeJudge


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

    from jev_watchdog.core.pack import load_packs
    from jev_watchdog.judge.registry import JudgeConfig, make_judge

    repo = Path(__file__).resolve().parents[2]
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
    monkeypatch.setattr("jev_watchdog.daemon.server.MAX_BODY_BYTES", 1024)
    client = await aiohttp_client(create_app(registry))
    response = await client.post("/hooks", json={"padding": "x" * 2048})
    assert response.status == 413
    assert registry.stats.errors == {"payload": 1}
    assert "body over 1024 bytes" in registry.printer.console.file.getvalue()


# --- what a dashboard reads: state.add_routes, for the app on the socket only ---------------


def attachable(registry):
    from jev_watchdog.daemon.state import add_routes
    from jev_watchdog.display.feed import Feed

    feed = Feed()
    registry.printer.feed = feed
    app = create_app(registry)
    add_routes(app, registry, feed)
    return app, feed


@pytest.mark.parametrize("path", ["/state", "/records", "/history", "/timeline"])
async def test_the_read_routes_are_not_in_the_app_that_goes_on_the_port(
    aiohttp_client, registry, path
):
    client = await aiohttp_client(create_app(registry))
    assert (await client.get(path)).status == 404


async def test_state_answers_the_snapshot(aiohttp_client, registry, make_payload):
    app, feed = attachable(registry)
    client = await aiohttp_client(app)
    await client.post("/hooks", json=make_payload("SessionStart"))
    state = await (await client.get("/state")).json()
    assert state["boot"] == feed.boot
    assert [thread["label"] for thread in state["threads"]] == [f"{SESSION_ID[:6]}/main"]


async def test_records_are_the_feed_after_a_number(aiohttp_client, registry, make_payload):
    app, feed = attachable(registry)
    client = await aiohttp_client(app)
    await client.post("/hooks", json=make_payload("SessionStart"))
    await client.post("/hooks", json=make_payload("SessionEnd"))
    answer = await (await client.get("/records")).json()
    assert answer["boot"] == feed.boot
    assert [(r["seq"], r["event"]) for r in answer["records"]] == [
        (1, "SessionStart"), (2, "SessionEnd"),
    ]  # fmt: skip
    later = await (await client.get("/records", params={"since": "1"})).json()
    assert [record["seq"] for record in later["records"]] == [2]
    assert (await client.get("/records", params={"since": "x"})).status == 400


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


# --- what the charts are drawn from: /history and /timeline -----------------------------------


@pytest.fixture
def tripping(out_registry=None):
    from conftest import ScriptedJudge

    console = Console(file=io.StringIO(), width=200, color_system=None)
    questions = [Question("exfil", "noul", "i", flag_threshold=0.55, quarantine_ref=0.45,
                          quarantine_limit=0.2)]  # fmt: skip
    judge = ScriptedJudge([{"exfil": 0.55}, {"exfil": 0.95}], name="jev")
    return SurfaceRegistry([judge], questions, Printer(console), enforce=True, transcript_wait_s=0)


async def test_history_is_a_threads_evidence_and_what_happened_to_it(
    aiohttp_client, tripping, make_payload
):
    app, _ = attachable(tripping)
    client = await aiohttp_client(app)
    for tool_use_id in ("t1", "t2"):
        await client.post("/hooks", json=make_payload("PostToolUse", tool_use_id=tool_use_id))
        await tripping.drain()
    await client.post("/release", json={"target": SESSION_ID[:6]})
    response = await client.get("/history", params={"session_id": SESSION_ID, "agent_id": "main"})
    history = await response.json()
    assert [point["shares"] for point in history["judges"]["jev"]] == [
        {"exfil": 0.5}, {"exfil": 3.0}, {"exfil": 0.0},
    ]  # fmt: skip
    assert [mark["kind"] for mark in history["marks"]] == ["quarantine", "release"]
    missing = await client.get("/history", params={"session_id": "nobody", "agent_id": "main"})
    assert missing.status == 404
    await tripping.shutdown()


async def test_timeline_buckets_every_thread_as_the_deciding_judge_saw_it(
    aiohttp_client, tripping, make_payload
):
    app, _ = attachable(tripping)
    client = await aiohttp_client(app)
    await client.post("/hooks", json=make_payload("PostToolUse", tool_use_id="t1"))
    await tripping.drain()
    timeline = await (
        await client.get("/timeline", params={"minutes": "10", "buckets": "5"})
    ).json()
    assert timeline["judge"] == "jev" and timeline["bucket_s"] == 120
    (row,) = timeline["threads"]
    assert row["label"] == f"{SESSION_ID[:6]}/main" and row["cells"][-1] == 0.5
    assert (await client.get("/timeline", params={"minutes": "x"})).status == 400
    # An older or newer dashboard may ask for more than this watchdog gives: it gets the most.
    wide = await (await client.get("/timeline", params={"buckets": "99999"})).json()
    assert len(wide["threads"][0]["cells"]) == 400
    await tripping.shutdown()
