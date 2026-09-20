"""The dashboard of `attach` and `run --tui`: threads, one thread's statistics, the feed.

It draws what the watchdog says: threads and statistics come from /state, asked again as
records arrive, and the feed is the watchdog's own display records, drawn with the console's
`render_line`. Nothing is computed here.

Labels, details, reasons, contexts and error text are the watched agent's to choose. They
reach the screen only through `printable`, as `Text` or with markup off, never as markup.
"""

import asyncio
import time
from collections import deque
from contextlib import aclosing
from datetime import datetime
from typing import ClassVar, Protocol

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.markup import escape
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Input, Label, RichLog, Static

from jev_watchdog.attach import AttachError, ControlError
from jev_watchdog.printer import printable, render_line

FEED_LINES = 5000
TICK_S = 0.25  # /state is asked at most this often while records arrive
IDLE_REFRESH_S = 2.0  # and this often when nothing does: uptime, a thread going quiet
RETRY_S = 1.0
ENDED = frozenset({"SessionEnd", "SubagentStop"})
THREAD_COLUMNS = ("thread", "status", "judged", "seen")
DETAIL_COLUMNS = ("question", "n", "last", "mean", "ewma", "min", "max", "streak", "longest",
                  "evidence")  # fmt: skip


class Client(Protocol):
    """What the dashboard needs of a watchdog; `attach.AttachClient` over the socket."""

    async def state(self) -> dict: ...
    def events(self, since: int = 0): ...
    async def quarantine(self, target: str, reason: str) -> dict: ...
    async def release(self, target: str) -> dict: ...
    async def context(self, target: str, text: str) -> dict: ...


class Ask(ModalScreen[str | None]):
    """One line of text. Enter answers, also with nothing; Escape does not."""

    BINDINGS: ClassVar = [Binding("escape", "cancel", "cancel")]
    DEFAULT_CSS = """
    Ask { align: center middle; }
    Ask > Vertical { width: 80; height: auto; border: round $accent; padding: 0 1;
                     background: $surface; }
    """

    def __init__(self, question: str, placeholder: str) -> None:
        super().__init__()
        self.question, self.placeholder = question, placeholder

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(Text(printable(self.question)))
            yield Input(placeholder=self.placeholder)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value)

    def action_cancel(self) -> None:
        self.dismiss(None)


