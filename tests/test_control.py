import asyncio
import io

import pytest
from conftest import SESSION_ID
from rich.console import Console

from jev_watchdog.cli import main
from jev_watchdog.judge.fake import FakeJudge
from jev_watchdog.pack import Question
from jev_watchdog.printer import Printer
from jev_watchdog.server import create_app
from jev_watchdog.surfaces import SurfaceRegistry


@pytest.fixture
def registry_with_session(make_payload):
    console = Console(file=io.StringIO(), width=200, color_system=None)
    registry = SurfaceRegistry([FakeJudge()], [Question("exfil", "noul", "i")], Printer(console))
    registry.handle(make_payload("SessionStart"))
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
