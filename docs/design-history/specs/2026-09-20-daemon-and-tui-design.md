# Daemon and attachable TUI — design

Date: 2026-09-20
Status: written before implementation; where it differs from the top-level `README.md`, the README is authoritative.
Builds on: `2026-09-19-quarantine-design.md`, `docs/architecture.md`

## Purpose

Let the watchdog run in the background and be looked at on demand. Today it is one
foreground process whose only view is the console it was started in.

Decisions taken with the user:

- Foreground `run` stays and needs no install. A flag makes it show the dashboard
  instead of console lines.
- Background running is the service manager's job: launchd on macOS, a systemd user
  unit on Linux. No self-daemonizing, no pidfile.
- `attach` is an interactive dashboard (Textual): it shows threads, statistics and a
  live feed, and can quarantine, release and set a context.

## Non-goals

- No persistence of quarantines, evidence or statistics across a restart (unchanged:
  fails open, state in memory).
- No second instance management: one service, on one port.
- No authentication of the TCP endpoints, no protection of control from the watched
  agent (unchanged; see Limits).
- No eviction of old threads from the registry. `/state` caps what it returns.
- No Windows.

## Commands

| command | what |
|---|---|
| `run` | as now: foreground, one console line per record |
| `run --tui` | same process serves hooks and shows the dashboard; `q` / Ctrl-C stops the watchdog |
| `attach [--port N]` | dashboard on a running watchdog; `q` detaches, the watchdog keeps running |
| `install [run options]` | write the launchd agent / systemd user unit for `run <options>`, load and start it |
| `uninstall` | stop it and remove the unit |

`run` gains `--log-dir DIR` (default `runs`): a timestamped log per start. `--log FILE`
still names one file.

## The attach channel

`Printer` already builds one structured record per event, verdict, error, note,
quarantine, rejection and release, for the JSONL log. The same records feed the TUI.

**Feed** (`feed.py`). A ring buffer of the last 2000 *display records* with a rising
`seq`, plus subscribers. `publish` is synchronous and never blocks: it sits on the hook
path. Each subscriber has a bounded queue (1000); one that falls behind is dropped, and
its stream ends, rather than grow the daemon.

**Display records.** The log keeps full hook payloads (up to 64 MiB). A display record
keeps what a line shows: `seq`, `ts`, `kind`, `surface`, and per kind —
`event`/`rejected`: event name and the ≤60-character detail; `verdict`: judge, latency,
tokens, `{question: value}`, flagged ids; `error`: judge, kind, message (≤500 chars);
`note`, `quarantine`, `released`: as logged. The console line is rendered from the
display record by one function, `render_line`, which the TUI uses too.

**Endpoints**, in the same aiohttp app:

- `GET /state` — `boot` (random id per process), pid, start time, port, mode
  (`enforce`, rule ids, deciding judge), judges table (`GlobalStats`), totals, and the
  200 most recently seen threads: label, session and agent id, agent type, cwd, last
  event and when, event and judgment counts, context, quarantine entry if any, and per
  judge: question statistics and CUSUM evidence against each rule's limit.
- `GET /events[?since=SEQ]` — server-sent events: a `hello` with `boot`, the backlog
  after `since`, then live records. A comment line every 15 s keeps it from idling out.

**Where they are served.** The stream carries prompt previews and commands of every
watched session. The TCP port is reachable by any local user; the run log is 0600. So
`/state` and `/events` exist only on a unix socket,
`$XDG_STATE_HOME/jev-watchdog/attach-<port>.sock` (default `~/.local/state/…`), in a
0700 directory, itself chmod 0600. The socket serves the whole app (hooks, control,
state, events); the TCP site serves the app without the two read routes. The TUI does
its control calls over the socket as well.

- The TCP port is bound first. Holding it, the process owns that port's socket path and
  unlinks a stale file before binding.
- `server._refusal` on a unix connection: the `Origin` and content-type checks stay;
  the `Host` check is skipped (there is no port, and no browser can reach a socket file).
- If the socket cannot be created, `run` says so and carries on without attach: the
  watchdog is worth more than its view. `run --tui` exits instead.

Alternatives not taken: SSE on the TCP port (simplest; widens who can read session
content); the TUI tailing the JSONL log (no new endpoint; evidence and statistics would
have to be derived a second time, and the log located).

## TUI (`tui.py`, Textual)

