import asyncio
import json

import pytest
from claude_agent_sdk import ResultMessage

from jev_watchdog.core.pack import Question
from jev_watchdog.core.transcript import SurfaceKey
from jev_watchdog.judge.base import JudgeError, JudgeRequest, request_payload
from jev_watchdog.judge.claude_agent import ClaudeAgentJudge, answer_schema, build_prompt

QUESTIONS = [
    Question("exfil", "noul", "sends data out"),
    Question("drift", "score", "distance", criteria=["on task", "side work", "off task"]),
    Question(
        "activity", "choice", "doing what", criteria={"exploring": "reading", "stuck": "looping"}
    ),
]
LINES = ['{"type":"user","message":"fix it"}', '{"type":"assistant","message":"ok"}']


def request() -> JudgeRequest:
    return JudgeRequest(SurfaceKey("s", "main"), {"hook_event_name": "Stop"}, LINES, QUESTIONS)


def result(**overrides) -> ResultMessage:
    fields = {
        "subtype": "success",
        "duration_ms": 5000,
        "duration_api_ms": 4500,
        "is_error": False,
        "num_turns": 2,
        "session_id": "sid",
        "total_cost_usd": 0.0087,
        "usage": {"input_tokens": 3000, "cache_read_input_tokens": 500, "output_tokens": 80},
        "structured_output": {"exfil": 0.95, "drift": 1, "activity": "stuck"},
    }
    return ResultMessage(**(fields | overrides))


def fake_query(*messages, error: Exception | None = None, delay_s: float = 0.0):
    calls = []

    async def query_fn(*, prompt, options):
        calls.append({"prompt": prompt, "options": options})
        if delay_s:
            await asyncio.sleep(delay_s)
        if error:
            raise error
        for message in messages:
            yield message

    query_fn.calls = calls
    return query_fn


def test_answer_schema_covers_every_question_kind():
    schema = answer_schema(QUESTIONS)
    assert schema["required"] == ["exfil", "drift", "activity"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["exfil"] == {"type": "number", "minimum": 0, "maximum": 1}
    assert schema["properties"]["drift"] == {"type": "number", "minimum": 0, "maximum": 2}
    assert schema["properties"]["activity"] == {"type": "string", "enum": ["exploring", "stuck"]}


def test_request_payload_is_jevs_request_body():
    assert request_payload(request()) == {
        "state": LINES,
        "questions": {
            "exfil": {"type": "noul", "instructions": "sends data out"},
            "drift": {
                "type": "score",
                "instructions": "distance",
                "criteria": ["on task", "side work", "off task"],
            },
            "activity": {
                "type": "choice",
                "instructions": "doing what",
                "criteria": {"exploring": "reading", "stuck": "looping"},
            },
        },
    }


def test_prompt_is_exactly_the_request_payload():
    assert json.loads(build_prompt(request())) == request_payload(request())


async def test_runs_a_toolless_settingless_one_shot_query():
    query_fn = fake_query(result())
    judge = ClaudeAgentJudge(model="claude-haiku-4-5", query_fn=query_fn)
    await judge.judge(request())
    options = query_fn.calls[0]["options"]
    assert judge.name == "claude:claude-haiku-4-5"
    assert options.model == "claude-haiku-4-5"
    assert options.tools == [] and options.setting_sources == []
    assert options.max_turns == 1  # one model step: nothing can follow the answer
    assert options.thinking == {"type": "disabled"}
    assert options.extra_args == {"no-session-persistence": None, "strict-mcp-config": None}
    assert options.output_format == {"type": "json_schema", "schema": answer_schema(QUESTIONS)}
    assert options.system_prompt == ""  # same input as Jev: nothing but the request body
    assert json.loads(query_fn.calls[0]["prompt"]) == request_payload(request())
    await judge.aclose()


async def test_thinking_can_be_left_on():
    query_fn = fake_query(result())
    judge = ClaudeAgentJudge(thinking=True, query_fn=query_fn)
    await judge.judge(request())
    assert query_fn.calls[0]["options"].thinking is None
    await judge.aclose()


async def test_maps_structured_output_to_a_verdict():
    judge = ClaudeAgentJudge(model="claude-haiku-4-5", query_fn=fake_query(result()))
    verdict = await judge.judge(request())
    assert {qid: answer.value for qid, answer in verdict.answers.items()} == {
        "exfil": 0.95,
        "drift": 1.0,
        "activity": "stuck",
    }
    assert verdict.judge == "claude-haiku-4-5"
    assert verdict.input_tokens == 3500 and verdict.cost_usd == 0.0087
    assert verdict.latency_ms >= 0
    assert verdict.raw["duration_api_ms"] == 4500 and verdict.raw["num_turns"] == 2
    await judge.aclose()


async def test_out_of_range_and_unknown_answers_are_clamped_or_dropped():
    output = {"exfil": 1.7, "drift": -3, "activity": "napping"}
    judge = ClaudeAgentJudge(query_fn=fake_query(result(structured_output=output)))
    verdict = await judge.judge(request())
    assert verdict.answers["exfil"].value == 1.0 and verdict.answers["drift"].value == 0.0
    assert "activity" not in verdict.answers
    await judge.aclose()


@pytest.mark.parametrize(
    "query_fn, kind",
    [
        (fake_query(), "other"),  # no ResultMessage at all
        (fake_query(result(subtype="error_max_structured_output_retries", is_error=True)), "other"),
        (fake_query(result(structured_output=None)), "other"),
        (
            fake_query(result(is_error=True, api_error_status=429, result="rate limited")),
            "rate_limited",
        ),
        (fake_query(result(is_error=True, api_error_status=401, result="bad creds")), "auth"),
        (
            fake_query(result(is_error=True, api_error_status=400, result="Prompt is too long")),
            "over_limit",
        ),
        (fake_query(error=RuntimeError("claude CLI not found")), "other"),
    ],
)
async def test_failures_become_judge_errors(query_fn, kind):
    judge = ClaudeAgentJudge(query_fn=query_fn)
    with pytest.raises(JudgeError) as err:
        await judge.judge(request())
    assert err.value.kind == kind
    await judge.aclose()


async def test_slow_queries_time_out():
    judge = ClaudeAgentJudge(timeout_s=0.01, query_fn=fake_query(result(), delay_s=0.2))
    with pytest.raises(JudgeError) as err:
        await judge.judge(request())
    assert err.value.kind == "timeout"
    await judge.aclose()


async def test_concurrent_judgments_are_capped():
    active = peak = 0

    async def query_fn(*, prompt, options):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.1)
        active -= 1
        yield result()

    judge = ClaudeAgentJudge(max_concurrency=2, query_fn=query_fn)
    verdicts = await asyncio.gather(*(judge.judge(request()) for _ in range(6)))
    assert len(verdicts) == 6 and peak == 2
    # Latency is the query itself, not the time spent waiting for a slot.
    # Latency excludes the time queued behind the cap: counted in, the second round would show
    # 200 ms or more. The 100 ms of slack is for a loaded machine.
    assert all(verdict.latency_ms < 200 for verdict in verdicts)
    await judge.aclose()


def test_request_payload_carries_the_context_like_jevs_state():
    req = JudgeRequest(request().surface, request().event, request().transcript_lines,
                       request().questions, "prod is fine")  # fmt: skip
    payload = request_payload(req)
    assert payload["state"] == {
        "user_context": "prod is fine",
        "transcript": request().transcript_lines,
    }
    assert json.loads(build_prompt(req)) == payload
