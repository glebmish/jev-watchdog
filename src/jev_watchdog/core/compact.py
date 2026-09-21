"""What a judge is sent of a long thread: its own verdicts decide what it has to see again.

Every judgment re-sends the thread, and a thread grows with every action. Of the actions
older than the recent ones, those the judge found benign are replaced by a count; an action
it flagged, and any error or denial, stays whole; an action in between, or with no verdict,
keeps its command and loses its output. What the user said always stays. When the recent
actions are too large to be sent whole, fewer of them count as recent.
Pure logic: lines and verdicts in, lines out.
"""

import json
from collections import Counter
from collections.abc import Mapping

from jev_watchdog.core.pack import Question
from jev_watchdog.judge.base import Verdict

RECENT_ACTIONS = 20  # sent whole, so that a series of harmless-looking steps is seen together
BENIGN_CUTOFF = 0.15  # a "higher is worse" probability up to here is no suspicion at all
# Jev takes 32k tokens of state, at 2.4 (uuids, paths) to 3.6 (prose) bytes a token.
STATE_BUDGET_BYTES = 80_000
OUTPUT_OMITTED = "[output omitted]"


def standing(questions: list[Question], verdict: Verdict) -> bool | None:
    """How the judge left an action: True benign, False flagged, None neither."""
    answers = [(question, verdict.answers.get(question.id)) for question in questions]
    if any(answer is not None and question.flags(answer.value) for question, answer in answers):
        return False
    for question, answer in answers:
        higher_is_worse = question.kind == "noul" and question.flag_threshold is not None
        if answer is None or (higher_is_worse and answer.value > BENIGN_CUTOFF):
            return None
    return True


def compacted(
    lines: list[str],
    benign: Mapping[str, bool],
    recent: int = RECENT_ACTIONS,
    budget: int = STATE_BUDGET_BYTES,
) -> list[str]:
    """The lines to send, given the standing of the tool calls judged so far, by id.

    The last `recent` actions are sent whole if that fits the budget, else the last half of
    them, and so on down to the one being judged, which is never touched.
    """
    entries = [json.loads(line) for line in lines]
    while True:
        sent = _compacted(lines, entries, benign, recent)
        if recent <= 1 or sum(len(line) for line in sent) <= budget:
            return sent
        recent //= 2


def _compacted(
    lines: list[str], entries: list[dict], benign: Mapping[str, bool], recent: int
) -> list[str]:
    calls = [index for index, entry in enumerate(entries) if _blocks(entry, "tool_use")]
    if len(calls) <= recent:
        return lines
    cut = calls[-recent] if recent else len(lines)
    failed = {
        block.get("tool_use_id")
        for entry in entries
        for block in _blocks(entry, "tool_result")
        if block.get("is_error")
    }

    def gone(entry: dict) -> bool:
        ids = [block.get("id") for block in _blocks(entry, "tool_use")]
        return all(benign.get(i) is True and i not in failed for i in ids)

    sent: list[str] = []
    dropped: Counter[str] = Counter()

    def send(*kept: str) -> None:
        if dropped:
            marker = {"type": "omitted", "judged": "benign", "actions": dropped.total()}
            sent.append(_dumps({**marker, "tools": dict(dropped)}))
            dropped.clear()
        sent.extend(kept)

    for index, (line, entry) in enumerate(zip(lines[:cut], entries[:cut], strict=True)):
        if _blocks(entry, "tool_use"):
            if gone(entry):
                dropped.update(block.get("name", "?") for block in _blocks(entry, "tool_use"))
            else:
                send(line)
        elif _blocks(entry, "tool_result"):
            if (kept := _results(line, entry, benign, failed)) is not None:
                send(kept)
        elif entry.get("type") == "assistant":
            # The agent's words go with the action they lead to; the last of a turn stay.
            following = next((e for e in entries[index + 1 : cut] if not _is_words(e)), None)
            if following is None or not (_blocks(following, "tool_use") and gone(following)):
                send(line)
        else:
            send(line)
    send(*lines[cut:])
    return sent


def _results(line: str, entry: dict, benign: Mapping[str, bool], failed: set) -> str | None:
    """A tool result line with each output whole, omitted or gone; None if nothing is left."""
    blocks = []
    for block in entry["message"]["content"]:
        if not (isinstance(block, dict) and block.get("type") == "tool_result"):
            blocks.append(block)
            continue
        verdict = benign.get(block.get("tool_use_id"))
        if verdict is False or block.get("tool_use_id") in failed:
            blocks.append(block)
        elif verdict is None:
            blocks.append({**block, "content": OUTPUT_OMITTED})
    if blocks == entry["message"]["content"]:
        return line
    if not blocks:
        return None
    message = {**entry["message"], "content": blocks}
    return _dumps({**entry, "message": message})


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
