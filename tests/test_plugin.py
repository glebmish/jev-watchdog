import asyncio
import io
import json
import shutil
from pathlib import Path

import pytest
from rich.console import Console

from jev_watchdog.cli import DEFAULT_PORT
from jev_watchdog.judge.fake import FakeJudge
from jev_watchdog.printer import Printer
from jev_watchdog.server import create_app
from jev_watchdog.surfaces import ALL_EVENTS, SurfaceRegistry

PLUGIN = Path(__file__).resolve().parent.parent / "plugin"
URL = f"http://127.0.0.1:{DEFAULT_PORT}/hooks"


def handlers() -> dict[str, dict]:
    hooks = json.loads((PLUGIN / "hooks" / "hooks.json").read_text())["hooks"]
    assert all(len(groups) == 1 and len(groups[0]["hooks"]) == 1 for groups in hooks.values())
    return {event: groups[0]["hooks"][0] for event, groups in hooks.items()}


def test_plugin_manifest():
    manifest = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text())
    assert manifest["name"] == "jev-watchdog-hooks"
    # The PreToolUse hook can reject tool calls: whoever installs this must not read otherwise.
    assert "PreToolUse" in manifest["description"]
    assert "observe-only" not in manifest["description"].lower()


def test_hooks_cover_exactly_the_handled_events():
    assert set(handlers()) == ALL_EVENTS


def test_all_but_session_start_are_http_hooks_to_the_default_port():
    for event, handler in handlers().items():
        if event == "SessionStart":
            continue
        assert handler == {"type": "http", "url": URL, "timeout": 2}, event


def test_session_start_is_a_silent_async_command_hook():
    handler = handlers()["SessionStart"]  # SessionStart does not support http handlers
    assert handler["type"] == "command" and handler["async"] is True
    command = handler["command"]
    assert URL in command and "--data-binary @-" in command
    assert "-o /dev/null" in command and command.endswith("|| true")


@pytest.mark.skipif(shutil.which("curl") is None, reason="needs curl")
async def test_the_session_start_command_is_a_client_the_server_accepts(
    aiohttp_server, make_payload, monkeypatch
):
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")  # curl must not ask it for localhost
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.delenv("NO_PROXY", raising=False)
    registry = SurfaceRegistry([FakeJudge()], [], Printer(Console(file=io.StringIO())))
    server = await aiohttp_server(create_app(registry))
    command = handlers()["SessionStart"]["command"].replace(f":{DEFAULT_PORT}/", f":{server.port}/")
    process = await asyncio.create_subprocess_shell(command, stdin=asyncio.subprocess.PIPE)
    await process.communicate(json.dumps(make_payload("SessionStart")).encode())
    assert registry.stats.events == {"SessionStart": 1}
    assert registry.stats.errors == {}