class WatchdogApp(App[None]):
    TITLE = "jev-watchdog"
    CSS = """
    #header { height: 1; padding: 0 1; background: $panel; }
    #top { height: 40%; min-height: 8; }
    #threads { width: 45%; height: 100%; border: round $primary; }
    #detail { height: 1fr; border: round $primary; }
    #context { height: auto; max-height: 4; padding: 0 2; }
    #feed { height: 1fr; border: round $primary; }
    """
    BINDINGS: ClassVar = [
        Binding("x", "quarantine", "quarantine"),
        Binding("r", "release", "release"),
        Binding("c", "context", "context"),
        Binding("f", "filter", "feed: all/thread"),
        Binding("j", "judge", "next judge"),
        Binding("ctrl+c", "quit", show=False, priority=True),
    ]

    def __init__(
        self,
        client: Client,
        owns_watchdog: bool = False,
        tick_s: float = TICK_S,
        retry_s: float = RETRY_S,
    ) -> None:
        super().__init__()
        self.client = client
        self.tick_s, self.retry_s = tick_s, retry_s
        # `run --tui` is the watchdog: leaving the dashboard stops it. `attach` only looks.
        self.bind("q", "quit", description="stop the watchdog" if owns_watchdog else "detach")
        self._state: dict | None = None
        self._records: deque[dict] = deque(maxlen=FEED_LINES)
        self._seq = 0  # the last record seen: a broken stream resumes after it
        self._boot: str | None = None
        self._live = False
        self._dirty = True
        self._selected: str | None = None  # row key of the selected thread
        self._narrowed = False  # feed: only the selected thread
        self._judge = 0

    def compose(self) -> ComposeResult:
        yield Static(id="header")
        with Horizontal(id="top"):
            yield DataTable(id="threads", cursor_type="row", zebra_stripes=True)
            with Vertical():
                yield DataTable(id="detail", cursor_type="none")
                yield Static(id="context")
        yield RichLog(id="feed", max_lines=FEED_LINES, wrap=False, markup=False, highlight=False)
        yield Footer()

    def on_mount(self) -> None:
        # Kept, not looked up each time: query_one searches the screen on top, and state
        # and records keep arriving while that is the question of `x` or `c`.
        self.header_bar = self.query_one("#header", Static)
        self.threads_table = threads = self.query_one("#threads", DataTable)
        self.detail_table = self.query_one("#detail", DataTable)
        self.about_box = self.query_one("#context", Static)
        self.feed_log = self.query_one("#feed", RichLog)
        for column in THREAD_COLUMNS:
            threads.add_column(column, key=column)
        threads.border_title = "threads"
        self.detail_table.add_columns(*DETAIL_COLUMNS)
        self._show_header()
        self._show_detail()
        self._show_feed()
        threads.focus()
        self.run_worker(self._follow(), name="follow")
        self.run_worker(self._watch_state(), name="state")

    @property
    def selected_label(self) -> str | None:
        thread = self._thread()
        return None if thread is None else thread["label"]

    # --- the two conversations with the watchdog ---------------------------------------------

    async def _follow(self) -> None:
        while True:
            restarted = False
            try:
                async with aclosing(self.client.events(self._seq)) as stream:
                    async for name, data in stream:
                        if name == "hello":
                            restarted = self._boot not in (None, data["boot"])
                            self._boot = data["boot"]
                            if restarted:  # its records are numbered from 1 again
                                self._records.clear()
                                self._seq = 0
                                self._show_feed()
                                break
                            self._set_live(True)
                        elif name == "record":
                            self._seq = data["seq"]
                            self._records.append(data)
                            if self._wanted(data):
                                self.feed_log.write(render_line(data))
                        self._dirty = True
            except AttachError:
                pass
            if not restarted:
                self._set_live(False)
                await asyncio.sleep(self.retry_s)

    async def _watch_state(self) -> None:
        asked = 0.0
        while True:
            await asyncio.sleep(self.tick_s)
            if not self._dirty and time.monotonic() - asked < IDLE_REFRESH_S:
                continue
            self._dirty, asked = False, time.monotonic()
            try:
                state = await self.client.state()
            except AttachError:
                continue  # _follow says so in the header
            self._show_state(state)

    def _set_live(self, live: bool) -> None:
        self._live = live
        self._show_header()

    # --- drawing -----------------------------------------------------------------------------

    def _show_state(self, state: dict) -> None:
        table = self.threads_table
        first = self._state is None
        if not first and state["boot"] != self._state["boot"]:
            table.clear()
        self._state = state
        decider = state["mode"]["decider"]
        # Oldest first and updated in place: a row never moves under the cursor.
        wanted = {_row_key(thread): thread for thread in reversed(state["threads"])}
        for key in [row.value for row in table.rows if row.value not in wanted]:
            table.remove_row(key)
        present = {row.value for row in table.rows}
        for key, thread in wanted.items():
            cells = _thread_cells(thread, decider, state["now"])
            if key in present:
                for column, cell in zip(THREAD_COLUMNS, cells, strict=True):
                    table.update_cell(key, column, cell, update_width=True)
            else:
                table.add_row(*cells, key=key)
        if first and table.row_count:
            table.move_cursor(row=table.row_count - 1)  # the most recently seen
            self._selected = _row_key(state["threads"][0])
        self._show_header()
        self._show_detail()

    def _show_header(self) -> None:
        self.header_bar.update(header_text(self._state, self._live))

    def _show_detail(self) -> None:
        table, context = self.detail_table, self.about_box
        table.clear()
        thread = self._thread()
        if thread is None:
            table.border_title = "no thread selected"
            context.update("")
            return
        judges = list(thread["judges"])
        judge = judges[self._judge % len(judges)]
        view = thread["judges"][judge]
        table.border_title = escape(printable(f"{thread['label']} · {judge}"))
        for qid, stat in view["questions"].items():
            table.add_row(*_question_cells(qid, stat, view["evidence"].get(qid)))
        context.update(_about(thread, view))

    def _show_feed(self) -> None:
        log = self.feed_log
        log.clear()
        label = self.selected_label if self._narrowed else None
        log.border_title = escape(printable(f"feed · {label or 'all threads'}"))
        for record in self._records:
            if self._wanted(record):
                log.write(render_line(record))

    def _wanted(self, record: dict) -> bool:
        return not self._narrowed or record["surface"] == self.selected_label

    def _thread(self) -> dict | None:
        threads = [] if self._state is None else self._state["threads"]
        return next((t for t in threads if _row_key(t) == self._selected), None)

    # --- keys --------------------------------------------------------------------------------

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table.id != "threads" or event.row_key.value == self._selected:
            return
        self._selected = event.row_key.value
        self._show_detail()
        if self._narrowed:
            self._show_feed()

    def action_filter(self) -> None:
        self._narrowed = not self._narrowed
        self._show_feed()

    def action_judge(self) -> None:
        self._judge += 1
        self._show_detail()

    def action_quarantine(self) -> None:
        label = self.selected_label
        if label is None:
            return

        def answered(reason: str | None) -> None:
            if reason is not None:
                self._control(self.client.quarantine(label, reason.strip() or "manual"))

        self.push_screen(Ask(f"quarantine {label}: why?", "manual"), answered)

    def action_release(self) -> None:
        label = self.selected_label
        if label is not None:
            self._control(self.client.release(label))

    def action_context(self) -> None:
        label = self.selected_label
        if label is None:
            return

        def answered(text: str | None) -> None:
            if text is not None:
                self._control(self.client.context(label, text))

        question = f"what do you know about {label}'s session that the agent does not?"
        self.push_screen(Ask(question, "nothing: clear the context"), answered)

    def _control(self, call) -> None:
        async def do() -> None:
            try:
                await call
            except (ControlError, AttachError) as exc:
                self.notify(printable(str(exc)), severity="error", markup=False)
            self._dirty = True

        self.run_worker(do())


