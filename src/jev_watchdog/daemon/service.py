"""`install` / `uninstall`: the watchdog as a launchd agent (macOS) or systemd user unit (Linux).

The service manager does the daemon's work: starting at login, restarting after a crash,
keeping the output. The unit runs `run` with the options `install` was given. A service has no
working directory and no shell environment, so paths are made absolute and PATH is carried
along for the `claude` and `codex` judges. The API key is never written into a unit.
"""

import argparse
import os
import plistlib
import re
import socket as sockets
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from pathlib import Path

from jev_watchdog.core.pack import PackError, load_packs
from jev_watchdog.daemon.paths import private_dir, socket_path, state_dir
from jev_watchdog.judge.registry import check_specs, needs_key

LABEL = "io.github.glebmish.jev-watchdog"
UNIT_NAME = "jev-watchdog.service"
RESTART_DELAY_S = 10
# launchd answers a bootstrap that follows a bootout too closely with "Input/output error".
BOOTSTRAP_TRIES = 10
BOOTSTRAP_PAUSE_S = 0.5
_BARE = re.compile(r"[A-Za-z0-9_@%+=:,./-]+")

Run = Callable[..., subprocess.CompletedProcess]


class ServiceError(Exception):
    """Why nothing was installed; the message is for the operator."""


def run_arguments(args: argparse.Namespace, state: Path) -> list[str]:
    """The `run` command line of the parsed `install` options, good from any directory."""
    arguments = ["run", "--port", str(args.port), "--transcript-wait", str(args.transcript_wait)]
    if args.enforce:
        arguments.append("--enforce")
    for spec in args.judge:
        arguments += ["--judge", spec]
    if args.claude_thinking:
        arguments.append("--claude-thinking")
    for pack in args.pack:
        arguments += ["--pack", str(pack.resolve())]
    if args.context:
        arguments += ["--context", args.context]
    if needs_key(args.judge):
        arguments += ["--key-file", str(args.key_file.resolve())]
    if args.log is not None:
        arguments += ["--log", str(args.log.resolve())]
    else:
        log_dir = state / "runs" if args.log_dir is None else args.log_dir.resolve()
        arguments += ["--log-dir", str(log_dir)]
    return arguments


def launchd_plist(command: list[str], path_env: str, log: Path, state_home: Path) -> bytes:
    return plistlib.dumps(
        {
            "Label": LABEL,
            "ProgramArguments": command,
            # A service sees no shell profile. Without the state home the watchdog would put
            # its socket under the default while `attach` looks where the shell says.
            "EnvironmentVariables": {"PATH": path_env, "XDG_STATE_HOME": str(state_home)},
            "RunAtLoad": True,
            # A stop (exit 0) stays stopped; a crash or a taken port (exit 1) is retried.
            # Whoever holds the port also gets the hooks, so taking it back is what is wanted.
            "KeepAlive": {"SuccessfulExit": False},
            "ThrottleInterval": RESTART_DELAY_S,
            "StandardOutPath": str(log),
            "StandardErrorPath": str(log),
            # No ProcessType: Background would throttle the process that answers the hooks.
        }
    )


def systemd_unit(command: list[str], path_env: str, state_home: Path) -> str:
    return (
        "[Unit]\n"
        "Description=jev-watchdog: judges Claude Code agent threads\n"
        "\n"
        "[Service]\n"
        f"ExecStart={' '.join(_unit_word(word) for word in command)}\n"
        f"Environment={_unit_word('PATH=' + path_env, environment=True)}\n"
        f"Environment={_unit_word(f'XDG_STATE_HOME={state_home}', environment=True)}\n"
        "Restart=on-failure\n"
        f"RestartSec={RESTART_DELAY_S}\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def install(
    args: argparse.Namespace,
    *,
    platform: str = sys.platform,
    home: Path | None = None,
    env: Mapping[str, str] = os.environ,
    uid: int | None = None,
    run: Run = subprocess.run,
    out: Callable[[str], None] = print,
    wait_s: float = 5.0,
) -> int:
    home = home or Path.home()
    try:
        _check(args, env, platform)
        state = state_dir(env)
        command = [sys.executable, "-m", "jev_watchdog.cli", *run_arguments(args, state)]
        path_env = env.get("PATH", os.defpath)
        if platform == "darwin":
            unit = _plist_path(home)
            content = launchd_plist(command, path_env, state / "daemon.log", state.parent)
        else:
            unit = _unit_path(home)
            content = systemd_unit(command, path_env, state.parent).encode()
    except ServiceError as exc:
        out(str(exc))
        return 1

    private_dir(state)
    replacing = unit.exists()
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.touch(mode=0o600)
    unit.chmod(0o600)  # it names the key file and may hold a context
    unit.write_bytes(content)
    out(f"wrote {unit}")

    if platform == "darwin":
        target = f"gui/{os.getuid() if uid is None else uid}"
        if replacing:
            _manager(run, out, ["launchctl", "bootout", f"{target}/{LABEL}"], may_fail=True)
        started = _bootstrap(run, out, ["launchctl", "bootstrap", target, str(unit)], replacing)
        logs = f"output: {state / 'daemon.log'}"
    else:
        started = all(
            _manager(run, out, ["systemctl", "--user", *words])
            for words in (["daemon-reload"], ["enable", UNIT_NAME], ["restart", UNIT_NAME])
        )
        logs = f"output: journalctl --user -u {UNIT_NAME}"
    if not started:
        out(f"the service manager refused; {unit} is left in place to look at")
        return 1

    if _came_up(socket_path(args.port, env), wait_s):
        out(f"jev-watchdog is running on port {args.port}; it starts at login")
    else:
        out(f"installed, but not answering yet on port {args.port}: see the output")
    out(logs)
    if args.log is None and args.log_dir is None:
        out(f"run logs: {state / 'runs'}")
    port = "" if args.port == 8787 else f" --port {args.port}"
    out(f"look at it with `jev-watchdog attach{port}`; remove it with `jev-watchdog uninstall`")
    return 0


