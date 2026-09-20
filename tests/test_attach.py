import asyncio
import io
import os
from datetime import datetime

import pytest
from aiohttp import web
from conftest import SESSION_ID
from rich.console import Console

from jev_watchdog.attach import AttachClient, AttachError, ControlError
from jev_watchdog.feed import Feed
from jev_watchdog.judge.fake import FakeJudge
from jev_watchdog.printer import Printer
from jev_watchdog.server import create_app
from jev_watchdog.state import DaemonInfo
from jev_watchdog.surfaces import SurfaceRegistry


@pytest.fixture
async def watchdog(socket_path):
    feed = Feed()
    registry = SurfaceRegistry([FakeJudge()], [], Printer(Console(file=io.StringIO()), feed=feed))
    info = DaemonInfo(feed.boot, os.getpid(), datetime(2026, 9, 20, 9, 0, 0), 8787, None)
    runner = web.AppRunner(create_app(registry, feed, info))
    await runner.setup()
    await web.UnixSite(runner, str(socket_path)).start()
    yield registry
    feed.close()
    await runner.cleanup()
    await registry.shutdown()


@pytest.fixture
async def client(socket_path):
    client = AttachClient(socket_path)
    yield client
    await client.aclose()


async def test_state_and_control_go_over_the_socket(watchdog, client, make_payload):
    await watchdog.handle(make_payload("SessionStart"))
    label = f"{SESSION_ID[:6]}/main"
    assert [thread["label"] for thread in (await client.state())["threads"]] == [label]
    assert (await client.quarantine(label, "looks odd"))["reason"] == "looks odd"
    assert (await client.state())["threads"][0]["quarantine"]["reason"] == "looks odd"
    assert (await client.release(label))["target"] == label
    assert (await client.context(label, "a known upload"))["context"] == "a known upload"
    assert (await client.state())["threads"][0]["context"] == "a known upload"


async def test_the_daemons_no_is_a_control_error_with_its_message(watchdog, client):
    with pytest.raises(ControlError, match="no agent thread matches 'nobody'"):
        await client.release("nobody")


async def test_events_are_the_hello_the_backlog_and_then_what_happens(watchdog, client):
    watchdog.printer.note("-", "before")
    stream = client.events()
    name, hello = await anext(stream)
    assert name == "hello" and hello["boot"] == watchdog.printer.feed.boot
    assert (await anext(stream))[1]["message"] == "before"
    watchdog.printer.note("-", "after")
    name, record = await asyncio.wait_for(anext(stream), 2)
    assert (name, record["message"], record["seq"]) == ("record", "after", 2)
    await stream.aclose()

    resumed = client.events(since=1)
    await anext(resumed)
    assert (await anext(resumed))[1]["message"] == "after"
    await resumed.aclose()


async def test_the_stream_ends_when_the_watchdog_closes_it(watchdog, client):
    stream = client.events()
    await anext(stream)
    watchdog.printer.feed.close()
    assert [item async for item in stream] == []


async def test_nothing_to_connect_to_is_an_attach_error(socket_path):
    client = AttachClient(socket_path)
    with pytest.raises(AttachError):
        await client.state()
    with pytest.raises(AttachError):
        await anext(client.events())
    await client.aclose()
