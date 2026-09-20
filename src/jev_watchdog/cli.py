"""jev-watchdog: judge Claude Code agent threads on every hook event, quarantine on evidence.

`run` listens in the foreground, with `--tui` under a dashboard; `install` makes it a service.
`attach` opens the dashboard on a running one; `status`, `quarantine`, `release` and `context`
talk to it.
"""

import argparse
import asyncio
import os
import signal
import sys
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from aiohttp import web
from rich.console import Console

from jev_watchdog import control, service
from jev_watchdog.attach import AttachClient, AttachError, ControlError
from jev_watchdog.feed import Feed
from jev_watchdog.judge.base import Judge
from jev_watchdog.judge.registry import JUDGES, JudgeConfig, backend_of, make_judge
from jev_watchdog.pack import PackError, Question, load_packs
from jev_watchdog.paths import private_dir, socket_path
from jev_watchdog.printer import Printer, printable
from jev_watchdog.replay import ReplayError, load_case, run_cases
from jev_watchdog.server import create_app
from jev_watchdog.state import DaemonInfo
from jev_watchdog.surfaces import TRANSCRIPT_WAIT_S, SurfaceRegistry

DEFAULT_PORT = 8787
DEFAULT_KEY_FILE = Path("prototype-throwaway-key")
HOST = "127.0.0.1"
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
        return asyncio.run(_attach(args.port))
    if args.command == "install":
        return service.install(args)
    if args.command == "uninstall":
        return service.uninstall()
    try:
        questions = load_packs(args.pack)
    except (OSError, PackError) as exc:
        raise SystemExit(f"cannot load pack: {exc}") from exc
    if len(set(args.judge)) != len(args.judge):
        raise SystemExit(f"--judge given more than once with the same value: {args.judge}")
    needs_key = any(backend_of(spec) == "jev" for spec in args.judge)
    api_key = resolve_api_key(os.environ, args.key_file) if needs_key else None
    # The Jev judge is handed the key. The claude and codex judges start child processes,
    # which would inherit it from the environment, and they run on the agent's transcripts.
    os.environ.pop("TYPESAFE_API_KEY", None)
    config = JudgeConfig(api_key=api_key, thinking=args.claude_thinking)
    judges = [make_judge(spec, config) for spec in args.judge]
    names = [judge.name for judge in judges]
    if len(set(names)) != len(names):  # e.g. jev and jev:jev-latest
        asyncio.run(_close(judges))
        raise SystemExit(f"--judge names the same judge more than once: {names}")
    log_dir = args.log_dir or Path("runs")
    log_path = args.log or log_dir / f"{datetime.now():%Y%m%d-%H%M%S}.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    # 0600: the log holds every prompt, tool input and tool output of the watched sessions.
    with open(log_path, "a", encoding="utf-8", opener=_private) as log_file:
        feed = Feed() if args.command == "run" else None
        tui = args.command == "run" and args.tui
        # The dashboard owns the terminal; what the console would say is in its feed.
        printer = Printer(_console(quiet=tui), log_file, feed=feed)
        enforce = args.command == "run" and args.enforce
        wait_s = args.transcript_wait if args.command == "run" else 0.0  # replay is complete
        registry = SurfaceRegistry(
            judges,
            questions,
            printer,
            enforce=enforce,
            transcript_wait_s=wait_s,
            context=(args.context or "").strip() or None,
        )
        if args.command == "replay":
            return asyncio.run(_replay(args.cases, registry, printer, questions))
        names = ",".join(judge.name for judge in judges)
        banner = (
            f"jev-watchdog listening on http://{HOST}:{args.port}/hooks · judges={names}"
            f" · {len(questions)} questions · {_mode(registry)} · log={log_path}"
        ) + (" · Ctrl-C to stop" if sys.stdout.isatty() else "")
        info = DaemonInfo(feed.boot, os.getpid(), datetime.now(), args.port, str(log_path))
        attachment = Attachment(feed, info, socket_path(args.port))
        foreground = _dashboard(attachment.socket, printer) if tui else None
        return asyncio.run(_serve(registry, printer, args.port, banner, attachment, foreground))


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
        if args.command == "status":
            status, body = control.call(args.port, "GET", "/quarantine")
        else:
            request = {"target": args.target}
            if args.command == "quarantine":
                request["reason"] = args.reason
            elif args.command == "context":
                request["text"] = args.text or ""
            status, body = control.call(args.port, "POST", f"/{args.command}", request)
    except control.Unreachable:
        print(f"no watchdog listening on port {args.port}", file=sys.stderr)
        return 1
    except control.NotAWatchdog:
        print(f"whatever listens on port {args.port} is not a jev-watchdog", file=sys.stderr)
        return 1
    # What comes back was put together from labels and reasons the watched agent can choose.
    if status != 200:
        print(printable(str(body.get("error", f"HTTP {status}"))), file=sys.stderr)
        return 1
    if args.command == "status":
        for entry in body["quarantined"]:
            line = f"{entry['target']}  {entry['source']}  {entry['at']}  {entry['reason']}"
            print(printable(line))
        if not body["quarantined"]:
            print("nothing is quarantined")
    elif args.command == "quarantine":
        print(printable(f"quarantined {body['target']}: {body['reason']}"))
    elif args.command == "context":
        print(printable(f"context {'set' if body['context'] else 'cleared'} for {body['target']}"))
    else:
        print(printable(f"released {body['target']}"))
    return 0


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


