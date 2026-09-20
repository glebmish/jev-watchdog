"""Run the watchdog: the hook and control app on the port, the same app plus what a dashboard
reads on a private socket, until a signal or until the foreground dashboard is left.

The port is loopback only and open to every local user; the socket is 0600 in a 0700
directory. What state.py serves shows every watched session, so it is only on the socket.
"""

import asyncio
import signal
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path

from aiohttp import web
from rich.console import Console

from jev_watchdog.client import Client, Refused, Unreachable
from jev_watchdog.feed import Feed
from jev_watchdog.paths import private_dir, socket_path
from jev_watchdog.printer import Printer
from jev_watchdog.server import create_app
from jev_watchdog.state import add_routes
from jev_watchdog.surfaces import SurfaceRegistry

HOST = "127.0.0.1"
Foreground = Callable[[asyncio.Event], Awaitable[None]]


async def serve(
    registry: SurfaceRegistry,
    printer: Printer,
    port: int,
    banner: str,
    feed: Feed,
    socket: Path,
    foreground: Foreground | None = None,
) -> int:
    """Listen until a signal, or for as long as `foreground` (the dashboard) runs."""
    public = web.AppRunner(create_app(registry), access_log=None)
    private_app = create_app(registry)
    add_routes(private_app, registry, feed)
    private = web.AppRunner(private_app, access_log=None)
    await public.setup()
    await private.setup()
    listening = False
    try:
        try:
            await web.TCPSite(public, HOST, port).start()
        except OSError as exc:
            print(f"cannot listen on {HOST}:{port}: {exc}", file=sys.stderr)
            return 1
        listening = True
        try:
            # This process holds the port, so a file at the port's socket path is a dead
            # watchdog's.
            private_dir(socket.parent)
            socket.unlink(missing_ok=True)
            await web.UnixSite(private, str(socket)).start()
            socket.chmod(0o600)
        except OSError as exc:
            # The watchdog is worth more than its view: carry on, unless the view is the point.
            print(f"attach unavailable: {socket}: {exc}", file=sys.stderr)
            if foreground is not None:
                return 1
        printer.banner(banner)
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(signum, stop.set)
        await (stop.wait() if foreground is None else foreground(stop))
        return 0
    finally:
        await private.cleanup()
        socket.unlink(missing_ok=True)
        await registry.shutdown()
        await public.cleanup()
        for judge in registry.judges:
            await judge.aclose()
        if listening:
            printer.global_summary(registry.stats, registry.summaries())


def dashboard(socket: Path, printer: Printer, console: Console, **app_options) -> Foreground:
    """`run --tui`: the dashboard of `attach`, on this process's own socket."""
    from jev_watchdog.tui import WatchdogApp  # textual is only needed by whoever looks

    async def foreground(stop: asyncio.Event) -> None:
        async with Client.on_socket(socket) as client:
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
                printer.console = console  # silent while the app had the terminal

    return foreground


async def attach(port: int) -> int:
    from jev_watchdog.tui import WatchdogApp

    async with Client.on_socket(socket_path(port)) as client:
        try:
            await client.state()
        except Unreachable, Refused:  # nothing there, or something that is not one
            print(f"no watchdog to attach to on port {port}", file=sys.stderr)
            return 1
        await WatchdogApp(client).run_async()
        return 0
