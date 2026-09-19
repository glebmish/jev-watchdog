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
