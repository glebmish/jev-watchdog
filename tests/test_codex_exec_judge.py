import asyncio
import json
from pathlib import Path

import pytest

from jev_watchdog.judge.base import JudgeError, JudgeRequest, answer_schema, request_payload
from jev_watchdog.judge.codex_exec import DISABLED_FEATURES, CodexExecJudge
from jev_watchdog.judge.registry import JudgeConfig, make_judge
from jev_watchdog.pack import Question
from jev_watchdog.transcript import SurfaceKey

QUESTIONS = [
    Question("exfil", "noul", "sends data out"),
    Question("drift", "score", "distance", criteria=["on task", "side work", "off task"]),
    Question(
        "activity", "choice", "doing what", criteria={"exploring": "reading", "stuck": "looping"}
    ),
]
LINES = ['{"type":"user","message":"fix it"}', '{"type":"assistant","message":"ok"}']
OUTPUT = {"exfil": 0.95, "drift": 1, "activity": "stuck"}
USAGE = {"input_tokens": 3000, "cached_input_tokens": 500, "output_tokens": 80}


def request() -> JudgeRequest:
    return JudgeRequest(SurfaceKey("s", "main"), {"hook_event_name": "Stop"}, LINES, QUESTIONS)


def events(*items: dict) -> str:
    return "\n".join(json.dumps(item) for item in items) + "\n"


def message(text: str) -> dict:
    return {"type": "item.completed", "item": {"id": "i", "type": "agent_message", "text": text}}


def success(output: dict = OUTPUT) -> str:
    return events(
        {"type": "thread.started", "thread_id": "t"},
        # a non-fatal notice from the CLI, e.g. about the disabled code-mode host
        {"type": "item.completed", "item": {"id": "w", "type": "error", "message": "notice"}},
        {"type": "turn.started"},
        message(json.dumps(output)),
        {"type": "turn.completed", "usage": USAGE},
    )


def fake_run(stdout: str = "", returncode: int = 0, stderr: str = "", delay_s: float = 0.0,
             error: Exception | None = None):  # fmt: skip
    calls = []

    async def run_fn(argv, stdin, cwd):
        schema_path = Path(argv[argv.index("--output-schema") + 1])
        calls.append(
            {
                "argv": argv,
                "stdin": stdin,
                "cwd": cwd,
                "schema": json.loads(schema_path.read_text()),
            }
        )
        if delay_s:
            await asyncio.sleep(delay_s)
        if error:
            raise error
        return returncode, stdout, stderr

    run_fn.calls = calls
    return run_fn


def config_overrides(argv: list[str]) -> list[str]:
    return [argv[i + 1] for i, arg in enumerate(argv) if arg == "-c"]


async def test_runs_a_stripped_one_shot_codex_exec():
    run_fn = fake_run(success())
    judge = CodexExecJudge(model="gpt-5.5", run_fn=run_fn)
    await judge.judge(request())
    call = run_fn.calls[0]
    argv = call["argv"]
    assert judge.name == "codex:gpt-5.5"
    assert argv[:3] == ["codex", "exec", "-"]  # the prompt goes through stdin
    assert argv[argv.index("-m") + 1] == "gpt-5.5"
    assert argv[argv.index("-s") + 1] == "read-only"  # backstop: a hijacked judge cannot write
    for flag in ("--json", "--ephemeral", "--ignore-user-config", "--ignore-rules",
                 "--skip-git-repo-check"):  # fmt: skip
        assert flag in argv
    overrides = config_overrides(argv)
    assert 'instructions=""' in overrides  # same input as Jev: no instructions at all
    assert 'web_search="disabled"' in overrides
    assert 'model_reasoning_effort="low"' in overrides
    disabled = [argv[i + 1] for i, arg in enumerate(argv) if arg == "--disable"]
    assert disabled == list(DISABLED_FEATURES)
    assert {"shell_tool", "unified_exec", "code_mode_host", "multi_agent", "plugins"} <= set(
        disabled
    )
    assert call["schema"] == answer_schema(QUESTIONS)
    assert json.loads(call["stdin"]) == request_payload(request())
    assert call["cwd"] and not list(Path(call["cwd"]).glob("AGENTS.md"))
    await judge.aclose()
    assert not Path(call["cwd"]).exists()


async def test_thinking_leaves_the_models_default_effort():
    run_fn = fake_run(success())
    judge = CodexExecJudge(thinking=True, run_fn=run_fn)
    await judge.judge(request())
    assert not any("model_reasoning_effort" in o for o in config_overrides(run_fn.calls[0]["argv"]))
    await judge.aclose()


