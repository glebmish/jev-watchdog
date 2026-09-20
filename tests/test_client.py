import io

import pytest
from aiohttp import web
from conftest import SESSION_ID
from rich.console import Console

from jev_watchdog.client import Client, NotAWatchdog, Refused, Unreachable
from jev_watchdog.feed import Feed
from jev_watchdog.judge.fake import FakeJudge
from jev_watchdog.printer import Printer
from jev_watchdog.server import create_app
from jev_watchdog.state import add_routes
from jev_watchdog.surfaces import SurfaceRegistry


@pytest.fixture
async def watchdog(socket_path):
    """A watchdog's private side: the control app plus what a dashboard reads, on a socket."""
    feed = Feed()
    registry = SurfaceRegistry([FakeJudge()], [], Printer(Console(file=io.StringIO()), feed=feed))
    app = create_app(registry)
    add_routes(app, registry, feed)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.UnixSite(runner, str(socket_path)).start()
    yield registry
    await runner.cleanup()
    await registry.shutdown()


@pytest.fixture
async def client(socket_path):
    async with Client.on_socket(socket_path) as client:
        yield client


async def test_state_and_control_go_over_the_socket(watchdog, client, make_payload):
    await watchdog.handle(make_payload("SessionStart"))
    label = f"{SESSION_ID[:6]}/main"
    assert [thread["label"] for thread in (await client.state())["threads"]] == [label]
    assert (await client.quarantine(label, "looks odd"))["reason"] == "looks odd"
    assert [entry["target"] for entry in await client.quarantined()] == [label]
    assert (await client.state())["threads"][0]["quarantine"]["reason"] == "looks odd"
    assert (await client.release(label))["target"] == label
    assert (await client.context(label, "a known upload"))["context"] == "a known upload"
    assert (await client.state())["threads"][0]["context"] == "a known upload"


async def test_the_watchdogs_no_is_a_refusal_with_its_message(watchdog, client):
    with pytest.raises(Refused, match="no agent thread matches 'nobody'"):
        await client.release("nobody")


async def test_records_are_what_came_after_a_number_with_the_boot_id(watchdog, client):
    watchdog.printer.note("-", "before")
    answer = await client.records()
    assert answer["boot"] == watchdog.printer.feed.boot
    assert [record["message"] for record in answer["records"]] == ["before"]
    watchdog.printer.note("-", "after")
    assert [r["message"] for r in (await client.records(since=1))["records"]] == ["after"]
    assert (await client.records(since=2))["records"] == []


async def test_the_charts_data_comes_over_the_socket(watchdog, client, make_payload):
    await watchdog.handle(make_payload("SessionStart"))
    assert await client.history(SESSION_ID, "main") == {"judges": {}, "marks": []}
    with pytest.raises(Refused, match="no such agent thread"):
        await client.history("nobody", "main")
    timeline = await client.timeline(minutes=10, buckets=5, judge="fake")
    assert timeline["judge"] == "fake" and timeline["threads"] == []


async def test_nothing_to_connect_to_is_unreachable(socket_path):
    async with Client.on_socket(socket_path) as client:
        with pytest.raises(Unreachable):
            await client.state()


async def test_the_port_serves_control_but_not_what_a_dashboard_reads(aiohttp_server, watchdog):
    server = await aiohttp_server(create_app(watchdog))
    async with Client.on_port(server.port) as client:
        assert await client.quarantined() == []
        with pytest.raises(NotAWatchdog):  # aiohttp's own plain-text 404
            await client.state()