def uninstall(
    *,
    platform: str = sys.platform,
    home: Path | None = None,
    uid: int | None = None,
    run: Run = subprocess.run,
    out: Callable[[str], None] = print,
) -> int:
    home = home or Path.home()
    unit = _plist_path(home) if platform == "darwin" else _unit_path(home)
    if not unit.exists():
        out(f"nothing is installed ({unit} does not exist)")
        return 0
    if platform == "darwin":
        target = f"gui/{os.getuid() if uid is None else uid}/{LABEL}"
        _manager(run, out, ["launchctl", "bootout", target], may_fail=True)  # not loaded: fine
    else:
        _manager(run, out, ["systemctl", "--user", "disable", "--now", UNIT_NAME], may_fail=True)
    unit.unlink()
    if platform != "darwin":
        _manager(run, out, ["systemctl", "--user", "daemon-reload"], may_fail=True)
    out(f"removed {unit}; run logs and output are kept")
    return 0


def _check(args: argparse.Namespace, env: Mapping[str, str], platform: str) -> None:
    """What `run` would refuse at start, refused now: a service fails where nobody looks."""
    if platform != "darwin" and not platform.startswith("linux"):
        raise ServiceError("install knows launchd and systemd: macOS and Linux only")
    try:
        load_packs(args.pack)
    except (OSError, PackError) as exc:
        raise ServiceError(f"cannot load pack: {exc}") from exc
    try:
        check_specs(args.judge)
    except ValueError as exc:
        raise ServiceError(str(exc)) from exc
    if needs_key(args.judge):
        key_file = args.key_file
        if not (key_file.is_file() and key_file.read_text(encoding="utf-8").strip()):
            seen = (
                " (a service does not see TYPESAFE_API_KEY)" if env.get("TYPESAFE_API_KEY") else ""
            )
            raise ServiceError(
                f"no Jev API key in {key_file}{seen}. The key is not written into the unit: "
                "put it in a file only you can read and name it with --key-file"
            )


def _bootstrap(run: Run, out: Callable[[str], None], command: list[str], replacing: bool) -> bool:
    tries = BOOTSTRAP_TRIES if replacing else 1
    for attempt in range(tries):
        last = attempt == tries - 1
        if _manager(run, out, command, may_fail=not last):
            return True
        if not last:
            time.sleep(BOOTSTRAP_PAUSE_S)
    return False


def _manager(
    run: Run,
    out: Callable[[str], None],
    command: list[str],
    may_fail: bool = False,
) -> bool:
    done = run(command, capture_output=True, text=True, check=False)
    if done.returncode == 0:
        return True
    if not may_fail:
        said = (done.stderr or done.stdout or "").strip()
        out(f"`{' '.join(command)}` failed ({done.returncode}): {said}")
    return False


def _came_up(socket: Path, wait_s: float) -> bool:
    """Whether something answers on the socket. The file alone may be a killed watchdog's."""
    deadline = time.monotonic() + wait_s
    while True:
        try:
            with sockets.socket(sockets.AF_UNIX) as probe:
                probe.settimeout(1.0)
                probe.connect(str(socket))
                return True
        except OSError:
            pass
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.1)


def _plist_path(home: Path) -> Path:
    return home / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def _unit_path(home: Path) -> Path:
    return home / ".config" / "systemd" / "user" / UNIT_NAME


def _unit_word(word: str, environment: bool = False) -> str:
    """One word of a unit file in systemd's quoting, with what systemd expands made literal.

    Specifiers (%) are expanded everywhere. Variables ($) are expanded in a command line only:
    in an Environment= assignment a $ is already literal, and $$ would stay two characters.
    """
    if "\n" in word or "\r" in word:
        raise ServiceError(f"a unit file cannot hold a line break: {word!r}")
    if environment or not _BARE.fullmatch(word):
        word = '"' + word.replace("\\", "\\\\").replace('"', '\\"') + '"'
    word = word.replace("%", "%%")
    return word if environment else word.replace("$", "$$")