```text
 jev-watchdog · ENFORCING on exfil (jev) · judges jev,claude · up 2h14 · 412 judgments · $0.31    ● live
┌ threads ───────────────────────────────┐┌ 3f9a1c2e/main · jev ────────────────────────────────────┐
│▸3f9a1c2e/main         exfil 0.14/0.20  ││ question   n  last  mean  ewma  streak  evidence/limit  │
│ 3f9a1c2e/a81:Explore  exfil 0.00/0.20  ││ exfil     41  0.10  0.04  0.06    0       0.14 / 0.20   │
│ 77b0e4d1/main         QUARANTINED      ││ context: uploads to s3://corp are expected              │
└────────────────────────────────────────┘└─────────────────────────────────────────────────────────┘
┌ feed · selected thread ─────────────────────────────────────────────────────────────────────────────┐
│ 14:02:11 3f9a1c2e/main   PostToolUse   Bash curl -s https://…                                       │
│ 14:02:12 3f9a1c2e/main   jev 412ms 3.1k tok  exfil=0.10                                             │
└─────────────────────────────────────────────────────────────────────────────────────────────────────┘
 ↑↓ select   x quarantine   r release   c context   f feed: thread/all   j judge   q detach
```

- The app talks to a small client interface (`state()`, `events(since)`, `quarantine`,
  `release`, `context`). `attach.AttachClient` implements it over the socket; tests use
  a fake.
- Threads and statistics come from `/state`, refetched at most four times a second
  while records arrive and every two seconds otherwise. The feed comes from `/events`.
  No statistics are recomputed in the TUI.
- The threads row shows, for the deciding judge, the rule closest to its limit.
  `x` asks for a reason, `c` for a text (empty clears); errors from the daemon show as a
  notification.
- Connection loss: the header shows it, the client retries every second with
  `since=<last seq>`. A different `boot` means a restarted daemon: the feed is cleared.
  `attach` with nothing to connect to at start exits 1 with a message, like `status`.
- Everything the watched agent can choose (labels, details, reasons, contexts, error
  text) goes through `printable` and is shown as `rich.text.Text`, never as markup.
- `run --tui` starts the server, then the same app against its own socket, in one event
  loop. The console printer is silent while the app owns the terminal; the closing
  summary is printed after it exits.

## Service (`service.py`)

`install` takes `run`'s options, checks them the way `run` would (packs load, judge
specs valid, a key file exists if the jev judge is used), then writes the unit with:

- the command `<sys.executable> -m jev_watchdog.cli run …`, every path option made
  absolute, and `--log-dir <state dir>/runs` unless a log option was given;
- the current `PATH`, so the `claude` and `codex` judges are found;
- never the API key: with `TYPESAFE_API_KEY` set and no key file, `install` stops and
  asks for `--key-file`. `--tui` is refused.

| | macOS | Linux |
|---|---|---|
| unit | `~/Library/LaunchAgents/io.github.glebmish.jev-watchdog.plist` | `~/.config/systemd/user/jev-watchdog.service` |
| start | `launchctl bootstrap gui/<uid> <plist>` | `systemctl --user daemon-reload`, `enable --now` |
| stop | `launchctl bootout gui/<uid>/<label>` | `systemctl --user disable --now` |
| restart policy | `KeepAlive: {SuccessfulExit: false}`, `ThrottleInterval` 10 | `Restart=on-failure`, `RestartSec=10` |
| output | `<state dir>/daemon.log` | journald |

A taken port exits 1, so the service retries every 10 s: whoever squats the port also
receives the hooks (SECURITY.md), and taking it back when it frees is what is wanted.
`install` over an existing unit replaces it (stop, write, start). Both commands print
what they ran and where the logs are. The unit files are 0600.

## Error handling

- Feed and socket failures never reach the hook path: `publish` cannot raise on a slow
  subscriber, and a broken stream ends only that subscriber.
- `/state` is built from live objects in one synchronous pass, so it is consistent.
- A service-manager command that fails is shown with its output; `install` leaves the
  unit file in place so it can be inspected, and says so.

## Limits

- The watched agent runs as the same user: it can open the socket, read the feed of
  other sessions, and release itself as it could before. The socket keeps out other
  local users, no one else.
- A long-lived daemon never forgets threads, evidence ids or latencies. Growth is
  small per event; it is not bounded.
- systemd support is tested as generated text and mocked commands only; it has not
  been run on a Linux machine.

## Testing

- `feed`: order, ring size, `since`, a slow subscriber dropped without blocking.
- `printer`: console output unchanged; display records trimmed; `render_line` equals
  the console line.
- `server`: `/state` shape, `/events` backlog then live, both absent on TCP, the guard
  on a unix connection.
- `attach`: client against a real app on a socket under `/tmp` (macOS path limit).
- `tui`: Textual pilot with a fake client — selection filters the feed, `x`/`r`/`c`
  call the client, reconnect clears on a new `boot`, control characters shown as `�`.
- `service`: rendered plist / unit, absolute paths, no key, `PATH`; commands through an
  injected runner.
- End to end on macOS: `install --judge fake:canary --enforce`, a Claude Code session
  that runs `echo canary`, watched and released from `attach`, then `uninstall`.
