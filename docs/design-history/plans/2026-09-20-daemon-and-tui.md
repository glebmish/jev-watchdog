# Daemon and attachable TUI — implementation plan

> Executed inline, task by task, test first, one commit per task. Interfaces below are the
> contract between tasks; the code is in `src/`.

**Goal:** run the watchdog as a launchd / systemd service and attach an interactive Textual
dashboard to it; `run --tui` shows the same dashboard in the foreground.

**Architecture:** `Printer` publishes trimmed display records to a `Feed` (ring buffer +
subscribers). The aiohttp app gains `GET /state` and `GET /events` (SSE), served only on a
0600 unix socket next to the unchanged TCP site. The TUI is a client of that socket.

**Tech stack:** Python 3.14, aiohttp (server, and client through `UnixConnector`), Textual ≥ 8,
rich, pytest + pytest-asyncio + pytest-aiohttp, `plistlib`.

**Spec:** `docs/design-history/specs/2026-09-20-daemon-and-tui-design.md`

## Global constraints

- The hook path never blocks or raises because of the feed, the socket or a subscriber.
- Agent-chosen text reaches a terminal only through `printable`, as `Text`, never as markup.
- The API key is never written to a unit file, the feed, the console or the log.
- `/state` and `/events` are not routed on the TCP site.
- Console output and the JSONL log of plain `run` and `replay` stay byte-for-byte what they are
  (the existing `tests/test_printer.py`, `test_surfaces.py`, `test_replay.py` pass unchanged).
- Unix sockets in tests live under `/tmp` (macOS limits the path to 104 bytes).
- ruff clean at line length 100; commit trailer is `Co-Authored-By` only.

## Files

| file | responsibility |
|---|---|
| `src/jev_watchdog/feed.py` (new) | ring buffer, `seq`, subscribers, drop-the-slow |
| `src/jev_watchdog/printer.py` | `display_record`, `render_line`, optional `feed` sink |
| `src/jev_watchdog/state.py` (new) | `DaemonInfo`, `snapshot(registry, info, now)` |
| `src/jev_watchdog/surfaces.py` | `Surface.last_event`, `Surface.last_seen` |
| `src/jev_watchdog/server.py` | `/state`, `/events`, guard on unix connections |
| `src/jev_watchdog/paths.py` (new) | state dir, socket path, private directory |
| `src/jev_watchdog/attach.py` (new) | `AttachClient` over the socket |
| `src/jev_watchdog/tui.py` (new) | `WatchdogApp` |
| `src/jev_watchdog/service.py` (new) | unit rendering, install / uninstall |
| `src/jev_watchdog/cli.py` | `run --tui`, `--log-dir`, `attach`, `install`, `uninstall`, socket site |

---

### Task 1: Feed

**Produces**

```python
BACKLOG = 2000
SUBSCRIBER_QUEUE = 1000

@dataclass
class Subscription:
    backlog: list[dict]                   # records with seq > since, oldest first
    queue: asyncio.Queue[dict | None]     # live records; None ends the stream

class Feed:
    boot: str                             # secrets.token_hex(8), one per process
    def __init__(self, backlog: int = BACKLOG, queue_size: int = SUBSCRIBER_QUEUE) -> None
    def publish(self, record: dict) -> dict      # returns record | {"seq": n}; never blocks, never raises
    def subscribe(self, since: int = 0) -> Subscription
    def unsubscribe(self, subscription: Subscription) -> None   # idempotent
    def close(self) -> None                      # end every subscriber (shutdown)
```

A subscriber whose queue is full is removed; its queue is emptied and given `None`, so its
stream ends and the client comes back with `since`.

**Tests** (`tests/test_feed.py`): seq starts at 1 and rises; backlog keeps the last N;
`subscribe(since=k)` returns only later records; a live record reaches every subscriber;
a full subscriber is dropped, gets `None`, and the others and `publish` are unaffected;
`unsubscribe` twice is fine; `close` ends all.

### Task 2: Display records and one renderer

**Consumes** `Feed.publish`.
**Produces**

```python
MESSAGE_LIMIT = 500
def display_record(kind: str, label: str, ts: datetime, **data) -> dict
def render_line(record: dict) -> Text           # sanitised with printable; styles as today
class Printer:
    def __init__(self, console, log_file=None, clock=datetime.now, feed: Feed | None = None)
```

Record fields beyond `ts` (ISO, seconds), `kind`, `surface`:
`event`: `event`, `detail` · `rejected`: `detail`, `reason` · `verdict`: `judge`,
`latency_ms`, `input_tokens`, `answers` `{qid: value}`, `flagged` `[qid]` · `error`: `judge`,
`error_kind`, `message` (≤ 500) · `note`: `message` · `quarantine`: `source`, `reason`,
`enforced` · `released`: nothing.

