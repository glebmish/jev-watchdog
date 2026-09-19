# jev-watchdog

Observe-only watchdog over Claude Code agents. It listens to Claude Code hooks,
treats every agent thread (the main thread and each subagent) as a separate
*surface*, and on every relevant hook event asks a judge — [Jev](https://docs.typesafe.ai)
by default — a pack of typed questions about that thread's transcript. It prints
every event and verdict and accumulates statistics. It never talks back to the agent.

> **Privacy:** the whole transcript JSONL of every watched thread is sent to
> TypeSafe **unredacted**, including any secrets the agent read. Prototype only;
> use it on sessions where that is acceptable.

## Run

```bash
uv sync
uv run jev-watchdog run                 # Jev judge; key from TYPESAFE_API_KEY or ./prototype-throwaway-key
uv run jev-watchdog run --judge fake    # offline, deterministic answers
```

In another terminal, start Claude Code with the hooks plugin:

```bash
claude --plugin-dir /path/to/jev-watchdog/plugin
```

If the watchdog is not running the hooks fail silently and Claude Code is unaffected.
`Ctrl-C` prints per-surface and global statistics. Every event, verdict and error
is also appended to `runs/<timestamp>.jsonl`.

Options: `--port` (default 8787; the plugin's URLs are fixed to 8787), `--judge jev|fake`,
`--pack pack.toml`, `--key-file`, `--log`.

## Output

```
15:02:11 a1b2c3/main                PostToolUse        Bash
15:02:12 a1b2c3/main                verdict 612ms 4.1k tok  exfil=0.95! serves_goal=0.02! goal_drift=2.88! activity=off_task!
15:02:12 a1b2c3/def456:Explore      SubagentStart      Explore
15:02:13 a1b2c3/main                judge error over_limit: ...
```

`!` marks an answer past its flag threshold. Transcripts are sent whole, so long
sessions exceed Jev's 32k-token state limit and show up as `over_limit` errors —
that is expected in this prototype.

## Questions

`pack.toml` defines the questions (`noul` = probability, `score` = ordered levels,
`choice` = one of several options) and their flag thresholds. Edit it and restart.

## Plugging in another judge

Implement the `Judge` protocol in `src/jev_watchdog/judge/base.py`
(`async judge(JudgeRequest) -> Verdict`, `async aclose()`), add a factory to
`JUDGES` in `src/jev_watchdog/judge/registry.py`, and select it with `--judge <name>`.
Only `judge/jev.py` knows about the TypeSafe SDK.

## Tests

```bash
uv run pytest            # offline
uv run pytest -m live    # two real Jev calls; needs a key
```
