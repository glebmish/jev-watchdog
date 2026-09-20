# Architecture

How a hook event becomes a verdict, and a verdict a quarantine. As of the code on 2026-09-20, the service and dashboard included.
Modules are under `src/jev_watchdog/`; names are given so that every statement can be checked.

## Overview

`jev-watchdog run` is one asyncio process: an aiohttp application (`server.create_app`) with
all state in memory in one `SurfaceRegistry`. It binds `127.0.0.1` only: `HOST` in `cli.py` is
a constant, and `--port` (default 8787) is the only thing that moves. Next to the port it
listens on a private unix socket, where `attach` reads `/state` and `/events`. `install` makes
that same `run` a launchd agent or systemd user unit; nothing in the process daemonizes. The plugin
(`plugin/hooks/hooks.json`) registers ten hooks, all posting to `http://127.0.0.1:8787/hooks`.
Nine are `http` hooks with a 2 s timeout: `UserPromptSubmit`, `PreToolUse`, `PostToolUse`,
`PostToolUseFailure`, `PermissionDenied`, `SubagentStart`, `SubagentStop`, `Stop`, `SessionEnd`.
`SessionStart` cannot use an `http` hook, so it is an `async` command hook that pipes its stdin
to the same URL with `curl` (2 s limit, no proxy, no output, always exit 0).

Only the hook answer is synchronous. Past the request guard, `server.hooks` answers 200
whatever happens: an empty body, or the deny body for a `PreToolUse` of a quarantined thread.
That gate (`SurfaceRegistry._gate`) is a dictionary lookup in `Quarantines.blocking` and
awaits nothing. A judging event is answered once its transcript is snapshotted (one file read,
in a thread). The rest runs in background tasks: the transcript wait, judge calls, statistics,
printing, the CUSUM and the quarantine itself.

## Pipeline

```text
Claude Code + plugin/hooks/hooks.json            jev-watchdog status|quarantine|release|context
  9 http hooks, SessionStart through curl           control.call: GET/POST /quarantine,
        |  POST /hooks                              POST /release, POST /context
        v                                                  |
server.local_clients_only  <-------------------------------+
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
  |           _snapshot     read_lines + conversation_lines, in a thread
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

Printer._emit: one display record per line --> console (render_line), Feed (ring of 2000)
                                                                          |
cli._serve: TCPSite 127.0.0.1:<port>   create_app(registry)               |
            UnixSite attach-<port>.sock, 0600: create_app(registry, feed, info)
                 GET /state   state.snapshot: threads, statistics, evidence, quarantines
                 GET /events  SSE: hello {boot}, backlog after ?since, live records
                      ^
attach.AttachClient --+-- tui.WatchdogApp (`attach`; `run --tui` on its own socket)
                          x / r / c --> POST /quarantine | /release | /context, same socket
```

## Components

| Module | Owns |
|---|---|
| `cli` | Arguments, API key, run log (0600), the two sites, `run` / `replay` / `attach` / control entry points. |
| `server` | aiohttp app: `/hooks`, control endpoints, `/state` and `/events` when given a feed, request guard, body cap. |
| `feed` | Ring buffer of display records with a rising `seq`; subscribers, the slow ones dropped. |
| `state` | `snapshot`: the registry as one JSON document, newest 200 threads. |
| `paths` | State directory (`$XDG_STATE_HOME/jev-watchdog`), socket path per port, 0700 directory. |
| `attach` | `AttachClient`: state, events (SSE) and control over the socket. |
| `tui` | `WatchdogApp` (Textual): threads, one thread's statistics, feed, control keys. |
| `service` | `install` / `uninstall`: launchd plist, systemd unit, absolute `run` arguments. |
| `surfaces` | `SurfaceRegistry`: routing, gate, transcript wait, queues, workers, trips. |
| `transcript` | `SurfaceKey`, transcript paths, bounded read, conversation filter, `tool_result_end`. |
| `decide` | `Decider`: CUSUM per (thread, judge, question). Pure logic, no I/O. |
| `quarantine` | `Quarantines` book, main-thread scope, the `deny_body` text. |
| `pack` | `Question` and the validating TOML pack loader; context wording. |
| `stats` | Read-only accumulators: counts, EWMA, streaks, latency, lag, tokens, cost. |
| `printer` | Display records, `render_line` (console and dashboard), JSONL run log, feed; `printable` makes control characters visible. |
| `control` | HTTP client of the control subcommands; no proxy; spots a non-watchdog. |
| `replay` | Cuts transcripts into steps, feeds `handle`, checks `.expect.toml`. |
| `judge/base` | `Judge` protocol, `JudgeRequest`, `Verdict`, shared payload and answer schema. |
| `judge/registry` | `--judge` spec to judge (`JUDGES`); lazy backend imports. |
| `judge/jev` | Jev through `typesafe_sdk`; error kinds; cost from input tokens. |
| `judge/claude_agent` | Claude through `claude_agent_sdk`: one-shot query, no tools. |
| `judge/codex_exec` | GPT through a `codex exec` subprocess, tool features disabled. |
| `judge/fake` | Deterministic offline judge; `fake:WORD` marker mode. |

