"""What the dashboard draws, as pure functions: a dict from the watchdog in, `Text` or a plot
out. No state, no I/O, no Textual app: tui.py owns those.

Labels, details, reasons, contexts and error text are the watched agent's to choose. They
reach the screen only through `printable`, as `Text`, never as markup.
"""

import math
from datetime import datetime

from rich.text import Text
from textual_plotext import PlotextPlot

from jev_watchdog.printer import printable

ENDED = frozenset({"SessionEnd", "SubagentStop"})
THREAD_COLUMNS = ("thread", "status", "judged", "seen")
DETAIL_COLUMNS = ("question", "n", "last", "mean", "ewma", "min", "max", "streak", "longest",
                  "evidence / counts")  # fmt: skip
TIMELINE_LABEL = 24
BLOCKS = "▁▂▃▄▅▆▇█"


def header_text(state: dict | None, live: bool) -> Text:
    text = Text("jev-watchdog", style="bold")
    if state is None:
        return text.append(" · connecting…", style="dim")
    mode, totals = state["mode"], state["totals"]
    rules = ",".join(mode["rules"])
    if not rules:
        text.append(" · no quarantine rules in the pack")
    elif mode["enforce"]:
        text.append(f" · ENFORCING on {rules} ({mode['decider']})", style="bold red")
    else:
        text.append(f" · dry run on {rules}")
    text.append(f" · judges {','.join(judge['name'] for judge in state['judges'])}")
    text.append(f" · up {uptime(state['started_at'], state['now'])}")
    text.append(f" · {totals['judgments']} judgments · ${totals['cost_usd']:.2f}")
    if totals["quarantines"] or totals["rejected"]:
        text.append(f" · quarantines {totals['quarantines']} · rejected {totals['rejected']}")
    text.append("   ")
    text.append("● live" if live else "○ reconnecting…", style="green" if live else "yellow")
    text.plain = printable(text.plain)
    return text


def thread_status(thread: dict, decider: str) -> Text:
    """The quarantine if there is one, else the deciding judge's rule closest to its limit."""
    blocking = thread["quarantine"]
    if blocking is not None:
        with_main = "" if blocking["target"] == thread["label"] else f" with {blocking['target']}"
        return Text(printable(f"QUARANTINED{with_main}"), style="bold white on red")
    view = thread["judges"].get(decider) or {"evidence": {}, "tripped": False}
    rules = [(e["value"] / e["limit"], rule, e) for rule, e in view["evidence"].items()]
    if not rules:
        return Text("")
    share, rule, evidence = max(rules)
    said = f"{rule} {evidence['value']:.2f}/{evidence['limit']:.2f}"
    if view["tripped"]:  # a dry run, or a judge that does not decide
        return Text(f"TRIPPED {said}", style="bold red")
    return Text(said, style="yellow" if share >= 0.5 else "")


