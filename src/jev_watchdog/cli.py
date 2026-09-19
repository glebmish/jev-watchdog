"""jev-watchdog run: foreground listener for Claude Code hooks."""

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

from jev_watchdog.judge.registry import JUDGES, JudgeConfig, make_judge
from jev_watchdog.pack import PackError, load_pack
from jev_watchdog.printer import Printer
from jev_watchdog.server import create_app
from jev_watchdog.surfaces import SurfaceRegistry

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
    run.add_argument("--judge", choices=sorted(JUDGES), default="jev")
    run.add_argument("--pack", type=Path, default=Path("pack.toml"))
    run.add_argument("--key-file", type=Path, default=DEFAULT_KEY_FILE)
    run.add_argument(
        "--log", type=Path, default=None, help="run log path (default runs/<timestamp>.jsonl)"
    )
    return parser


def resolve_api_key(env: Mapping[str, str], key_file: Path) -> str:
    key = env.get("TYPESAFE_API_KEY", "").strip()
    if not key and key_file.is_file():
        key = key_file.read_text(encoding="utf-8").strip()
    if not key:
        raise SystemExit(f"no Jev API key: set TYPESAFE_API_KEY or put the key in {key_file}")
    return key


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        questions = load_pack(args.pack)
    except (OSError, PackError) as exc:
        raise SystemExit(f"cannot load pack {args.pack}: {exc}") from exc
    api_key = resolve_api_key(os.environ, args.key_file) if args.judge == "jev" else None
    judge = make_judge(args.judge, JudgeConfig(api_key=api_key))
    log_path = args.log or Path("runs") / f"{datetime.now():%Y%m%d-%H%M%S}.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as log_file:
        printer = Printer(Console(), log_file)
        registry = SurfaceRegistry(judge, questions, printer)
        banner = (
            f"jev-watchdog listening on http://{HOST}:{args.port}/hooks · judge={args.judge}"
            f" · {len(questions)} questions · log={log_path} · Ctrl-C to stop"
        )
        return asyncio.run(_serve(registry, printer, args.port, banner))


async def _serve(registry: SurfaceRegistry, printer: Printer, port: int, banner: str) -> int:
    runner = web.AppRunner(create_app(registry), access_log=None)
    await runner.setup()
    try:
        try:
            await web.TCPSite(runner, HOST, port).start()
        except OSError as exc:
            print(f"cannot listen on {HOST}:{port}: {exc}", file=sys.stderr)
            return 1
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
        await registry.judge.aclose()
        printer.global_summary(registry.stats, registry.summaries())


if __name__ == "__main__":
    sys.exit(main())
