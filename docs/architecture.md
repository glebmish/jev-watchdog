# Architecture

How a hook event becomes a verdict, and a verdict a quarantine. As of the code on 2026-09-20, the service and dashboard included.
Modules are under `src/jev_watchdog/`, one package per layer ([Source map](#source-map)). Module
names are unique across the package, so `surfaces` below is `core/surfaces.py` and every statement
can be checked.

## Overview

`jev-watchdog run` is one asyncio process: an aiohttp application (`app.create_app`) with
all state in memory in one `SurfaceRegistry`. It binds `127.0.0.1` only: `HOST` in `daemon/serve.py` is
a constant, and `--port` (default 8787) is the only thing that moves. Next to the port it
listens on a private unix socket, where `attach` reads what it draws (`state.add_routes`). `install` makes
that same `run` a launchd agent or systemd user unit; nothing in the process daemonizes. The plugin
(`plugin/hooks/hooks.json`) registers ten hooks, all posting to `http://127.0.0.1:8787/hooks`.
Nine are `http` hooks with a 2 s timeout: `UserPromptSubmit`, `PreToolUse`, `PostToolUse`,
`PostToolUseFailure`, `PermissionDenied`, `SubagentStart`, `SubagentStop`, `Stop`, `SessionEnd`.
`SessionStart` cannot use an `http` hook, so it is an `async` command hook that pipes its stdin
to the same URL with `curl` (2 s limit, no proxy, no output, always exit 0).

Only the hook answer is synchronous. Past the request guard, `app.hooks` answers 200
whatever happens: an empty body, or the deny body for a `PreToolUse` of a quarantined thread.
That gate (`SurfaceRegistry._gate`) is a dictionary lookup in `Quarantines.blocking` and
awaits nothing. A judging event is answered once its transcript is snapshotted (one file read,
in a thread). The rest runs in background tasks: the transcript wait, judge calls, statistics,
printing, the CUSUM and the quarantine itself.

## Source map

```text
src/jev_watchdog/
  cli.py            arguments, composition root; units run `-m jev_watchdog.cli`
  replay.py         transcripts through the same registry, offline
  core/             a hook event becomes a verdict, a verdict a quarantine
    transcript.py     thread identity, transcript reading
    pack.py           questions and the TOML pack loader
    surfaces.py       SurfaceRegistry: routing, gate, queues, workers, trips
    stats.py          read-only accumulators
    decide.py         CUSUM per (thread, judge, question)
    quarantine.py     who is quarantined, the deny body
  judge/            the judge boundary and its backends
    base.py  registry.py  jev.py  claude_agent.py  codex_exec.py  fake.py
  display/          what is shown
    printer.py        console line, run log, feed record
    feed.py           ring of display records a dashboard polls
    history.py        evidence per thread, for the charts
  daemon/           running the watchdog and reaching a running one
    app.py            the port's app: /hooks, control endpoints, request guard
    state.py          the socket-only routes a dashboard reads
    serve.py          the two sites, signals, shutdown
    client.py         the one HTTP client
    paths.py          state directory, socket path
    install.py        install / uninstall: launchd, systemd
  dashboard/        the Textual dashboard
    tui.py            the app, one polling loop, the keys
    draw.py           dicts in, Text and plots out
tests/              the same tree: tests/core/test_decide.py is for core/decide.py
```

## Layers

Three programs share the package, and each can be read without the ones below it in this list.

1. **The watchdog core**: `core/*`, `judge/*`, `daemon/app`, `replay`. A hook comes in, is
   judged, evidence accumulates, a thread is quarantined, `PreToolUse` is denied. It knows
   nothing of dashboards or services.
2. **What is shown**: `display/*` (`printer`, `feed`, `history`). The core touches this layer at two seams
   and nowhere else: `Printer._emit` (every line, to the console, the run log and the feed)
   and `History.record` / `History.mark` (called in `SurfaceRegistry._record`, `_add`,
   `release` and `_tripped`). Neither decides anything or is read back by the core.
3. **Looking and running**: the rest of `daemon/` (`state`, the read routes; `client`; `serve`,
   the two sites; `install`, launchd / systemd; `paths`) and `dashboard/` (`tui` + `draw`). `client` imports
   nothing from the package; `tui` and `draw` see only JSON, and import `printer` for the one
   line renderer (`render_line`, `printable`) and `feed` for the ring's size. `install` imports only `pack`,
   `paths` and `judge/registry`, to check options the way `run` would.

`cli` is the composition root: it parses, builds a registry and hands it to `serve` or
`replay`. Leaves that import nothing from the package: `transcript`, `pack`, `feed`, `paths`,
`client`.

## Pipeline

```text
Claude Code + plugin/hooks/hooks.json            jev-watchdog status|quarantine|release|context
  9 http hooks, SessionStart through curl          client.Client: GET/POST /quarantine,
        |  POST /hooks                              POST /release, POST /context
        v                                                  |
app.local_clients_only  <----------------------------------+
  no Origin header, Host is 127.0.0.1|localhost:<port>, POSTs are application/json,
  body <= 64 MiB; otherwise 403 / 415 / 413 and an `error payload` line
        |                                                  |
        v                                                  v
SurfaceRegistry.handle                           SurfaceRegistry.quarantine | release |
  ^   surface = (session_id, agent_id | "main")    set_context  --> Quarantines, contexts
  |     |
  |     +-- PreToolUse --> _gate: Quarantines.blocking --> deny body | empty 200
  |     +-- SessionStart, SubagentStart: register, print    SessionEnd: end mark to workers
  |     +-- judging events, under the Surface.arrival lock:
  |           _snapshot     read_lines + conversation_lines + trimmed_lines, in a thread
  |           _own_lines    cut just after the event's own tool_result
  |           not there yet --> intake queue; _caught_up polls, up to --transcript-wait
  |             v  Job(event, lines, context)
  |         one queue and worker per (surface, judge)       _enqueue, _work
  |             v
  |         Judge.judge(JudgeRequest) --> Verdict           jev | claude | codex | fake
  |             v
  |         _record: SurfaceStats, GlobalStats, Printer (console, runs/<timestamp>.jsonl)
  |             v
  |         Decider.fold (CUSUM, tool events only) -- Trip --> _tripped
  |             first --judge and --enforce: Quarantines.add, which _gate reads
  |             otherwise: a "would quarantine" line
  |
replay.run_cases: the same handle(), synthetic payloads over transcript prefixes, no wait

Printer._emit: one display record per line --> console (render_line), Feed (ring of 2000),
                                                 run log (the fuller record)
serve.serve: TCPSite 127.0.0.1:<port>          app.create_app(registry)
             UnixSite attach-<port>.sock, 0600: the same app + state.add_routes(app, registry, feed)
                 GET /state     snapshot: threads, statistics, evidence, quarantines
                 GET /records   the feed after ?since, with the process's boot id
                 GET /history   History.series: one thread's evidence per verdict, marks
                 GET /timeline  History.timeline: every thread, bucketed on the server
                      ^
client.Client --------+-- tui.WatchdogApp, drawn by draw.py (`attach`; `run --tui` on its own socket)
   on_socket | on_port    x / r / c --> POST /quarantine | /release | /context, same socket
        ^
        +-- cli status | quarantine | release | context, over the port
```

## Components

| Module | Owns |
|---|---|
| `cli` | Arguments, API key, run log (0600), building the registry, dispatch to `serve`, `replay`, `install` and the control calls. |
| `replay` | Cuts transcripts into steps, feeds `handle`, checks `.expect.toml`. |
| `core/transcript` | `SurfaceKey`, transcript paths, bounded read, conversation filter, `tool_result_end`. |
| `core/pack` | `Question` and the validating TOML pack loader; context wording. |
| `core/surfaces` | `SurfaceRegistry`: routing, gate, transcript wait, queues, workers, trips. |
| `core/stats` | Read-only accumulators: counts, EWMA, streaks, latency, lag, tokens, cost. |
| `core/decide` | `Decider`: CUSUM per (thread, judge, question). Pure logic, no I/O. |
| `core/quarantine` | `Quarantines` book, main-thread scope, the `deny_body` text. |
| `judge/base` | `Judge` protocol, `JudgeRequest`, `Verdict`, shared payload and answer schema. |
| `judge/registry` | `--judge` spec to judge (`JUDGES`); lazy backend imports. |
| `judge/jev` | Jev through `typesafe_sdk`; error kinds; cost from input tokens. |
| `judge/claude_agent` | Claude through `claude_agent_sdk`: one-shot query, no tools. |
| `judge/codex_exec` | GPT through a `codex exec` subprocess, tool features disabled. |
| `judge/fake` | Deterministic offline judge; `fake:WORD` marker mode. |
| `display/printer` | Display records, `render_line` (console and dashboard), JSONL run log, feed; `printable` makes control characters visible. |
| `display/feed` | Ring buffer of display records with a rising `seq` and the process's `boot` id. |
| `display/history` | Last 500 verdicts per thread and judge as shares of each rule's limit; quarantine, release and trip marks; `series` and the bucketed `timeline`. Decides nothing. |
| `daemon/app` | aiohttp app of the port: `/hooks`, control endpoints, request guard, body cap. Knows nothing of the dashboard. |
| `daemon/state` | What a dashboard reads and its routes (`add_routes`): `snapshot`, `/records`, `/history`, `/timeline`. |
| `daemon/serve` | The two sites (port, private socket), signals, shutdown order; `dashboard` for `run --tui`, `attach`. |
| `daemon/client` | `Client`: the one HTTP client, `on_port` for the control subcommands, `on_socket` for the dashboard. |
| `daemon/paths` | State directory (`$XDG_STATE_HOME/jev-watchdog`), socket path per port, 0700 directory. |
| `daemon/install` | `install` / `uninstall`: launchd plist, systemd unit, absolute `run` arguments. |
| `dashboard/tui` | `WatchdogApp` (Textual): the widgets, one polling loop, the keys. |
| `dashboard/draw` | What the dashboard shows, as pure functions from the watchdog's dicts to `Text` and plots. |

## Invariants

- **The hook is answered at once; judging is asynchronous.** `SurfaceRegistry.handle` never
  raises and never awaits a judge. `app.hooks` turns bad JSON and handler exceptions into an
  empty 200 and a `payload` error.
- **Per-thread event order is kept.** `Surface.arrival` (a fair asyncio lock) is held from a
  hook's arrival until `_admit`, so a slow read cannot reorder events. `_admit` dispatches
  directly only when `backlog` is 0; otherwise the event joins the FIFO `intake` queue, which
  one task (`_take_in`) empties in order.
- **Serial per (surface, judge), concurrent across.** `_enqueue` gives each judge its own
  queue and worker per surface, so a slow judge never delays a fast one. The claude and codex
  judges also cap their processes (`MAX_CONCURRENCY`: 4 and 2).
- **One CUSUM contribution per executed action.** `Decider.fold` takes only `TOOL_EVENTS`
  (`PostToolUse`, `PostToolUseFailure`, `PermissionDenied`), once per `tool_use_id`. `Stop` and
  `UserPromptSubmit` verdicts re-judge the last action and are ignored. A tool event is judged
  on the transcript cut at its own result (`_own_lines`, `transcript.tool_result_end`), so
  parallel calls do not all score the same last action.
- **Evidence is rounded** to `EVIDENCE_DIGITS` (9) in `Decider.fold`: without it two 0.7s
  against ref 0.6 fall short of a limit of 0.2 where one 0.8 reaches it.
- **Only the first `--judge` quarantines, and only with `--enforce`** (`_tripped`). Every
  judge's verdicts are folded, so the others print `would quarantine`. A manual quarantine
  holds either way (`_gate` ignores `enforce`); `release` resets evidence (`Decider.reset`).
- **Scope.** `Quarantines.blocking` returns the thread's own entry, else its session's
  main-thread entry: a quarantined main thread blocks its subagents, a subagent only itself.
- **One display record, three readers.** `Printer._emit` builds what a line shows
  (`display_record`), prints `render_line` of it, publishes a copy with every text cut at
  `MESSAGE_LIMIT` (500) to the `Feed`, and writes the run log, which gets the fuller record
  (the hook payload, the whole verdict) where there is one. The feed's ring stays small
  whatever a hook carries.
- **The feed never holds up a hook.** `Feed.publish` appends to a ring and nothing else: there
  are no subscribers to serve. A dashboard asks `/records?since=<its last seq>`.
- **Session content is not on the port.** `state.add_routes` is called only for the app that
  `serve.serve` puts on the unix socket (0600, in a 0700 directory); `app.create_app` has no
  such routes to switch on. The TCP port is bound first; holding it, the process owns that
  port's socket path and replaces a stale file. On a unix connection `app._refusal` skips
  the `Host` check (no port, and no page can open a socket file) and keeps the `Origin` and
  content-type checks. Without a socket `run` carries on and says `attach unavailable`;
  `run --tui` exits 1.
- **Charts are drawn from `History`, not from the feed.** `SurfaceRegistry._record` stores the
  evidence each verdict left (`folded` marks tool events, the only ones that move it), and
  `_add`, `release` and a non-enforced `_tripped` store marks; a release also appends an empty
  point per judge, because `Decider.reset` starts every judge's evidence over. `/timeline` is
  bucketed on the server (at most `MAX_TIMELINE_BUCKETS` cells a thread), and a cell is
  the worst share of a limit among its points; `History` stores shares, so no reader divides.
- **The dashboard polls, in one loop** (`tui.WatchdogApp._watch`): `/records` every `TICK_S`
  (0.25 s), and `/state` plus the chart that is up when records came, a key was pressed or
  `IDLE_REFRESH_S` (2 s) passed. A `boot` other than the last one is a restarted watchdog: the
  feed is cleared and read from 0. "Live" is whether the last poll got an answer. The loop
  survives everything but cancellation: a refusal (an older watchdog may not know a chart's
  request) or an answer it cannot draw is a notification, once, and the next tick asks again.
  Thread rows are updated in place, oldest first, and only when they changed, so none moves
  under the cursor and an idle screen stays idle. Widgets are kept from `on_mount`: `query_one`
  searches the screen on top, and state keeps arriving while the question of `x` or `c` is
  open. It computes nothing: `draw.py` turns the watchdog's dicts into `Text` and plots.
- **Agent-chosen text reaches the dashboard as `Text` through `printable`**, border titles
  through `textual.markup.escape`, notifications with `markup=False`.
- **A unit never holds the key.** `install.install` checks what `run` would refuse (packs,
  duplicate judges, a key file for the Jev judge), writes absolute paths and the current
  `PATH`, pins `XDG_STATE_HOME` to the installing shell's (a service sees no shell profile, and
  `paths.socket_path` must give the service and `attach` the same answer), and refuses when
  only `TYPESAFE_API_KEY` is set. `install` reports `running` only when something accepts a
  connection on the socket: the file alone may be a killed watchdog's. launchd: `KeepAlive.SuccessfulExit
  = false`, so a stop stays stopped and a crash or a taken port is retried every 10 s.
- **The registry forgets by age, in one place.** `SurfaceRegistry.surfaces` is kept in the
  order threads were last heard from (`_surface_for` re-inserts), and on every event `_forget`
  drops from the old end whatever has been silent for `THREAD_TTL` (24 h): its workers are
  cancelled, its evidence (`Decider.reset`), history (`History.forget`) and, with the last
  thread of a session, its context go with it. There is no cap on how many: a swarm is kept
  whole. A quarantined thread is never dropped: it has to be there to be released. A forgotten
  thread that speaks again is a new thread, without its evidence. `GlobalStats` still counts
  everything that was seen.
- **A poll is bounded, memory is not.** `state.shown` sends a dashboard the `MAX_SHOWN` (200)
  threads heard from last plus every quarantined one, in the registry's order, for `/state`
  and `/timeline` alike: a slice, no sort. The closing summary prints every thread still
  remembered, which is about 3 s per 1000.
- **Fails open, state in memory.** `Quarantines`, `Decider` and `contexts` are plain dicts; a
  restart forgets them. `MAX_BODY_BYTES` is 64 MiB because a 413 would mean allow;
  `Printer._log` drops the run log on a write error rather than fail a deny.
- **Every backend gets the same request.** `request_payload` / `build_prompt` in `judge/base`
  produce Jev's body (`state`, `questions`); `request_state` adds `user_context` only when
  there is a context. `answer_schema` and `schema_answers` stand in for Jev's typed answers.
- **One module per SDK or CLI.** `typesafe_sdk` is imported only by `judge/jev`,
  `claude_agent_sdk` only by `judge/claude_agent`, and only `judge/codex_exec` starts `codex`.
  `judge/registry` imports them lazily.
- **Judges are disarmed.** Claude (`ClaudeAgentJudge.options`): `tools=[]`,
  `setting_sources=[]`, `strict-mcp-config`, `MAX_TURNS = 1`, an empty working directory.
  Codex (`CodexExecJudge.command`): no such switch, so `DISABLED_FEATURES` and
  `CONFIG_OVERRIDES`, `-s read-only`, `--ignore-user-config`, `--ephemeral`.
- **A failure after the verdict does not kill the worker.** `_judge` wraps `judge.judge()` and
  `_record` alike and reports through `_judge_error`; `_start` reports a task that dies anyway.

## Claude Code hook behaviour this design depends on

Observed by the author on Claude Code 2.1.278 on 2026-09-19, or read in its hooks reference
that day (the `bypassPermissions` row): observed, version-pinned, not re-verified.

| Fact | Where the code deals with it |
|---|---|
| `async` applies only to command hooks; `http` hooks are synchronous | `app.hooks` answers at once; judging runs in the `_work` tasks |
| `SessionStart` cannot use an `http` hook | the `curl` command hook in `plugin/hooks/hooks.json`; `tests/test_plugin.py` |
| `UserPromptSubmit` fires before the transcript is written | `_snapshot` reads a missing file as empty and `_dispatch` only registers; later prompts are judged without the new prompt; `Decider.fold` ignores the event |
| `PostToolUse` usually arrives before its tool call is in the transcript | `_awaited`, `_caught_up`, `TRANSCRIPT_WAIT_S`, `--transcript-wait` |
| A tool call Claude Code itself denies fires no hook | not dealt with: the attempt is only history at the next executed action |
| Subagent payloads carry `agent_id` / `agent_type`; subagents have their own transcript | `transcript.surface_key`, `transcript.resolve_transcript_path`, `_surface_for` |
| A `PreToolUse` `deny` holds in `bypassPermissions` mode | `Quarantine.deny_body` is the one answer in every mode |
| A refused connection, a timeout or a non-2xx is non-blocking, so it means allow | always-200 `app.hooks`, `MAX_BODY_BYTES`, the threaded `_snapshot` |

## External dependencies

- **TypeSafe API** through `typesafe-sdk` (`judge/jev`). Key from `TYPESAFE_API_KEY`, else the
  `--key-file` (`cli.resolve_api_key`); the variable is then dropped from the environment.
  32k-token state limit: lines are trimmed to the conversation (`transcript.trimmed_lines`)
  and a long thread is compacted per judge, by its own verdicts, under a 64 kB budget
  (`compact.compacted`, called in `_judge`; the standings are kept on the `Surface`).
- **Claude Agent SDK** and the local Claude Code login, to which usage is billed. It starts a
  `claude` process per judgment: 4.3–6.5 s p50, ~1.2–1.6k harness tokens that cannot be removed.
- **Codex CLI** and a ChatGPT login (`codex login`). The feature list is pinned to codex 0.153
  and not checked at runtime; other versions ignore unknown names. Eight concurrent processes
  got ~15% of requests refused (hence `MAX_CONCURRENCY = 2`), and 444 judgments in 11 minutes
  emptied the plan's 5-hour Codex allowance.

Usage and the measured runs are in [`../README.md`](../README.md); the original specs are in
[`design-history/`](design-history/README.md).