async def test_maps_the_final_message_to_a_verdict():
    judge = CodexExecJudge(model="gpt-5.5", run_fn=fake_run(success()))
    verdict = await judge.judge(request())
    assert {qid: answer.value for qid, answer in verdict.answers.items()} == {
        "exfil": 0.95,
        "drift": 1.0,
        "activity": "stuck",
    }
    assert verdict.judge == "gpt-5.5"
    assert verdict.input_tokens == 3000  # cached tokens are part of input_tokens
    assert verdict.cost_usd is None  # a ChatGPT login reports no cost
    assert verdict.latency_ms >= 0
    assert verdict.raw["usage"] == USAGE and verdict.raw["output"] == OUTPUT
    await judge.aclose()


async def test_the_last_agent_message_is_the_answer():
    stdout = events(
        message("Let me look at the files first."),
        message(json.dumps(OUTPUT)),
        {"type": "turn.completed", "usage": USAGE},
    )
    verdict = await CodexExecJudge(run_fn=fake_run(stdout)).judge(request())
    assert verdict.answers["exfil"].value == 0.95
    assert verdict.raw["agent_messages"] == 2


async def test_out_of_range_and_unknown_answers_are_clamped_or_dropped():
    output = {"exfil": 1.7, "drift": -3, "activity": "napping"}
    verdict = await CodexExecJudge(run_fn=fake_run(success(output))).judge(request())
    assert verdict.answers["exfil"].value == 1.0 and verdict.answers["drift"].value == 0.0
    assert "activity" not in verdict.answers


EDGE_403 = (
    "Reconnecting... 5/5 (unexpected status 403 Forbidden: Unknown error, url: "
    "wss://chatgpt.com/backend-api/codex/responses, cf-ray: 0000000000000000-XXX)"
)
FAILED = {"type": "turn.failed", "error": {"message": "boom"}}


@pytest.mark.parametrize(
    "run_fn, kind",
    [
        (fake_run(""), "other"),  # no events at all
        (
            fake_run(events(message("not json"), {"type": "turn.completed", "usage": USAGE})),
            "other",
        ),
        (fake_run(events({"type": "turn.completed", "usage": USAGE})), "other"),  # no message
        (fake_run(events(FAILED), returncode=1), "other"),
        (
            fake_run(events({"type": "error", "message": "You've hit your usage limit."}), 1),
            "rate_limited",
        ),
        (
            fake_run(events({"type": "error", "message": "429 Too Many Requests"}), 1),
            "rate_limited",
        ),
        (
            # seen live under load: the edge in front of the ChatGPT backend refuses the socket
            fake_run(events({"type": "error", "message": EDGE_403}), 1),
            "rate_limited",
        ),
        (fake_run("", 1, "Not logged in. Run codex login"), "auth"),
        (fake_run(events({"type": "error", "message": "401 Unauthorized"}), 1), "auth"),
        (
            fake_run(events({"type": "error", "message": "input exceeds the context window"}), 1),
            "over_limit",
        ),
        (fake_run(error=FileNotFoundError("codex")), "other"),
    ],
)
async def test_failures_become_judge_errors(run_fn, kind):
    judge = CodexExecJudge(run_fn=run_fn)
    with pytest.raises(JudgeError) as err:
        await judge.judge(request())
    assert err.value.kind == kind
    await judge.aclose()


async def test_slow_runs_time_out():
    judge = CodexExecJudge(timeout_s=0.01, run_fn=fake_run(success(), delay_s=0.2))
    with pytest.raises(JudgeError) as err:
        await judge.judge(request())
    assert err.value.kind == "timeout"
    await judge.aclose()


async def test_concurrent_judgments_are_capped():
    active = peak = 0

    async def run_fn(argv, stdin, cwd):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.02)
        active -= 1
        return 0, success(), ""

    judge = CodexExecJudge(max_concurrency=2, run_fn=run_fn)
    verdicts = await asyncio.gather(*(judge.judge(request()) for _ in range(6)))
    assert len(verdicts) == 6 and peak == 2
    assert all(verdict.latency_ms < 60 for verdict in verdicts)
    await judge.aclose()


def test_registry_builds_codex_judges():
    assert make_judge("codex", JudgeConfig()).name.startswith("codex:")
    judge = make_judge("codex:gpt-6-astra", JudgeConfig(thinking=True))
    assert judge.name == "codex:gpt-6-astra" and judge.thinking is True
