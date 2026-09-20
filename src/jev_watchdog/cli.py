"""jev-watchdog: judge Claude Code agent threads on every hook event, quarantine on evidence.

`run` listens in the foreground, with `--tui` under a dashboard; `install` makes it a service.
`attach` opens the dashboard on a running one; `status`, `quarantine`, `release` and `context`
talk to it.
"""

import argparse
import asyncio
import os
import sys
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path

from rich.console import Console

from jev_watchdog import service
from jev_watchdog.client import Client, NotAWatchdog, Refused, Unreachable
from jev_watchdog.feed import Feed
from jev_watchdog.judge.base import Judge
from jev_watchdog.judge.registry import (
    JUDGES,
    JudgeConfig,
    backend_of,
    check_specs,
    make_judge,
    needs_key,
)
from jev_watchdog.pack import PackError, Question, load_packs
from jev_watchdog.paths import socket_path
from jev_watchdog.printer import Printer, printable
from jev_watchdog.replay import ReplayError, load_case, run_cases
from jev_watchdog.serve import HOST, attach, dashboard, serve
from jev_watchdog.surfaces import TRANSCRIPT_WAIT_S, SurfaceRegistry

DEFAULT_PORT = 8787
DEFAULT_KEY_FILE = Path("prototype-throwaway-key")
SERVICE_WIDTH = 200


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jev-watchdog", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser(
        "run", help="listen for hooks in the foreground and judge every event"
    )
    run.add_argument(
        "--tui",
        action="store_true",
        help="show the dashboard of `attach` instead of console lines; leaving it stops the "
        "watchdog",
    )
    install = commands.add_parser(
        "install",
        help="run in the background from now on: a launchd agent (macOS) or systemd user "
        "unit (Linux) that runs `run` with these options, starts at login and restarts on a crash",
    )
    for command in (run, install):
        command.add_argument("--port", type=int, default=DEFAULT_PORT)
        command.add_argument(
            "--enforce",
            action="store_true",
            help="quarantine a thread when a pack rule trips and reject its tool calls; "
            "without it, only report what would be quarantined",
        )
        command.add_argument(
            "--transcript-wait",
            type=float,
            default=TRANSCRIPT_WAIT_S,
            metavar="SECONDS",
            help="how long a tool event may wait, in the background, for its tool call to "
            "reach the transcript before it is judged anyway; 0 = never wait. "
            f"Default: {TRANSCRIPT_WAIT_S}",
        )
        _add_judging_options(command)
    commands.add_parser("uninstall", help="stop the installed service and remove its unit")

    replay = commands.add_parser(
        "replay",
        help="judge transcript files step by step and check <name>.expect.toml expectations",
    )
    replay.add_argument("cases", nargs="+", type=Path, metavar="CASE.jsonl")
    # A replayed transcript is complete and nothing is there to reject: run's options, off.
    replay.set_defaults(enforce=False, transcript_wait=0.0, tui=False)
    _add_judging_options(replay)

    attach = commands.add_parser(
        "attach", help="open the dashboard on a running watchdog; leaving it leaves that running"
    )
    status = commands.add_parser("status", help="list quarantined threads of a running watchdog")
    quarantine = commands.add_parser("quarantine", help="quarantine a thread by hand")
    quarantine.add_argument("--reason", default="manual")
    release = commands.add_parser("release", help="release a quarantined thread")
    context = commands.add_parser(
        "context",
        help="tell the judges what you know about a session and the agent does not",
    )
    for command in (quarantine, release, context):
        command.add_argument(
            "target", metavar="TARGET", help="as shown on the console: SESSION[/AGENT]"
        )
    context.add_argument("text", nargs="?", metavar="TEXT", help="applies to the whole session")
    context.add_argument("--clear", action="store_true", help="forget the session's context")
    for command in (attach, status, quarantine, release, context):
        command.add_argument("--port", type=int, default=DEFAULT_PORT)
    return parser


