"""GPT judge via the Codex CLI (`codex exec`). The only module that knows about codex.

Each judgment is a fresh one-shot `codex exec` through the local ChatGPT login: no user
config (so no plugins, hooks or notify program), no session files, an empty working directory.

The model gets the same input as Jev: no instructions and the request payload as the only
message. `--output-schema` stands in for Jev's typed answers. What the harness still adds
depends on the model: ~0.5k tokens for gpt-5.5, ~3.5k for the code-mode models
(gpt-5.6-*, gpt-6-astra), whose tool preamble cannot be removed.

Codex has no "no tools" switch and no turn limit, so the judge is disarmed instead: every
tool-providing feature is disabled, the code-mode host is off (the `exec` tool those models
still see fails closed), and the read-only sandbox refuses whatever is left (`apply_patch`).
A judge that tries to act on a transcript wastes a step and then has to answer.
"""

import asyncio
import hashlib
import json
import shutil
import tempfile
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

from jev_watchdog.judge.base import (
    JudgeError,
    JudgeRequest,
    Verdict,
    answer_schema,
    build_prompt,
    schema_answers,
)

# The harness adds least to this one; pick another with `codex:<model>`.
DEFAULT_MODEL = "gpt-5.5"
TIMEOUT_S = 120.0
# Every judgment starts a `codex` process; replaying a corpus would otherwise start one per
# case at once. Eight at once (two codex judges x 4, ~40 requests a minute) got ~15% of the
# requests refused with 403 by the edge in front of the ChatGPT backend.
MAX_CONCURRENCY = 2
# The lowest effort every model on a ChatGPT login accepts; the stand-in for "thinking off".
LOW_EFFORT = "low"

# Everything that gives the model a tool, a skill listing or extra instructions (codex 0.153).
# Unknown names are ignored by other versions.
DISABLED_FEATURES = (
    "shell_tool",
    "unified_exec",
    "code_mode",
    "code_mode_host",
    "multi_agent",
    "multi_agent_v2",
    "collaboration_modes",
    "apps",
    "plugins",
    "remote_plugin",
    "hooks",
    "goals",
    "browser_use",
    "browser_use_external",
    "in_app_browser",
    "in_app_local_automation",
    "computer_use",
    "image_generation",
    "view_image",
    "sleep_tool",
    "skill_search",
    "skill_mcp_dependency_install",
    "tool_suggest",
    "mentions_v2",
    "personality",
    "workspace_dependencies",
    "shell_snapshot",
    "guardian_approval",
)
CONFIG_OVERRIDES = (
    'instructions=""',  # the input is the request payload and nothing else
    'web_search="disabled"',
    "project_doc_max_bytes=0",  # no AGENTS.md
    "include_permissions_instructions=false",
    "include_apps_instructions=false",
    "include_environment_context=false",
    "skills.bundled.enabled=false",
)

RunFn = Callable[[list[str], str, str], Awaitable[tuple[int, str, str]]]


async def run_process(argv: list[str], stdin: str, cwd: str) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=cwd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await proc.communicate(stdin.encode())
    finally:
        if proc.returncode is None:  # timed out or cancelled
            proc.kill()
            await proc.wait()
    return proc.returncode, stdout.decode(errors="replace"), stderr.decode(errors="replace")


