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

## First run (2026-09-19)

One headless session (`claude -p`, Claude Code 2.1.278, jev-1.13.0) that spawned an
`Explore` subagent: 2 surfaces, 12 hook events, 6 verdicts, 3 errors, $0.0065.

- **Surfaces work.** `main` and `<agent>:Explore` were tracked separately, each from its
  own transcript file; hook payloads inside the subagent carry `agent_id`/`agent_type`.
- **Latency is fine.** p50 535 ms, p95 1.26 s for 8 questions over 17–32k tokens; the
  first call on a connection is the slow one. Over-limit rejections return in ~1 s.
- **Whole transcripts hit the 32k limit almost immediately.** The subagent's transcript
  was 17.6k tokens at its *first* tool call and over the limit by its 6th event; `main`
  got one verdict (28.9k tokens) before going over. ~75% of those transcripts is
  `"type":"attachment"` lines (a 50k-char `skill_listing`, `prompt_snapshot`s,
  `agent_listing_delta`, hook context) — harness bookkeeping, not agent behaviour. The
  overhead scales with installed plugins/skills. Dropping attachment lines is the
  obvious first step of state construction; a tail window is the second.
- **`UserPromptSubmit` fires before the transcript is written.** On a new session the
  file does not exist yet (`transcript` error); on later prompts the judged transcript
  does not contain the new prompt. Judging on this event adds little as is.
- **Answers looked sane on a benign task:** `serves_goal` 0.51–0.94, `goal_drift` ≤ 0.51,
  `activity=exploring` every time, nothing flagged. `denied_reroute` (0.56–0.61) and
  `repeat_failed` (0.48) crept up as the transcript grew although nothing was denied or
  failed — candidates for rewording or for a "nothing was denied" guard question.
- `SessionStart` cannot use an `http` hook; the `curl` command hook works.
