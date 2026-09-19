import json
from pathlib import Path

from jev_watchdog.cli import DEFAULT_PORT
from jev_watchdog.surfaces import ALL_EVENTS

PLUGIN = Path(__file__).resolve().parent.parent / "plugin"
URL = f"http://127.0.0.1:{DEFAULT_PORT}/hooks"


def handlers() -> dict[str, dict]:
    hooks = json.loads((PLUGIN / "hooks" / "hooks.json").read_text())["hooks"]
    assert all(len(groups) == 1 and len(groups[0]["hooks"]) == 1 for groups in hooks.values())
    return {event: groups[0]["hooks"][0] for event, groups in hooks.items()}


def test_plugin_manifest():
    manifest = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text())
    assert manifest["name"] == "jev-watchdog-hooks"


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