## Invariants

- **The hook is answered at once; judging is asynchronous.** `SurfaceRegistry.handle` never
  raises and never awaits a judge. `server.hooks` turns bad JSON and handler exceptions into an
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
  (`display_record`), prints `render_line` of it, and publishes a copy with every text cut at
  `MESSAGE_LIMIT` (500) to the `Feed`. The run log is written separately and keeps the full
  payloads, so the feed's ring stays small whatever a hook carries.
- **The feed never holds up a hook.** `Feed.publish` is synchronous and only `put_nowait`s. A
  subscriber whose queue (1000) is full is removed and its stream ended with `None`; the
  dashboard comes back with `?since=<last seq>`. `Feed.close()` runs first at shutdown, or the
  open `/events` handlers would hold `runner.cleanup()` up.
- **Session content is not on the port.** `/state` and `/events` are routed only in the app
  that `cli._serve` puts on the unix socket (0600, in a 0700 directory). The TCP port is bound
  first; holding it, the process owns that port's socket path and replaces a stale file. On a
  unix connection `server._refusal` skips the `Host` check (no port, and no page can open a
  socket file) and keeps the `Origin` and content-type checks. Without a socket `run` carries
  on and says `attach unavailable`; `run --tui` exits 1.
- **The dashboard computes nothing.** Statistics and evidence come from `/state`, asked again
  at most every `TICK_S` (0.25 s) while records arrive and every `IDLE_REFRESH_S` (2 s)
  otherwise. A new `boot` in `hello` is a restarted watchdog: the feed is cleared and read
  from 0. Thread rows are updated in place, oldest first, so none moves under the cursor.
  Widgets are kept from `on_mount`: `query_one` searches the screen on top, and state keeps
  arriving while the question of `x` or `c` is open.
- **Agent-chosen text reaches the dashboard as `Text` through `printable`**, border titles
  through `textual.markup.escape`, notifications with `markup=False`.
- **A unit never holds the key.** `service.install` checks what `run` would refuse (packs,
  duplicate judges, a key file for the Jev judge), writes absolute paths and the current
  `PATH`, and refuses when only `TYPESAFE_API_KEY` is set. launchd: `KeepAlive.SuccessfulExit
  = false`, so a stop stays stopped and a crash or a taken port is retried every 10 s.
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
| `async` applies only to command hooks; `http` hooks are synchronous | `server.hooks` answers at once; judging runs in the `_work` tasks |
| `SessionStart` cannot use an `http` hook | the `curl` command hook in `plugin/hooks/hooks.json`; `tests/test_plugin.py` |
| `UserPromptSubmit` fires before the transcript is written | `_snapshot` reads a missing file as empty and `_dispatch` only registers; later prompts are judged without the new prompt; `Decider.fold` ignores the event |
| `PostToolUse` usually arrives before its tool call is in the transcript | `_awaited`, `_caught_up`, `TRANSCRIPT_WAIT_S`, `--transcript-wait` |
| A tool call Claude Code itself denies fires no hook | not dealt with: the attempt is only history at the next executed action |
| Subagent payloads carry `agent_id` / `agent_type`; subagents have their own transcript | `transcript.surface_key`, `transcript.resolve_transcript_path`, `_surface_for` |
| A `PreToolUse` `deny` holds in `bypassPermissions` mode | `Quarantine.deny_body` is the one answer in every mode |
| A refused connection, a timeout or a non-2xx is non-blocking, so it means allow | always-200 `server.hooks`, `MAX_BODY_BYTES`, the threaded `_snapshot` |

## External dependencies

- **TypeSafe API** through `typesafe-sdk` (`judge/jev`). Key from `TYPESAFE_API_KEY`, else the
  `--key-file` (`cli.resolve_api_key`); the variable is then dropped from the environment.
  32k-token state limit and no windowing: a long session shows up as `over_limit` errors.
- **Claude Agent SDK** and the local Claude Code login, to which usage is billed. It starts a
  `claude` process per judgment: 4.3–6.5 s p50, ~1.2–1.6k harness tokens that cannot be removed.
- **Codex CLI** and a ChatGPT login (`codex login`). The feature list is pinned to codex 0.153
  and not checked at runtime; other versions ignore unknown names. Eight concurrent processes
  got ~15% of requests refused (hence `MAX_CONCURRENCY = 2`), and 444 judgments in 11 minutes
  emptied the plan's 5-hour Codex allowance.

Usage and the measured runs are in [`../README.md`](../README.md); the original specs are in
[`design-history/`](design-history/README.md).