# --- what the cells say ----------------------------------------------------------------------


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


def _row_key(thread: dict) -> str:
    return f"{thread['session_id']}/{thread['agent_id']}"


def _thread_cells(thread: dict, decider: str, now: str) -> tuple[Text, Text, Text, Text]:
    over = thread["last_event"] in ENDED
    seen = "" if thread["last_seen"] is None else uptime(thread["last_seen"], now)
    return (
        Text(printable(thread["label"]), style="dim" if over else "cyan"),
        thread_status(thread, decider),
        Text(str(thread["judgments"]), justify="right"),
        Text("ended" if over else seen, style="dim", justify="right"),
    )


def _question_cells(qid: str, stat: dict, evidence: dict | None) -> tuple[Text, ...]:
    against = "" if evidence is None else f"{evidence['value']:.2f} / {evidence['limit']:.2f}"
    if stat["kind"] == "choice":
        counts = " ".join(f"{choice}×{n}" for choice, n in stat["counts"].items())
        cells = (qid, str(sum(stat["counts"].values())), str(stat["last"]), counts, "", "", "",
                 str(stat["streak"]), str(stat["longest"]), against)  # fmt: skip
    else:
        cells = (qid, str(stat["n"]), _num(stat["last"]), _num(stat["mean"]), _num(stat["ewma"]),
                 _num(stat["min"]), _num(stat["max"]), str(stat["streak"]), str(stat["longest"]),
                 against)  # fmt: skip
    flagged = "bold red" if stat["streak"] else ""
    return tuple(
        Text(printable(cell), style=flagged if n == 0 else "") for n, cell in enumerate(cells)
    )


def _about(thread: dict, view: dict) -> Text:
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
