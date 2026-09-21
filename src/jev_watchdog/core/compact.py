"""What a judge is sent of a long thread: its own verdicts decide what it has to see again.

Every judgment re-sends the thread, and a thread grows with every action. The recent actions
are sent whole. Of the older ones, those the judge found benign are replaced by a count; an
action it flagged, and any error or denial, stays whole; an action in between, or with no
verdict, keeps its command and loses its output.

The state has a budget. Over it, the oldest lines are dropped until it fits, whatever they
are, except what the human said and the action being judged.
Pure logic: lines and standings in, lines out.
"""

import json
from collections import Counter
from collections.abc import Mapping

from jev_watchdog.core.pack import Question
from jev_watchdog.judge.base import Verdict

RECENT_ACTIONS = 20  # sent whole, so that a series of harmless-looking steps is seen together
BENIGN_CUTOFF = 0.15  # a "higher is worse" probability up to here is no suspicion at all
# Jev takes 32k tokens of state and questions. Transcripts measured 2.4 (ids, paths, code) to
# 3.6 (prose) bytes a token; at 2.1 this is still under the limit.
STATE_BUDGET_BYTES = 64_000
OMITTED = "[omitted]"
BENIGN, FLAGGED, UNSURE = "benign", "flagged", "unsure"


def standing(questions: list[Question], verdict: Verdict) -> str:
    """How the judge left an action: flagged by a question, benign by all, or unsure."""
    answers = [(question, verdict.answers.get(question.id)) for question in questions]
    if any(answer is not None and question.flags(answer.value) for question, answer in answers):
        return FLAGGED
    for question, answer in answers:
        higher_is_worse = question.kind == "noul" and question.flag_threshold is not None
        if answer is None or (higher_is_worse and answer.value > BENIGN_CUTOFF):
            return UNSURE
    return BENIGN


def compacted(
    lines: list[str],
    standings: Mapping[str, str],
    recent: int = RECENT_ACTIONS,
    budget: int = STATE_BUDGET_BYTES,
) -> list[str]:
    """The lines to send, given the standing of the tool calls judged so far, by id."""
    entries = [json.loads(line) for line in lines]
    calls = [index for index, entry in enumerate(entries) if _blocks(entry, "tool_use")]
    judged_from = calls[-1] if calls else 0  # the action being judged, and what follows it
    sent = _by_standing(lines, entries, calls, standings, recent)
    excess = sum(len(line) for _, line in sent) - budget
    if excess <= 0:
        result = [line for _, line in sent]
        return lines if result == lines else result
    kept, dropped = [], 0
    for index, line in sent:
        if excess > 0 and index < judged_from and not _said_by_human(entries[index]):
            excess -= len(line)
            dropped += 1
        else:
            kept.append(line)
    return [_dumps({"type": "omitted", "lines": dropped}), *kept] if dropped else kept


def _by_standing(
    lines: list[str],
    entries: list[dict],
    calls: list[int],
    standings: Mapping[str, str],
    recent: int,
) -> list[tuple[int, str]]:
    """Each line to send with the index of the line it came from (a count: of its first)."""
    if len(calls) <= recent:
        return list(enumerate(lines))
    cut = calls[-recent] if recent else len(lines)
    failed = {
        block.get("tool_use_id")
        for entry in entries
        for block in _blocks(entry, "tool_result")
        if block.get("is_error")
    }

    def gone(entry: dict) -> bool:
        ids = [block.get("id") for block in _blocks(entry, "tool_use")]
        return all(standings.get(i) == BENIGN and i not in failed for i in ids)

    sent: list[tuple[int, str]] = []
    dropped: Counter[str] = Counter()
    first_dropped = 0

    def count() -> None:
        if dropped:
            marker = {"type": "omitted", "actions": dropped.total(), "judged": BENIGN}
            sent.append((first_dropped, _dumps({**marker, "tools": dict(dropped)})))
            dropped.clear()

    def send(index: int, line: str) -> None:
        count()
        sent.append((index, line))

    for index in range(cut):
        line, entry = lines[index], entries[index]
        if used := _blocks(entry, "tool_use"):
            if gone(entry):
                first_dropped = first_dropped if dropped else index
                dropped.update(block.get("name", "?") for block in used)
            else:
                send(index, line)
        elif _blocks(entry, "tool_result"):
            if (kept := _results(line, entry, standings, failed)) is not None:
                send(index, kept)
        elif entry.get("type") == "assistant":
            # The agent's words go with the action they lead to; the last of a turn stay.
            following = next((e for e in entries[index + 1 : cut] if not _is_words(e)), None)
            if following is None or not (_blocks(following, "tool_use") and gone(following)):
                send(index, line)
        else:
            send(index, line)
    for index in range(cut, len(lines)):
        send(index, lines[index])
    count()
    return sent


def _results(line: str, entry: dict, standings: Mapping[str, str], failed: set) -> str | None:
    """A tool result line, each output whole, omitted or gone; None if nothing is left."""
    blocks = []
    for block in entry["message"]["content"]:
        if not (isinstance(block, dict) and block.get("type") == "tool_result"):
            blocks.append(block)
            continue
        tool_use_id = block.get("tool_use_id")
        left = standings.get(tool_use_id)
        if left == FLAGGED or tool_use_id in failed:
            blocks.append(block)
        elif left != BENIGN:
            blocks.append({**block, "content": OMITTED})
    if blocks == entry["message"]["content"]:
        return line
    if not blocks:
        return None
    return _dumps({**entry, "message": {**entry["message"], "content": blocks}})


def _dumps(entry: dict) -> str:
    return json.dumps(entry, ensure_ascii=False, separators=(",", ":"))


def _blocks(entry: dict, kind: str) -> list[dict]:
    message = entry.get("message") if isinstance(entry, dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return []
    return [block for block in content if isinstance(block, dict) and block.get("type") == kind]


def _is_words(entry: dict) -> bool:
    """An assistant line that only says or thinks something."""
    return (
        isinstance(entry, dict)
        and entry.get("type") == "assistant"
        and not _blocks(entry, "tool_use")
    )


def _said_by_human(entry: dict) -> bool:
    """A user line that is neither a tool result nor the harness speaking in the user's place,
    e.g. a subagent's report (transcript.trimmed_lines keeps the origin of such a line)."""
    return (
        isinstance(entry, dict)
        and entry.get("type") == "user"
        and "origin" not in entry
        and not _blocks(entry, "tool_result")
    )
