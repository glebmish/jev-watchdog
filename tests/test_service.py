import plistlib
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from jev_watchdog.cli import build_parser
from jev_watchdog.service import (
    LABEL,
    ServiceError,
    install,
    launchd_plist,
    run_arguments,
    systemd_unit,
    uninstall,
)

REPO = Path(__file__).resolve().parent.parent
PACK = str(REPO / "pack.toml")


def parsed(*argv: str):
    return build_parser().parse_args(["install", *argv])


class Runner:
    """Stands in for subprocess.run: records the commands, fails the ones it is told to."""

    def __init__(self, failing: tuple[str, ...] = ()) -> None:
        self.commands, self.failing = [], failing

    def __call__(self, argv, **_):
        self.commands.append(argv)
        failed = any(word in argv for word in self.failing)
        return SimpleNamespace(returncode=5 if failed else 0, stdout="", stderr="it broke\n")


@pytest.fixture
def home(tmp_path):
    return tmp_path / "home"


def do_install(home, runner, *argv, platform="darwin", env=None, out=None):
    env = {"PATH": "/opt/bin:/usr/bin", "XDG_STATE_HOME": str(home / "state")} | (env or {})
    lines = [] if out is None else out
    code = install(
        parsed(*argv), platform=platform, home=home, env=env, uid=501, run=runner,
        out=lines.append, wait_s=0,
    )  # fmt: skip
    return code, lines


def test_run_arguments_are_absolute_and_log_to_the_state_dir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    args = parsed("--judge", "fake", "--pack", "pack.toml", "--pack", "packs/canary.toml",
                  "--enforce", "--context", "a known upload", "--port", "9000")  # fmt: skip
    assert run_arguments(args, Path("/state")) == [
        "run", "--port", "9000", "--transcript-wait", "2.0", "--enforce", "--judge", "fake",
        "--pack", str(tmp_path / "pack.toml"), "--pack", str(tmp_path / "packs/canary.toml"),
        "--context", "a known upload", "--log-dir", "/state/runs",
    ]  # fmt: skip


def test_run_arguments_keep_what_was_asked_about_logs_keys_and_waiting(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    args = parsed("--key-file", "key", "--log", "one.jsonl", "--transcript-wait", "0.5",
                  "--claude-thinking", "--judge", "jev", "--judge", "claude:claude-haiku-4-5")  # fmt: skip
    arguments = run_arguments(args, Path("/state"))
    assert arguments[-2:] == ["--log", str(tmp_path / "one.jsonl")]
    assert "--log-dir" not in arguments
    assert ["--key-file", str(tmp_path / "key")] == arguments[-4:-2]
    assert "--claude-thinking" in arguments and ["--transcript-wait", "0.5"] == arguments[3:5]
    args = parsed("--judge", "fake", "--log-dir", "logs")
    assert run_arguments(args, Path("/state"))[-2:] == ["--log-dir", str(tmp_path / "logs")]
    assert "--key-file" not in run_arguments(args, Path("/state"))  # no jev judge: no key


def test_the_plist_restarts_a_crash_not_a_stop_and_carries_the_path():
    plist = plistlib.loads(
        launchd_plist(["/py", "-m", "x", "run"], "/opt/bin", Path("/s/d.log"), Path("/st"))
    )
    assert plist["Label"] == LABEL
    assert plist["ProgramArguments"] == ["/py", "-m", "x", "run"]
    assert plist["EnvironmentVariables"] == {"PATH": "/opt/bin", "XDG_STATE_HOME": "/st"}
    assert plist["RunAtLoad"] is True and plist["KeepAlive"] == {"SuccessfulExit": False}
    assert plist["ThrottleInterval"] == 10
    assert plist["StandardOutPath"] == plist["StandardErrorPath"] == "/s/d.log"
    assert "ProcessType" not in plist  # Background would throttle what answers the hooks


def test_the_unit_restarts_on_failure_and_escapes_what_systemd_expands():
    unit = systemd_unit(
        ["/py", "run", "--context", '100% of $HOME\'s "files"'], "/opt/bin:/b%n", Path("/st")
    )
    assert "Restart=on-failure" in unit and "RestartSec=10" in unit
    assert "WantedBy=default.target" in unit
    assert 'ExecStart=/py run --context "100%% of $$HOME\'s \\"files\\""' in unit
    assert 'Environment="PATH=/opt/bin:/b%%n"' in unit


def test_a_newline_cannot_go_into_a_unit():
    with pytest.raises(ServiceError, match="line break"):
        systemd_unit(["/py", "run", "--context", "two\nlines"], "/bin", Path("/st"))


def test_install_on_macos_writes_a_private_plist_and_bootstraps_it(home):
    runner = Runner()
    code, lines = do_install(home, runner, "--judge", "fake", "--pack", PACK)
    path = home / "Library/LaunchAgents" / f"{LABEL}.plist"
    assert code == 0 and stat.S_IMODE(path.stat().st_mode) == 0o600
    plist = plistlib.loads(path.read_bytes())
    assert plist["ProgramArguments"][:4] == [sys.executable, "-m", "jev_watchdog.cli", "run"]
    assert plist["EnvironmentVariables"]["PATH"] == "/opt/bin:/usr/bin"
    assert plist["StandardOutPath"] == str(home / "state/jev-watchdog/daemon.log")
    assert runner.commands == [["launchctl", "bootstrap", "gui/501", str(path)]]
    assert any("jev-watchdog attach" in line for line in lines)


def test_install_over_an_install_stops_the_old_one_first(home):
    do_install(home, Runner(), "--judge", "fake", "--pack", PACK)
    runner = Runner()
    code, _ = do_install(home, runner, "--judge", "fake", "--pack", PACK, "--enforce")
    assert code == 0
    assert [command[1] for command in runner.commands] == ["bootout", "bootstrap"]
    assert runner.commands[0] == ["launchctl", "bootout", f"gui/501/{LABEL}"]


def test_install_on_linux_writes_the_unit_and_enables_it(home):
    runner = Runner()
    code, _ = do_install(home, runner, "--judge", "fake", "--pack", PACK, platform="linux")
    unit = home / ".config/systemd/user/jev-watchdog.service"
    assert code == 0 and stat.S_IMODE(unit.stat().st_mode) == 0o600
    assert f"ExecStart={sys.executable} -m jev_watchdog.cli run" in unit.read_text()
    assert runner.commands == [
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "jev-watchdog.service"],
        ["systemctl", "--user", "restart", "jev-watchdog.service"],
    ]


