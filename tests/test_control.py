import asyncio
import io

import pytest
from aiohttp import web
from conftest import SESSION_ID
from rich.console import Console

from jev_watchdog.cli import main
from jev_watchdog.core.pack import Question
from jev_watchdog.core.surfaces import SurfaceRegistry
from jev_watchdog.daemon.server import create_app
from jev_watchdog.display.printer import Printer
from jev_watchdog.judge.fake import FakeJudge


@pytest.fixture
async def registry_with_session(make_payload):
    console = Console(file=io.StringIO(), width=200, color_system=None)
    registry = SurfaceRegistry([FakeJudge()], [Question("exfil", "noul", "i")], Printer(console))
    await registry.handle(make_payload("SessionStart"))
    return registry


async def test_status_quarantine_and_release_against_a_running_server(
    aiohttp_server, registry_with_session, capsys
):
    server = await aiohttp_server(create_app(registry_with_session))

    def run(*argv):
        return asyncio.to_thread(main, [*argv, "--port", str(server.port)])

    target = f"{SESSION_ID[:6]}/main"
    assert await run("status") == 0
    assert "nothing is quarantined" in capsys.readouterr().out
    assert await run("quarantine", SESSION_ID[:6], "--reason", "because") == 0
    assert f"quarantined {target}: because" in capsys.readouterr().out
    assert await run("status") == 0
    status = capsys.readouterr().out
    assert target in status and "because" in status and "manual" in status
    assert await run("release", SESSION_ID[:6]) == 0
    assert f"released {target}" in capsys.readouterr().out
    assert await run("release", SESSION_ID[:6]) == 1
    assert "is not quarantined" in capsys.readouterr().err


async def test_context_against_a_running_server(aiohttp_server, registry_with_session, capsys):
    server = await aiohttp_server(create_app(registry_with_session))

    def run(*argv):
        return asyncio.to_thread(main, [*argv, "--port", str(server.port)])

    target = f"{SESSION_ID[:6]}/main"
    assert await run("context", SESSION_ID[:6], "deploys to staging are expected") == 0
    assert f"context set for {target}" in capsys.readouterr().out
    assert registry_with_session.contexts == {SESSION_ID: "deploys to staging are expected"}
    assert await run("context", SESSION_ID[:6], "--clear") == 0
    assert f"context cleared for {target}" in capsys.readouterr().out
    assert registry_with_session.contexts == {}
    assert await run("context", "nope", "x") == 1
    assert "nope" in capsys.readouterr().err


async def test_something_else_on_the_port_is_named_not_a_traceback(aiohttp_server, capsys):
    async def hello(request):
        return web.Response(text="hello")

    app = web.Application()
    app.router.add_get("/quarantine", hello)
    server = await aiohttp_server(app)

    def run(*argv):
        return asyncio.to_thread(main, [*argv, "--port", str(server.port)])

    assert await run("status") == 1  # 200, but not JSON
    assert f"port {server.port} is not a jev-watchdog" in capsys.readouterr().err
    assert await run("release", "x") == 1  # aiohttp's plain-text 404
    assert f"port {server.port} is not a jev-watchdog" in capsys.readouterr().err


async def test_a_proxy_in_the_environment_is_not_asked_for_localhost(
    aiohttp_server, registry_with_session, capsys, monkeypatch
):
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.delenv("NO_PROXY", raising=False)
    server = await aiohttp_server(create_app(registry_with_session))
    assert await asyncio.to_thread(main, ["status", "--port", str(server.port)]) == 0
    assert "nothing is quarantined" in capsys.readouterr().out


async def test_what_the_watchdog_answers_is_printed_without_control_characters(
    aiohttp_server, registry_with_session, capsys
):
    """reason is whatever was POSTed to /quarantine, and the watched agent can POST."""
    server = await aiohttp_server(create_app(registry_with_session))

    def run(*argv):
        return asyncio.to_thread(main, [*argv, "--port", str(server.port)])

    assert await run("quarantine", SESSION_ID[:6], "--reason", "x\x1b[2Jy") == 0
    assert await run("status") == 0
    assert await run("release", "nope\x1b[2J") == 1
    shown = capsys.readouterr()
    assert "\x1b" not in shown.out + shown.err
    assert shown.out.count("x�[2Jy") == 2