@dataclass(frozen=True)
class Attachment:
    """What `attach` needs of a running watchdog, and the socket it is offered on."""

    feed: Feed
    info: DaemonInfo
    socket: Path


async def _serve(
    registry: SurfaceRegistry,
    printer: Printer,
    port: int,
    banner: str,
    attachment: Attachment | None = None,
    foreground: Callable[[asyncio.Event], Awaitable[None]] | None = None,
) -> int:
    """Listen until a signal, or for as long as `foreground` (the dashboard) runs."""
    runner = web.AppRunner(create_app(registry), access_log=None)
    await runner.setup()
    private = None  # the same app plus /state and /events, on the socket only
    listening = False
    try:
        try:
            await web.TCPSite(runner, HOST, port).start()
        except OSError as exc:
            print(f"cannot listen on {HOST}:{port}: {exc}", file=sys.stderr)
            return 1
        listening = True
        if attachment is not None:
            private = web.AppRunner(
                create_app(registry, attachment.feed, attachment.info), access_log=None
            )
            await private.setup()
            try:
                await _offer(private, attachment.socket)
            except OSError as exc:
                print(f"attach unavailable: {attachment.socket}: {exc}", file=sys.stderr)
                await private.cleanup()
                private = None
        if foreground is not None and private is None:
            return 1  # a dashboard with nothing to read
        printer.banner(banner)
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(signum, stop.set)
        await (stop.wait() if foreground is None else foreground(stop))
        return 0
    finally:
        if attachment is not None:
            attachment.feed.close()  # or the open /events streams hold the cleanup up
        if private is not None:
            await private.cleanup()
            attachment.socket.unlink(missing_ok=True)
        await registry.shutdown()
        await runner.cleanup()
        await _close(registry.judges)
        if listening:
            printer.global_summary(registry.stats, registry.summaries())


def _dashboard(socket: Path, printer: Printer, **app_options):
    """`run --tui`: the dashboard of `attach`, on this process's own socket."""
    from jev_watchdog.tui import WatchdogApp  # textual is only needed by whoever looks

    async def foreground(stop: asyncio.Event) -> None:
        client = AttachClient(socket)
        app = WatchdogApp(client, owns_watchdog=True)
        showing = asyncio.create_task(app.run_async(**app_options))
        stopped = asyncio.create_task(stop.wait())
        try:
            await asyncio.wait({showing, stopped}, return_when=asyncio.FIRST_COMPLETED)
            if not showing.done():  # SIGTERM: the terminal has to be given back first
                app.exit()
            await showing
        finally:
            stopped.cancel()
            await client.aclose()
            printer.console = _console()  # for the closing summary

    return foreground


async def _attach(port: int) -> int:
    from jev_watchdog.tui import WatchdogApp

    client = AttachClient(socket_path(port))
    try:
        try:
            await client.state()
        except AttachError, ControlError:  # nothing there, or something that is not one
            print(f"no watchdog to attach to on port {port}", file=sys.stderr)
            return 1
        await WatchdogApp(client).run_async()
        return 0
    finally:
        await client.aclose()


async def _offer(runner: web.AppRunner, path: Path) -> None:
    # This process holds the port, so a file at the port's socket path is a dead watchdog's.
    private_dir(path.parent)
    path.unlink(missing_ok=True)
    await web.UnixSite(runner, str(path)).start()
    path.chmod(0o600)


if __name__ == "__main__":
    sys.exit(main())