def test_a_failing_service_manager_is_reported_and_the_unit_kept(home):
    code, lines = do_install(
        home, Runner(failing=("bootstrap",)), "--judge", "fake", "--pack", PACK
    )
    assert code == 1
    assert (home / "Library/LaunchAgents" / f"{LABEL}.plist").exists()
    said = "\n".join(lines)
    assert "it broke" in said and "launchctl bootstrap" in said and "left in place" in said


def test_the_key_is_never_written_and_an_environment_key_is_refused(home, tmp_path):
    runner = Runner()
    env = {"TYPESAFE_API_KEY": "sk-secret"}
    missing = str(tmp_path / "no-key")
    code, lines = do_install(home, runner, "--pack", PACK, "--key-file", missing, env=env)
    assert code == 1 and runner.commands == []
    assert "--key-file" in "\n".join(lines) and "sk-secret" not in "\n".join(lines)

    key_file = tmp_path / "key"
    key_file.write_text("sk-secret\n")
    code, _ = do_install(home, runner, "--pack", PACK, "--key-file", str(key_file), env=env)
    assert code == 0
    written = (home / "Library/LaunchAgents" / f"{LABEL}.plist").read_text()
    assert "sk-secret" not in written and str(key_file) in written


def test_what_run_would_refuse_is_refused_before_anything_is_written(home, tmp_path):
    runner = Runner()
    code, lines = do_install(home, runner, "--judge", "fake", "--pack", str(tmp_path / "none.toml"))
    assert code == 1 and "cannot load pack" in "\n".join(lines)
    code, lines = do_install(home, runner, "--judge", "fake", "--judge", "fake", "--pack", PACK)
    assert code == 1 and "more than once" in "\n".join(lines)
    code, lines = do_install(home, runner, "--judge", "fake", "--pack", PACK, platform="win32")
    assert code == 1 and "macOS and Linux" in "\n".join(lines)
    assert runner.commands == [] and not (home / "Library").exists()


def test_install_does_not_take_tui():
    with pytest.raises(SystemExit):
        parsed("--tui")


def test_uninstall_stops_and_removes_and_is_calm_when_there_is_nothing(home):
    do_install(home, Runner(), "--judge", "fake", "--pack", PACK)
    runner, lines = Runner(), []
    assert uninstall(platform="darwin", home=home, uid=501, run=runner, out=lines.append) == 0
    assert runner.commands == [["launchctl", "bootout", f"gui/501/{LABEL}"]]
    assert not (home / "Library/LaunchAgents" / f"{LABEL}.plist").exists()

    runner, lines = Runner(), []
    assert uninstall(platform="darwin", home=home, uid=501, run=runner, out=lines.append) == 0
    assert runner.commands == [] and "nothing is installed" in "\n".join(lines)


def test_uninstall_on_linux_disables_the_unit(home):
    do_install(home, Runner(), "--judge", "fake", "--pack", PACK, platform="linux")
    runner = Runner()
    assert uninstall(platform="linux", home=home, uid=501, run=runner, out=lambda _: None) == 0
    assert runner.commands == [
        ["systemctl", "--user", "disable", "--now", "jev-watchdog.service"],
        ["systemctl", "--user", "daemon-reload"],
    ]
    assert not (home / ".config/systemd/user/jev-watchdog.service").exists()


# --- found in review --------------------------------------------------------------------------


def test_the_unit_pins_the_state_dir_so_service_and_attach_agree_on_the_socket(home):
    """A service does not see an XDG_STATE_HOME exported from a shell profile."""
    do_install(home, Runner(), "--judge", "fake", "--pack", PACK)
    plist = plistlib.loads((home / "Library/LaunchAgents" / f"{LABEL}.plist").read_bytes())
    assert plist["EnvironmentVariables"]["XDG_STATE_HOME"] == str(home / "state")
    do_install(home, Runner(), "--judge", "fake", "--pack", PACK, platform="linux")
    unit = (home / ".config/systemd/user/jev-watchdog.service").read_text()
    assert f'Environment="XDG_STATE_HOME={home / "state"}"' in unit


def test_a_dollar_is_literal_in_an_environment_line_and_doubled_in_a_command():
    unit = systemd_unit(["/py", "run", "--context", "$HOME"], "/opt/my$tools/bin", Path("/st"))
    assert 'Environment="PATH=/opt/my$tools/bin"' in unit
    assert 'ExecStart=/py run --context "$$HOME"' in unit


def test_a_socket_file_nobody_listens_on_is_not_a_running_watchdog(socket_path):
    import socket

    from jev_watchdog.service import _came_up

    socket_path.write_text("left by a killed watchdog")
    assert _came_up(socket_path, 0.2) is False
    socket_path.unlink()
    with socket.socket(socket.AF_UNIX) as listener:
        listener.bind(str(socket_path))
        listener.listen()
        assert _came_up(socket_path, 1.0) is True
