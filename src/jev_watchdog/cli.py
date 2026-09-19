"""jev-watchdog: judge Claude Code agent threads on every hook event, quarantine on evidence.

`run` listens in the foreground; `status`, `quarantine` and `release` talk to a running one.
"""

import argparse
import asyncio
import os
import signal
import sys
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path

from aiohttp import web
from rich.console import Console

from jev_watchdog import control
from jev_watchdog.judge.registry import JUDGES, JudgeConfig, backend_of, make_judge
from jev_watchdog.pack import PackError, Question, load_packs
from jev_watchdog.printer import Printer
from jev_watchdog.replay import load_case, run_cases
from jev_watchdog.server import create_app
from jev_watchdog.surfaces import TRANSCRIPT_WAIT_S, SurfaceRegistry

DEFAULT_PORT = 8787
DEFAULT_KEY_FILE = Path("prototype-throwaway-key")
HOST = "127.0.0.1"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jev-watchdog", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser(
        "run", help="listen for hooks in the foreground and judge every event"
    )
    run.add_argument("--port", type=int, default=DEFAULT_PORT)
    run.add_argument(
        "--enforce",
        action="store_true",
        help="quarantine a thread when a pack rule trips and reject its tool calls; "
        "without it, only report what would be quarantined",
    )
    run.add_argument(
        "--transcript-wait",
        type=float,
        default=TRANSCRIPT_WAIT_S,
        metavar="SECONDS",
        help="how long a tool event may wait, in the background, for its tool call to reach "
        f"the transcript before it is judged anyway; 0 = never wait. Default: {TRANSCRIPT_WAIT_S}",
    )
    _add_judging_options(run)

    replay = commands.add_parser(
        "replay",
        help="judge transcript files step by step and check <name>.expect.toml expectations",
    )
    replay.add_argument("cases", nargs="+", type=Path, metavar="CASE.jsonl")
    _add_judging_options(replay)

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
    for command in (status, quarantine, release, context):
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
        help="leave thinking on for claude judges (default: off, like a non-reasoning judge)",
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
        "--log", type=Path, default=None, help="run log path (default runs/<timestamp>.jsonl)"
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
    try:
        questions = load_packs(args.pack)
    except (OSError, PackError) as exc:
        raise SystemExit(f"cannot load pack: {exc}") from exc
    if len(set(args.judge)) != len(args.judge):
        raise SystemExit(f"--judge given more than once with the same value: {args.judge}")
    needs_key = any(backend_of(spec) == "jev" for spec in args.judge)
    config = JudgeConfig(
        api_key=resolve_api_key(os.environ, args.key_file) if needs_key else None,
        thinking=args.claude_thinking,
    )
    judges = [make_judge(spec, config) for spec in args.judge]
    log_path = args.log or Path("runs") / f"{datetime.now():%Y%m%d-%H%M%S}.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as log_file:
        printer = Printer(Console(), log_file)
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
            " · Ctrl-C to stop"
        )
        return asyncio.run(_serve(registry, printer, args.port, banner))


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
    if status != 200:
        print(body.get("error", f"HTTP {status}"), file=sys.stderr)
        return 1
    if args.command == "status":
        for entry in body["quarantined"]:
            print(f"{entry['target']}  {entry['source']}  {entry['at']}  {entry['reason']}")
        if not body["quarantined"]:
            print("nothing is quarantined")
    elif args.command == "quarantine":
        print(f"quarantined {body['target']}: {body['reason']}")
    elif args.command == "context":
        print(f"context {'set' if body['context'] else 'cleared'} for {body['target']}")
    else:
        print(f"released {body['target']}")
    return 0


async def _replay(
    paths: list[Path], registry: SurfaceRegistry, printer: Printer, questions: list[Question]
) -> int:
    try:
        cases = [load_case(path, questions) for path in paths]
    except (OSError, ValueError) as exc:
        raise SystemExit(f"cannot load case: {exc}") from exc
    try:
        await run_cases(cases, registry, printer)
    finally:
        await registry.shutdown()
        for judge in registry.judges:
            await judge.aclose()
    printer.global_summary(registry.stats, {})
    return 0


async def _serve(registry: SurfaceRegistry, printer: Printer, port: int, banner: str) -> int:
    runner = web.AppRunner(create_app(registry), access_log=None)
    await runner.setup()
    listening = False
    try:
        try:
            await web.TCPSite(runner, HOST, port).start()
        except OSError as exc:
            print(f"cannot listen on {HOST}:{port}: {exc}", file=sys.stderr)
            return 1
        listening = True
        printer.banner(banner)
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(signum, stop.set)
        await stop.wait()
        return 0
    finally:
        await registry.shutdown()
        await runner.cleanup()
        for judge in registry.judges:
            await judge.aclose()
        if listening:
            printer.global_summary(registry.stats, registry.summaries())


if __name__ == "__main__":
    sys.exit(main())