Every `Printer` method builds the display record, prints `render_line(record)`, logs the full
data as before, publishes the display record. The clock is read once per call.

**Tests** (`tests/test_printer.py`, added): for each kind the console line equals
`render_line` of the published record; an `event` record holds no `payload` and a 1 MiB
`tool_input` yields a record under 1 KiB; error messages are cut at 500; ESC in a label comes
out as `�` from `render_line`; with no feed nothing changes.

### Task 3: State snapshot

**Produces**

```python
@dataclass(frozen=True)
class DaemonInfo:
    boot: str; pid: int; started_at: datetime; port: int; log_path: str | None

MAX_THREADS = 200
def snapshot(registry: SurfaceRegistry, info: DaemonInfo, now: datetime) -> dict
```

`Surface` gains `last_event: str | None` and `last_seen: datetime | None`, set in
`SurfaceRegistry.handle` for every accepted payload (clock: `self.printer.clock()`).

Shape: `boot, pid, started_at, now, port, log`, `mode {enforce, rules[], decider}`,
`judges [{name, judgments, errors{}, latency_p50, latency_p95, lag_p50, lag_p95,
input_tokens, cost_usd}]`, `totals {surfaces, events, judgments, errors{}, quarantines,
rejected, cost_usd}`, `threads [...]` newest `last_seen` first, at most 200, each
`{label, session_id, agent_id, agent_type, cwd, last_event, last_seen, events, judgments,
context, quarantine: Quarantine.as_dict() of the blocking entry | null,
judges {name: {judgments, errors{}, tripped, evidence {rule: {value, limit}},
questions {qid: {kind: "numeric", n, last, mean, ewma, min, max, streak, longest} |
{kind: "choice", counts{}, last, streak, longest}}}}}`. JSON-serialisable as is.

**Tests** (`tests/test_state.py`): a registry driven with `ScriptedJudge` and a ruled pack —
evidence and limit appear under the judge; a quarantined main thread shows as the subagent's
`quarantine` with the main label as `target`; session context falls back to the default;
threads ordered and capped; `json.dumps` succeeds.

### Task 4: `/state`, `/events`, the guard on a socket

**Consumes** `Feed`, `snapshot`, `DaemonInfo`.
**Produces** `create_app(registry, feed: Feed | None = None, info: DaemonInfo | None = None)`;
`KEEPALIVE_S = 15`. Routes exist only when `feed` and `info` are given. `/events`: `hello`
event with `{"boot"}`, then `record` events (`id:` = seq); `?since=` not an int → 400; a
`: keepalive` comment after `KEEPALIVE_S` idle; `unsubscribe` in `finally`.
`_refusal`: when `sockname` is a `str`/`bytes` (AF_UNIX) the Host check is skipped.

**Tests** (`tests/test_server.py`, added): `/state` and `/events` are 404 on an app built
without a feed; with one, `/state` answers the snapshot and `/events` yields hello, backlog,
then a record published afterwards; `since` skips; bad `since` is 400; over a real
`web.UnixSite` under `/tmp` a request with `Host: localhost` passes and one with `Origin`
is refused.

### Task 5: Paths, the socket site, `--log-dir`

**Produces**

```python
# paths.py
def state_dir(env: Mapping[str, str] = os.environ) -> Path    # $XDG_STATE_HOME/jev-watchdog or ~/.local/state/jev-watchdog
def socket_path(port: int, env: Mapping[str, str] = os.environ) -> Path   # state_dir / f"attach-{port}.sock"
def private_dir(path: Path) -> Path                            # mkdir -p, chmod 0700, returns path
```

`cli._serve(registry, printer, port, banner, feed, info, socket: Path | None)`: bind TCP
(app without feed); then unlink a stale socket, `web.UnixSite(full_runner, socket)`, chmod
0600; on `OSError` report `attach unavailable: …` and carry on. On shutdown: `feed.close()`
first, then the existing order; unlink the socket. `run --log-dir DIR` (default `runs`);
`--log` wins.

**Tests** (`tests/test_paths.py`, `tests/test_cli.py` added): XDG override; directory mode
0700; `_serve` with a `/tmp` socket answers `/state` on the socket and 404 on TCP; socket mode
0600; socket file gone after shutdown; a stale file is replaced; `--log-dir` places the log.

### Task 6: Attach client

**Produces**

```python
class AttachError(Exception): ...        # nothing to connect to / connection lost
class ControlError(Exception): ...       # the daemon said no; str(exc) is its message
class AttachClient:
    def __init__(self, socket: Path) -> None
    async def state(self) -> dict
    def events(self, since: int = 0) -> AsyncIterator[tuple[str, dict]]   # ("hello"|"record", data)
    async def quarantine(self, target: str, reason: str) -> dict
    async def release(self, target: str) -> dict
    async def context(self, target: str, text: str) -> dict
    async def aclose(self) -> None
```