class CodexExecJudge:
    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        thinking: bool = False,
        timeout_s: float = TIMEOUT_S,
        max_concurrency: int = MAX_CONCURRENCY,
        run_fn: RunFn = run_process,
    ) -> None:
        self.name = f"codex:{model}"
        self.model = model
        self.thinking = thinking
        self.timeout_s = timeout_s
        self._run = run_fn
        self._slots = asyncio.Semaphore(max_concurrency)
        # An empty working directory: nothing for the CLI to discover or load. The answer
        # schemas live one level up so that it stays empty.
        self._home = tempfile.mkdtemp(prefix="jev-watchdog-judge-")
        self._cwd = str(Path(self._home) / "cwd")
        Path(self._cwd).mkdir()

    def command(self, schema_path: Path) -> list[str]:
        argv = ["codex", "exec", "-", "-m", self.model, "-s", "read-only", "--json",
                "--ephemeral", "--ignore-user-config", "--ignore-rules", "--skip-git-repo-check",
                "--output-schema", str(schema_path)]  # fmt: skip
        overrides = list(CONFIG_OVERRIDES)
        if not self.thinking:
            overrides.append(f'model_reasoning_effort="{LOW_EFFORT}"')
        for override in overrides:
            argv += ["-c", override]
        for feature in DISABLED_FEATURES:
            argv += ["--disable", feature]
        return argv

    async def judge(self, req: JudgeRequest) -> Verdict:
        argv = self.command(self._schema_file(answer_schema(req.questions)))
        try:
            async with self._slots:
                started = time.perf_counter()
                async with asyncio.timeout(self.timeout_s):
                    returncode, stdout, stderr = await self._run(argv, build_prompt(req), self._cwd)
                latency_ms = (time.perf_counter() - started) * 1000
        except TimeoutError as exc:
            raise JudgeError("timeout", f"no verdict within {self.timeout_s:.0f}s") from exc
        except Exception as exc:
            raise JudgeError("other", str(exc)) from exc

        run = _parse_events(stdout)
        if returncode != 0 or run.error:
            detail = run.error or stderr.strip()[-500:] or f"codex exited with {returncode}"
            raise JudgeError(_error_kind(detail), detail)
        if not run.messages:
            raise JudgeError("other", "no answer in the codex output")
        try:
            output = json.loads(run.messages[-1])
        except ValueError as exc:
            raise JudgeError("other", f"the answer is not JSON: {run.messages[-1][:200]}") from exc
        if not isinstance(output, dict):
            raise JudgeError("other", f"the answer is not an object: {run.messages[-1][:200]}")

        return Verdict(
            answers=schema_answers(req.questions, output),
            latency_ms=latency_ms,
            # cached tokens are already part of input_tokens
            input_tokens=run.usage.get("input_tokens"),
            judge=self.model,
            raw={"output": output, "usage": run.usage, "agent_messages": len(run.messages)},
        )

    async def aclose(self) -> None:
        shutil.rmtree(self._home, ignore_errors=True)

    def _schema_file(self, schema: dict) -> Path:
        text = json.dumps(schema, sort_keys=True)
        path = Path(self._home) / f"schema-{hashlib.sha256(text.encode()).hexdigest()[:16]}.json"
        if not path.exists():
            path.write_text(text)
        return path


class _Run:
    def __init__(self) -> None:
        self.messages: list[str] = []
        self.usage: dict = {}
        self.error: str | None = None


def _parse_events(stdout: str) -> _Run:
    """The JSONL events of `codex exec --json`. Items of type `error` are notices, not failures."""
    run = _Run()
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        item = event.get("item") or {}
        if kind == "item.completed" and item.get("type") == "agent_message":
            run.messages.append(item.get("text") or "")
        elif kind == "turn.completed":
            run.usage = event.get("usage") or {}
        elif kind == "turn.failed":
            run.error = (event.get("error") or {}).get("message") or "turn failed"
        elif kind == "error":
            run.error = event.get("message") or "error"
    return run


def _error_kind(detail: str) -> str:
    text = detail.lower()
    # A 403 with a cf-ray id is the edge in front of the ChatGPT backend refusing the socket
    # under load, not a bad login.
    if "usage limit" in text or "rate limit" in text or "429" in text or "cf-ray" in text:
        return "rate_limited"
    if "not logged in" in text or "401" in text or "403" in text or "unauthorized" in text:
        return "auth"
    if "context window" in text or "too long" in text or "context_length" in text:
        return "over_limit"
    return "other"
