"""Claude judge via the Claude Agent SDK. The only module that imports claude_agent_sdk.

Each judgment is a fresh one-shot query through the local Claude Code login: no tools, no
settings (so no hooks or plugins, and no way to re-trigger the watchdog), no session files.

The model gets the same input as Jev: an empty system prompt and the request payload as the
only message. The output schema stands in for Jev's typed answers. The Agent SDK harness
still adds ~3.5k tokens of its own (structured-output tool), which cannot be removed.
"""

import asyncio
import shutil
import tempfile
import time
from collections.abc import AsyncIterator, Callable
from typing import Any

from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query

from jev_watchdog.judge.base import (
    JudgeError,
    JudgeRequest,
    Verdict,
    answer_schema,
    build_prompt,
    schema_answers,
)

DEFAULT_MODEL = "claude-opus-5"
TIMEOUT_S = 120.0
# Exactly one model step. The structured answer is that step's tool call (the SDK reports it
# as a second turn), so the judge can answer but can never take a follow-up action.
MAX_TURNS = 1
# Every judgment starts a `claude` CLI process; replaying a corpus would otherwise start one
# per case at once.
MAX_CONCURRENCY = 4


class ClaudeAgentJudge:
    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        thinking: bool = False,
        timeout_s: float = TIMEOUT_S,
        max_concurrency: int = MAX_CONCURRENCY,
        query_fn: Callable[..., AsyncIterator[Any]] = query,
    ) -> None:
        self.name = f"claude:{model}"
        self.model = model
        self.thinking = thinking
        self.timeout_s = timeout_s
        self._query = query_fn
        self._slots = asyncio.Semaphore(max_concurrency)
        # An empty working directory: nothing for the CLI to discover or load.
        self._cwd = tempfile.mkdtemp(prefix="jev-watchdog-judge-")

    def options(self, req: JudgeRequest) -> ClaudeAgentOptions:
        return ClaudeAgentOptions(
            model=self.model,
            system_prompt="",  # the input is the request payload and nothing else
            tools=[],
            setting_sources=[],
            max_turns=MAX_TURNS,
            cwd=self._cwd,
            output_format={"type": "json_schema", "schema": answer_schema(req.questions)},
            # strict-mcp-config: without it the account's claude.ai connectors (Docs, Gmail,
            # Drive, ...) are offered to the judge, which then tries to act on the transcript.
            extra_args={"no-session-persistence": None, "strict-mcp-config": None},
            thinking=None if self.thinking else {"type": "disabled"},
        )

    async def judge(self, req: JudgeRequest) -> Verdict:
        options = self.options(req)
        try:
            async with self._slots:
                started = time.perf_counter()
                async with asyncio.timeout(self.timeout_s):
                    result = await self._result(build_prompt(req), options)
                latency_ms = (time.perf_counter() - started) * 1000
        except TimeoutError as exc:
            raise JudgeError("timeout", f"no verdict within {self.timeout_s:.0f}s") from exc
        except JudgeError:
            raise
        except Exception as exc:
            raise JudgeError(_error_kind(None, str(exc)), str(exc)) from exc

        if result.is_error or result.subtype != "success":
            detail = result.result or "; ".join(result.errors or []) or result.subtype
            raise JudgeError(_error_kind(result.api_error_status, detail), detail)
        if not isinstance(result.structured_output, dict):
            raise JudgeError("other", "no structured output in the result")

        usage = result.usage or {}
        return Verdict(
            answers=schema_answers(req.questions, result.structured_output),
            latency_ms=latency_ms,
            input_tokens=sum(
                usage.get(key) or 0
                for key in (
                    "input_tokens",
                    "cache_read_input_tokens",
                    "cache_creation_input_tokens",
                )
            ),
            judge=self.model,
            cost_usd=result.total_cost_usd,
            raw={
                "structured_output": result.structured_output,
                "duration_ms": result.duration_ms,
                "duration_api_ms": result.duration_api_ms,
                "num_turns": result.num_turns,
                "usage": usage,
            },
        )

    async def aclose(self) -> None:
        shutil.rmtree(self._cwd, ignore_errors=True)

    async def _result(self, prompt: str, options: ClaudeAgentOptions) -> ResultMessage:
        result = None
        async for message in self._query(prompt=prompt, options=options):
            if isinstance(message, ResultMessage):
                result = message
        if result is None:
            raise JudgeError("other", "the query ended without a result")
        return result


def _error_kind(status: int | None, detail: str) -> str:
    if status == 429:
        return "rate_limited"
    if status in (401, 403):
        return "auth"
    if "too long" in detail.lower():
        return "over_limit"
    return "other"