def uptime(started_at: str, now: str) -> str:
    seconds = int(
        (datetime.fromisoformat(now) - datetime.fromisoformat(started_at)).total_seconds()
    )
    seconds = max(seconds, 0)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h{seconds % 3600 // 60:02d}"
    return f"{seconds // 86400}d{seconds % 86400 // 3600:02d}h"


def evidence_lines(history: dict | None, judge: str | None) -> dict:
    """One judge's evidence per judged tool call, each rule as a share of its limit.

    Only tool events move the evidence, so they are the x axis. A quarantine, release or trip
    is put at the first tool call that came after it.
    """
    points = [] if history is None else history["judges"].get(judge, [])
    points = [point for point in points if point["folded"]]
    rules = points[0]["shares"] if points else ()
    lines = {rule: [point["shares"][rule] for point in points] for rule in rules}
    marks = []
    for mark in [] if history is None else history["marks"]:
        if mark["judge"] in (None, judge):
            after = [n for n, point in enumerate(points, 1) if point["ts"] >= mark["ts"]]
            marks.append((after[0] if after else len(points), mark["kind"]))
    return {"x": list(range(1, len(points) + 1)), "lines": lines, "marks": marks}


def draw_evidence(plot: PlotextPlot, history: dict | None, judge: str | None) -> None:
    data = evidence_lines(history, judge)
    plt = plot.plt
    plt.clear_figure()
    if not data["x"]:
        plt.title(printable(f"{judge or 'no judge'}: no judged tool calls yet"))
    else:
        for rule, shares in data["lines"].items():
            plt.plot(data["x"], shares, label=printable(rule), marker="braille")
        plt.hline(1.0, "red")
        for x, kind in data["marks"]:
            if x:
                plt.vline(x, {"quarantine": "red", "trip": "orange"}.get(kind, "green"))
        top = max([1.2, *(max(shares) * 1.1 for shares in data["lines"].values() if shares)])
        plt.ylim(0, top)
        plt.yticks([tick / 2 for tick in range(int(top * 2) + 1)])
        plt.xticks(_whole_ticks(len(data["x"])))
        plt.xlabel("judged tool calls")
        plt.title(printable(f"{judge} · limit at 1.0 · red: quarantined, green: released"))
    plot.refresh()


def draw_judges(plot: PlotextPlot, judges: list[dict]) -> None:
    plt = plot.plt
    plt.clear_figure()
    for judge in judges:
        latency, lag = judge.get("recent_latency_ms", []), judge.get("recent_lag_ms", [])
        if latency:
            x = list(range(1, len(latency) + 1))
            plt.plot(x, latency, label=printable(f"{judge['name']} latency"), marker="braille")
            plt.plot(x, lag, label=printable(f"{judge['name']} lag"), marker="braille")
    longest = max((len(judge.get("recent_latency_ms", [])) for judge in judges), default=0)
    if longest:
        plt.xticks(_whole_ticks(longest))
    plt.xlabel("judgments, oldest first")
    plt.ylabel("ms")
    plt.title("lag above latency is time spent queued: the judge is not keeping up")
    plot.refresh()


def timeline_text(timeline: dict, selected: str | None = None) -> Text:
    """Every thread's window as a row of cells: how near its worst rule came to the limit."""
    text = Text()
    text.append(f"{timeline['judge']} · one cell = {span(timeline['bucket_s'] / 60)} · ", "dim")
    for share, word in ((0.2, "calm "), (0.7, "over half "), (1.0, "at the limit ")):
        text.append(_cell(share, None))
        text.append(f" {word}", style="dim")
    text.append("· Q quarantined  R released  T tripped\n\n", style="dim")
    if not timeline["threads"]:
        text.append("nothing was judged in this window", style="dim")
        return text
    for row in timeline["threads"]:
        label = printable(row["label"])[: TIMELINE_LABEL - 1]
        style = "bold cyan" if row["label"] == selected else "cyan"
        text.append(f"{label:<{TIMELINE_LABEL}}", style=style)
        for index, share in enumerate(row["cells"]):
            text.append(_cell(share, row["marks"].get(str(index))))
        text.append("\n")
    cells = len(timeline["threads"][0]["cells"])
    start, end = timeline["start"][11:16], timeline["end"][11:16]
    text.append(" " * TIMELINE_LABEL + start + " " * max(cells - 10, 1) + end, style="dim")
    return text


def _cell(share: float | None, mark: str | None) -> Text:
    if mark == "quarantine":
        return Text("Q", style="bold white on red")
    if mark == "release":
        return Text("R", style="bold green")
    if mark == "trip":
        return Text("T", style="bold red")
    if share is None:
        return Text("·", style="dim")
    block = BLOCKS[min(max(math.ceil(share * len(BLOCKS)), 1), len(BLOCKS)) - 1]
    return Text(block, style="red" if share >= 1 else "yellow" if share >= 0.5 else "green")


def _whole_ticks(count: int, wanted: int = 8) -> list[int]:
    step = max(math.ceil(count / wanted), 1)
    return list(range(1, count + 1, step))


def span(minutes: float) -> str:
    if minutes < 1:
        return f"{minutes * 60:.0f}s"
    return f"{minutes:.0f}m" if minutes < 60 else f"{minutes / 60:.0f}h"


def row_key(thread: dict) -> str:
    return f"{thread['session_id']}/{thread['agent_id']}"


def thread_cells(thread: dict, decider: str, now: str) -> tuple[Text, Text, Text, Text]:
    over = thread["last_event"] in ENDED
    seen = "" if thread["last_seen"] is None else uptime(thread["last_seen"], now)
    return (
        Text(printable(thread["label"]), style="dim" if over else "cyan"),
        thread_status(thread, decider),
        Text(str(thread["judgments"]), justify="right"),
        Text("ended" if over else seen, style="dim", justify="right"),
    )


def question_cells(qid: str, stat: dict, evidence: dict | None) -> tuple[Text, ...]:
    against = "" if evidence is None else f"{evidence['value']:.2f} / {evidence['limit']:.2f}"
    if stat["kind"] == "choice":
        counts = " ".join(f"{choice}×{n}" for choice, n in stat["counts"].items())
        cells = (qid, str(sum(stat["counts"].values())), str(stat["last"]), "", "", "", "",
                 str(stat["streak"]), str(stat["longest_streak"]), counts)  # fmt: skip
    else:
        cells = (qid, str(stat["n"]), _num(stat["last"]), _num(stat["mean"]), _num(stat["ewma"]),
                 _num(stat["min"]), _num(stat["max"]), str(stat["streak"]), str(stat["longest_streak"]),
                 against)  # fmt: skip
    flagged = "bold red" if stat["streak"] else ""
    return tuple(
        Text(printable(cell), style=flagged if n == 0 else "") for n, cell in enumerate(cells)
    )


def about(thread: dict, view: dict) -> Text:
    text = Text()
    blocking = thread["quarantine"]
    if blocking is not None:
        by = printable(f"quarantined by {blocking['source']} at {blocking['at'][11:19]}: ")
        text.append(by, style="bold red")
        text.append(printable(blocking["reason"]) + "\n")
    text.append("context: ", style="dim")
    text.append(printable(thread["context"] or "-") + "\n")
    errors = " ".join(f"{kind}×{n}" for kind, n in view["errors"].items()) or "0"
    about = f"cwd {thread['cwd'] or '-'} · events {thread['events']} · errors {errors}"
    text.append(printable(about), style="dim")
    return text


def _num(value: float | None) -> str:
    return "-" if value is None else f"{value:.2f}"