def _add_judging_options(command: argparse.ArgumentParser) -> None:
    command.add_argument(
        "--judge",
        action=_AppendReplacingDefault,
        default=["jev"],
        type=_judge_spec,
        metavar="NAME[:MODEL]",
        help=f"judge backend, one of {sorted(JUDGES)}, optionally with a model, e.g. "
        "claude:claude-haiku-4-5. Repeat to run several side by side. fake:WORD is calm until "
        "a judged tool call contains WORD, then trips every quarantine rule. Default: jev",
    )
    command.add_argument(
        "--claude-thinking",
        action="store_true",
        help="leave thinking on for claude judges and the model's default reasoning effort for "
        "codex judges (default: off / low, like a non-reasoning judge)",
    )
    command.add_argument(
        "--pack",
        action=_AppendReplacingDefault,
        default=[Path("pack.toml")],
        type=Path,
        help="question pack; repeat to merge several, e.g. --pack pack.toml --pack "
        "packs/canary.toml adds an easy-to-trip rule for end-to-end tests. Default: pack.toml",
    )
    command.add_argument(
        "--context",
        default=None,
        metavar="TEXT",
        help="what you know about the sessions and the agent does not; judged next to the "
        "transcript. `jev-watchdog context` sets it for one running session instead",
    )
    command.add_argument("--key-file", type=Path, default=DEFAULT_KEY_FILE)
    command.add_argument(
        "--log", type=Path, default=None, help="run log path (default <log dir>/<timestamp>.jsonl)"
    )
    command.add_argument(
        "--log-dir", type=Path, default=None, help="where a run log goes. Default: runs"
    )


class _AppendReplacingDefault(argparse.Action):
    """Like `append`, but the first explicit value replaces the default instead of joining it."""

    def __call__(self, parser, namespace, value, option_string=None):
        current = getattr(namespace, self.dest)
        setattr(namespace, self.dest, [value] if current is self.default else [*current, value])


def _judge_spec(spec: str) -> str:
    if backend_of(spec) not in JUDGES:
        raise argparse.ArgumentTypeError(f"unknown judge {spec!r}; available: {sorted(JUDGES)}")
    return spec


def resolve_api_key(env: Mapping[str, str], key_file: Path) -> str:
    key = env.get("TYPESAFE_API_KEY", "").strip()
    if not key and key_file.is_file():
        key = key_file.read_text(encoding="utf-8").strip()
    if not key:
        raise SystemExit(f"no Jev API key: set TYPESAFE_API_KEY or put the key in {key_file}")
    return key


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "context" and (args.text is None) != args.clear:
        parser.error("context takes TEXT or --clear")
    if args.command in ("status", "quarantine", "release", "context"):
        return _control(args)
    if args.command == "attach":
        return asyncio.run(attach(args.port))
    if args.command == "install":
        return service.install(args)
    if args.command == "uninstall":
        return service.uninstall()
    try:
        questions = load_packs(args.pack)
    except (OSError, PackError) as exc:
        raise SystemExit(f"cannot load pack: {exc}") from exc
    try:
        check_specs(args.judge)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    api_key = resolve_api_key(os.environ, args.key_file) if needs_key(args.judge) else None
    # The Jev judge is handed the key. The claude and codex judges start child processes,
    # which would inherit it from the environment, and they run on the agent's transcripts.
    os.environ.pop("TYPESAFE_API_KEY", None)
    config = JudgeConfig(api_key=api_key, thinking=args.claude_thinking)
    judges = [make_judge(spec, config) for spec in args.judge]
    names = [judge.name for judge in judges]
    if len(set(names)) != len(names):  # e.g. jev and jev:jev-latest
        asyncio.run(_close(judges))
        raise SystemExit(f"--judge names the same judge more than once: {names}")
    log_path = args.log or (args.log_dir or Path("runs")) / f"{datetime.now():%Y%m%d-%H%M%S}.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    # 0600: the log holds every prompt, tool input and tool output of the watched sessions.
    with open(log_path, "a", encoding="utf-8", opener=_private) as log_file:
        feed = Feed()
        # With --tui the dashboard owns the terminal; what the console would say is in its feed.
        printer = Printer(_console(quiet=args.tui), log_file, feed=feed)
        registry = SurfaceRegistry(
            judges,
            questions,
            printer,
            enforce=args.enforce,
            transcript_wait_s=args.transcript_wait,
            context=(args.context or "").strip() or None,
        )
        if args.command == "replay":
            return asyncio.run(_replay(args.cases, registry, printer, questions))
        banner = (
            f"jev-watchdog listening on http://{HOST}:{args.port}/hooks · judges={','.join(names)}"
            f" · {len(questions)} questions · {_mode(registry)} · log={log_path}"
        ) + (" · Ctrl-C to stop" if sys.stdout.isatty() else "")
        socket = socket_path(args.port)
        foreground = dashboard(socket, printer, _console()) if args.tui else None
        return asyncio.run(serve(registry, printer, args.port, banner, feed, socket, foreground))


