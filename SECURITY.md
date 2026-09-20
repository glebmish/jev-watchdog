# Security

jev-watchdog is a research prototype, not a security control. Do not rely on it to contain an
agent. Only the latest commit on `main` is supported.

## Known limitations, by design (not vulnerabilities)

- **Transcripts leave the machine unredacted.** Every user and assistant line of a watched
  thread, secrets the agent read included, goes to the judge backend: TypeSafe by default,
  Anthropic or OpenAI with the `claude` and `codex` judges. A context, when set, goes with it.
- **Run logs are unredacted.** `runs/*.jsonl` holds full hook payloads (prompts, tool inputs
  and outputs). The file is created 0600 (`cli._private`) and `runs/` is gitignored; no more.
- **The attach socket keeps out other users, not the agent.** `/state`, `/records`, `/history` and `/timeline` (thread
  labels, prompt and command previews, verdicts of every watched session) are served only on
  `attach-<port>.sock`, 0600 in a 0700 directory. The watched agent runs as you, so it can
  open the socket, read other sessions' feeds, and use the control endpoints there too.
- **An installed service's unit and output are files.** The unit (0600) holds the run
  options, a `--context` text and the path of the key file, never the key. `daemon.log` and
  the run logs are not rotated.
- **The localhost endpoints have no authentication.** Any local process, the watched agent
  included, can release a thread, set a context, quarantine one or post made-up hook events.
- **Evidence is forgotten with the thread.** The watchdog keeps the 200 threads it heard from
  last. A thread that is pushed out by 200 others, or a session that is left and resumed after
  that, starts again with no evidence. Whoever can post hook events can also post 200 made-up
  sessions to push a thread out; they could already release it (below).
- **Quarantine is reactive and fails open.** The action that trips a rule has already run.
  State is in memory; a stopped, hung or slow watchdog, a non-2xx or a late answer all mean
  *allow* to Claude Code.
- **The plugin trusts whatever listens on `127.0.0.1:8787`.** While the watchdog is down,
  another local process on that port (a common dev-server default) receives every hook payload
  and can answer the hooks. Single-user machines only.
- **The `claude` and `codex` judges run without a system prompt**, so transcript content can
  steer them (seen with Haiku: README, third run).
- **The Codex judge's tool lock-down is a deny-list** (`DISABLED_FEATURES` in
  `src/jev_watchdog/judge/codex_exec.py`), pinned to codex 0.153 and not checked at runtime.

## In scope: a bypass of any of these is a bug worth reporting

- A browser reaching the server: requests with an `Origin` header, a foreign `Host` or a
  non-JSON POST are refused (`server._refusal`), against cross-site posts and DNS rebinding.
- The server binding to anything but loopback (`HOST` in `cli.py`).
- Any route of `state.add_routes` answering on the TCP port, or the attach socket or its
  directory being open to other users (`serve.serve`, `paths.private_dir`).
- The TypeSafe API key reaching a launchd or systemd unit (`service.install`).
- Agent-chosen text reaching the dashboard as terminal escapes or as markup (`tui`).
- The TypeSafe API key reaching the run log, the console or a judge's child process.
- A judge session gaining tools, MCP servers, settings or hooks.
- Terminal escape sequences in agent-chosen text reaching the console (`printer.printable`).
- Command injection into the `codex` subprocess (`codex_exec.run_process`: argv, no shell).

## Reporting

If **Security → Report a vulnerability** is available on this repository, use it. Otherwise
open an issue that says only that you have a security report, with no details, and a private
channel will be arranged. Solo maintainer, best-effort response, no bounty.