aiohttp `ClientSession(connector=UnixConnector(path))`, base URL `http://localhost`, no total
timeout on the stream. `ClientError`/`OSError` → `AttachError`.

**Tests** (`tests/test_attach.py`): against the Task 5 server — state, a quarantine seen in
the next state, `ControlError` on an unknown target, events backlog then live, `AttachError`
when the socket is missing.

### Task 7: The TUI

**Consumes** the client interface (duck-typed), `render_line`, `printable`.
**Produces** `WatchdogApp(client, owns_watchdog: bool = False)`; pure helpers
`thread_row(thread: dict, decider: str) -> tuple[Text, Text]`, `header_text(state, live) -> Text`,
`detail_rows(thread, judge) -> list[tuple[Text, ...]]`, `uptime(started_at, now) -> str`.

Layout: header `Static`, `DataTable#threads`, `DataTable#detail` + `Static#context`,
`RichLog#feed` (`markup=False`, `max_lines=5000`), `Footer`. Bindings: `x` quarantine (modal
input: reason, default `manual`), `r` release, `c` context (modal input, empty clears),
`f` feed thread/all, `j` next judge, `q`/`ctrl+c` detach (label `stop` when `owns_watchdog`).
Workers: `_follow` (events, reconnect every 1 s with `since`, new `boot` clears the feed) and
`_refresh` (state at most 4×/s when dirty, every 2 s otherwise). Records kept in a
`deque(maxlen=5000)`; a filter change re-renders the log. Selection survives a refresh by
`(session_id, agent_id)`.

**Tests** (`tests/test_tui.py`, Textual `run_test` with a fake client): threads listed;
selecting the second thread filters the feed, `f` shows all; `x` + reason + enter calls
`quarantine(label, reason)`; `r` and `c` likewise; a `ControlError` becomes a notification,
not a crash; after the fake ends the stream and returns a new `boot`, the feed is cleared and
the header says live again; a label with ESC renders as `�`; helper functions unit-tested.

### Task 8: `attach` and `run --tui`

`attach [--port]`: no socket or nothing answering → `no watchdog to attach to on port N`
and exit 1; otherwise `WatchdogApp(client).run()`.
`run --tui`: `Printer(Console(quiet=True), log, feed=feed)`; serve, then
`await app.run_async()` in the same loop, SIGTERM → `app.exit()`; no socket → exit 1; the
summary goes to a fresh `Console()` after the app exits. `replay --tui` does not exist.

**Tests** (`tests/test_cli.py`): parser accepts both; `attach` without a daemon exits 1 with
the message; `run --tui` with a headless app (`WatchdogApp.run_async(headless=True)` and
an auto-quit) serves hooks and exits 0.

### Task 9: Service install / uninstall

**Produces**

```python
LABEL = "io.github.glebmish.jev-watchdog"
UNIT_NAME = "jev-watchdog.service"
def run_arguments(args: argparse.Namespace, state: Path) -> list[str]   # absolute paths, --log-dir default
def launchd_plist(command: list[str], path_env: str, log: Path) -> bytes
def systemd_unit(command: list[str], path_env: str) -> str
def install(args, *, platform=sys.platform, home=Path.home(), env=os.environ, run=subprocess.run, out=print) -> int
def uninstall(*, platform=..., home=..., run=..., out=print) -> int
```

Checks before writing: packs load; a jev judge needs an existing key file (env key only →
refuse, name `--key-file`); `--tui` refused; unknown platform refused. Files 0600.
systemd `ExecStart` quoting: `shlex.join` with `%` doubled.

**Tests** (`tests/test_service.py`): plist parses with `plistlib` and holds the command,
`PATH`, `KeepAlive.SuccessfulExit is False`, log paths, no key text even when
`TYPESAFE_API_KEY` is set; relative `--pack` and `--key-file` come out absolute; unit text
has `Restart=on-failure` and an escaped `%`; `install` calls bootout-then-bootstrap (macOS)
or daemon-reload + enable --now (Linux) through the injected runner; a failing command
returns 1, prints its output, keeps the file; `uninstall` removes the file and is calm when
nothing is installed.

### Task 10: Docs and the end-to-end run

README (Run: foreground, `--tui`, service, attach; Output; options), `docs/architecture.md`
(pipeline, components, invariants), `SECURITY.md` (socket, unit file, feed content),
design-history index. Then on this Mac: `install --judge fake:canary --enforce`, a Claude
Code session with the plugin that runs `echo canary`, `attach` to see and release it,
`uninstall`; record what happened.