def _console(quiet: bool = False) -> Console:
    # A service's output is a file: rich would take it for 80 columns and fold the tables.
    return (
        Console(quiet=quiet) if sys.stdout.isatty() else Console(quiet=quiet, width=SERVICE_WIDTH)
    )


def _private(path: str, flags: int) -> int:
    return os.open(path, flags, 0o600)


def _mode(registry: SurfaceRegistry) -> str:
    if not registry.decider.rules:
        return "no quarantine rules in the pack"
    rules = ",".join(rule.id for rule in registry.decider.rules)
    if registry.enforce:
        return f"ENFORCING quarantine on {rules} (decided by {registry.judges[0].name})"
    return f"dry run, would quarantine on {rules}"


def _control(args: argparse.Namespace) -> int:
    try:
        body = asyncio.run(_ask(args))
    except NotAWatchdog:
        print(f"whatever listens on port {args.port} is not a jev-watchdog", file=sys.stderr)
        return 1
    except Unreachable:
        print(f"no watchdog listening on port {args.port}", file=sys.stderr)
        return 1
    except Refused as exc:
        print(printable(str(exc)), file=sys.stderr)
        return 1
    # What comes back was put together from labels and reasons the watched agent can choose.
    if args.command == "status":
        for entry in body:
            line = f"{entry['target']}  {entry['source']}  {entry['at']}  {entry['reason']}"
            print(printable(line))
        if not body:
            print("nothing is quarantined")
    elif args.command == "quarantine":
        print(printable(f"quarantined {body['target']}: {body['reason']}"))
    elif args.command == "context":
        print(printable(f"context {'set' if body['context'] else 'cleared'} for {body['target']}"))
    else:
        print(printable(f"released {body['target']}"))
    return 0


async def _ask(args: argparse.Namespace) -> dict | list[dict]:
    async with Client.on_port(args.port) as client:
        if args.command == "status":
            return await client.quarantined()
        if args.command == "quarantine":
            return await client.quarantine(args.target, args.reason)
        if args.command == "context":
            return await client.context(args.target, args.text or "")
        return await client.release(args.target)


async def _replay(
    paths: list[Path], registry: SurfaceRegistry, printer: Printer, questions: list[Question]
) -> int:
    try:  # from the start: a judge may hold a temp dir, and a case that fails to load exits
        try:
            cases = [load_case(path, questions) for path in paths]
            names = [case.name for case in cases]
            if repeated := sorted({name for name in names if names.count(name) > 1}):
                # The name is the session id: same-named cases would be judged as one thread.
                raise ReplayError(f"more than one case is named {', '.join(map(repr, repeated))}")
        except (OSError, ValueError) as exc:
            raise SystemExit(f"cannot load case: {exc}") from exc
        await run_cases(cases, registry, printer)
    finally:
        await registry.shutdown()
        await _close(registry.judges)
    printer.global_summary(registry.stats, {})
    return 0


async def _close(judges: list[Judge]) -> None:
    for judge in judges:
        await judge.aclose()


if __name__ == "__main__":
    sys.exit(main())
