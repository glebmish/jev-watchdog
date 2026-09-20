import asyncio
import io
import os
import signal
import socket
import stat

import aiohttp
from rich.console import Console

from jev_watchdog.feed import Feed
from jev_watchdog.judge.fake import FakeJudge
from jev_watchdog.printer import Printer
from jev_watchdog.serve import dashboard, serve
from jev_watchdog.surfaces import SurfaceRegistry


def watchdog():
    """A registry, its printer and feed, and a free port."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    feed, out = Feed(), io.StringIO()
    printer = Printer(Console(file=out, width=200, color_system=None), feed=feed)
    return SurfaceRegistry([FakeJudge()], [], printer), printer, feed, port, out


async def test_port_in_use_exits_1_without_a_summary(socket_path, capsys):
    registry, printer, feed, _, out = watchdog()
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        port = busy.getsockname()[1]
        assert await serve(registry, printer, port, "banner", feed, socket_path) == 1
    assert "cannot listen on 127.0.0.1" in capsys.readouterr().err
    assert out.getvalue() == ""


async def test_the_socket_serves_state_and_the_port_does_not(socket_path):
    registry, printer, feed, port, _ = watchdog()
    socket_path.write_text("stale")  # left by a watchdog that was killed
    seen = {}

    async def look(stop) -> None:
        seen["mode"] = stat.S_IMODE(socket_path.stat().st_mode)
        connector = aiohttp.UnixConnector(path=str(socket_path))
        async with aiohttp.ClientSession(connector=connector) as session:
            seen["socket"] = (await session.get("http://localhost/state")).status
            seen["boot"] = (await (await session.get("http://localhost/state")).json())["boot"]
        async with aiohttp.ClientSession() as session:
            seen["tcp"] = (await session.get(f"http://127.0.0.1:{port}/state")).status
            seen["tcp control"] = (await session.get(f"http://127.0.0.1:{port}/quarantine")).status

    assert await serve(registry, printer, port, "banner", feed, socket_path, look) == 0
    assert seen == {"mode": 0o600, "socket": 200, "boot": feed.boot, "tcp": 404,
                    "tcp control": 200}  # fmt: skip
    assert not socket_path.exists()


async def test_without_a_socket_run_carries_on_but_a_dashboard_cannot(socket_path, capsys):
    registry, printer, feed, port, _ = watchdog()
    too_long = socket_path.parent / ("x" * 120)
    ran = []

    async def look(stop) -> None:
        ran.append(True)

    assert await serve(registry, printer, port, "banner", feed, too_long, look) == 1
    assert ran == [] and "attach unavailable" in capsys.readouterr().err

    registry, printer, feed, port, _ = watchdog()
    asyncio.get_running_loop().call_later(0.05, os.kill, os.getpid(), signal.SIGTERM)
    assert await serve(registry, printer, port, "banner", feed, too_long) == 0
    assert "attach unavailable" in capsys.readouterr().err


async def test_run_tui_serves_hooks_under_the_dashboard_until_q(socket_path, make_payload):
    registry, printer, feed, port, _ = watchdog()
    seen = {}

    async def operator(pilot) -> None:
        async with aiohttp.ClientSession() as session:
            await session.post(f"http://127.0.0.1:{port}/hooks", json=make_payload("SessionStart"))
        for _ in range(200):
            if pilot.app.selected_label:
                break
            await pilot.pause(0.01)
        seen["selected"] = pilot.app.selected_label
        await pilot.press("q")

    console = Console(file=io.StringIO())
    shown = dashboard(socket_path, printer, console, headless=True, auto_pilot=operator)
    assert await serve(registry, printer, port, "banner", feed, socket_path, shown) == 0
    assert seen == {"selected": "012345/main"}
    assert printer.console is console  # given back for the closing summary


async def test_run_tui_stops_on_a_signal(socket_path):
    registry, printer, feed, port, _ = watchdog()
    asyncio.get_running_loop().call_later(0.3, os.kill, os.getpid(), signal.SIGTERM)
    shown = dashboard(socket_path, printer, Console(file=io.StringIO()), headless=True)
    served = serve(registry, printer, port, "banner", feed, socket_path, shown)
    assert await asyncio.wait_for(served, 5) == 0
