"""The dashboard of `attach` and `run --tui`: threads, one thread's statistics, and below
them one of four views: the feed, the selected thread's evidence as a line, every thread's
last hour as a heat map, the judges' latency and lag.

One loop asks the watchdog: every tick for the records after the last one it has, and, when
some came, a key was pressed or a while has passed, for /state and the chart that is up.
Nothing is computed here; what is drawn, and how, is in draw.py.
"""

import asyncio
import time
from collections import deque
from typing import ClassVar

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.markup import escape
from textual.screen import ModalScreen
from textual.widgets import ContentSwitcher, DataTable, Footer, Input, Label, RichLog, Static
from textual_plotext import PlotextPlot

from jev_watchdog import draw
from jev_watchdog.client import Client, Refused, Unreachable
from jev_watchdog.feed import BACKLOG
from jev_watchdog.printer import printable, render_line

TICK_S = 0.25  # the feed is asked for this often, /state at most this often
IDLE_REFRESH_S = 2.0  # /state when nothing happens: uptime, a thread going quiet
WINDOWS_MIN = (60, 15, 5, 360, 1440)  # of the timeline; `t` goes round


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
    #views { height: 1fr; border: round $primary; }
    #feed, #evidence, #timeline, #judges { height: 1fr; }
    #timeline { padding: 0 1; }
    """
    BINDINGS: ClassVar = [
        Binding("x", "quarantine", "quarantine"),
        Binding("r", "release", "release"),
        Binding("c", "context", "context"),
        Binding("f", "filter", "feed: all/thread"),
        Binding("j", "judge", "next judge"),
        Binding("1", "view('feed')", "feed"),
        Binding("2", "view('evidence')", "evidence"),
        Binding("3", "view('timeline')", "timeline"),
        Binding("4", "view('judges')", "judges"),
        Binding("t", "window", "timeline window"),
        Binding("ctrl+c", "quit", show=False, priority=True),
    ]

    def __init__(self, client: Client, owns_watchdog: bool = False, tick_s: float = TICK_S) -> None:
        super().__init__()
        self.client = client
        self.tick_s = tick_s
        # `run --tui` is the watchdog: leaving the dashboard stops it. `attach` only looks.
        self.bind("q", "quit", description="stop the watchdog" if owns_watchdog else "detach")
        self._state: dict | None = None
        self._threads: dict[str, dict] = {}  # of the last /state, by row key, oldest first
        self._records: deque[dict] = deque(maxlen=BACKLOG)
        self._boot: str | None = None
        self._live = False
        self._dirty = True  # a key changed what /state or the chart should show
        self._selected: str | None = None  # row key of the selected thread
        self._narrowed = False  # feed: only the selected thread
        self._judge = 0
        self._window = 0  # index into WINDOWS_MIN
        self._complained: str | None = None  # the last thing the loop complained of

    def compose(self) -> ComposeResult:
        yield Static(id="header")
        with Horizontal(id="top"):
            yield DataTable(id="threads", cursor_type="row", zebra_stripes=True)
            with Vertical():
                yield DataTable(id="detail", cursor_type="none")
                yield Static(id="context")
        with ContentSwitcher(initial="feed", id="views"):
            yield RichLog(id="feed", max_lines=BACKLOG, wrap=False, markup=False, highlight=False)
            yield PlotextPlot(id="evidence")
            yield Static(id="timeline")
            yield PlotextPlot(id="judges")
        yield Footer()

    def on_mount(self) -> None:
        # Kept, not looked up each time: query_one searches the screen on top, and state
        # and records keep arriving while that is the question of `x` or `c`.
        self.header_bar = self.query_one("#header", Static)
        self.threads_table = threads = self.query_one("#threads", DataTable)
        self.detail_table = self.query_one("#detail", DataTable)
        self.about_box = self.query_one("#context", Static)
        self.views = self.query_one("#views", ContentSwitcher)
        self.feed_log = self.query_one("#feed", RichLog)
        self.evidence_plot = self.query_one("#evidence", PlotextPlot)
        self.timeline_box = self.query_one("#timeline", Static)
        self.judges_plot = self.query_one("#judges", PlotextPlot)
        for column in draw.THREAD_COLUMNS:
            threads.add_column(column, key=column)
        threads.border_title = "threads"
        self.detail_table.add_columns(*draw.DETAIL_COLUMNS)
        self._show_header()
        self._show_detail()
        self._show_feed()
        threads.focus()
        self.run_worker(self._watch(), name="watch")

    @property
    def selected_label(self) -> str | None:
        thread = self._threads.get(self._selected)
        return None if thread is None else thread["label"]

    # --- the conversation with the watchdog --------------------------------------------------

    async def _watch(self) -> None:
        asked = 0.0
        while True:
            try:
                arrived = await self._follow()
                if arrived or self._dirty or time.monotonic() - asked >= IDLE_REFRESH_S:
                    self._dirty, asked = False, time.monotonic()
                    self._show_state(await self.client.state())
                    await self._show_view()
                    self._complained = None
                self._set_live(True)
            except Unreachable:
                self._set_live(False)
            except Exception as exc:  # noqa: BLE001 - this loop is all that keeps the screen true
                # The watchdog's no (an older one may not know a chart's request), or an
                # answer in a shape this dashboard does not draw. Say it once and keep asking.
                complaint = (
                    str(exc) if isinstance(exc, Refused) else f"{self.views.current}: {exc!r}"
                )
                if complaint != self._complained:
                    self._complained = complaint
                    self.notify(printable(complaint), severity="warning", markup=False)
            await asyncio.sleep(self.tick_s)

    def _set_live(self, live: bool) -> None:
        if live != self._live:  # not every tick: a screen that is redrawn is never idle
            self._live = live
            self._show_header()

    async def _follow(self) -> bool:
        """Take in the records after the last one held; whether any came."""
        last = self._records[-1]["seq"] if self._records else 0
        answer = await self.client.records(last)
        if self._boot not in (None, answer["boot"]):  # restarted: its numbers start over
            self._records.clear()
            self._show_feed()
            answer = await self.client.records(0)
        self._boot = answer["boot"]
        for record in answer["records"]:
            self._records.append(record)
            if self._wanted(record):
                self.feed_log.write(render_line(record))
        return bool(answer["records"])

    # --- drawing -----------------------------------------------------------------------------

    def _show_state(self, state: dict) -> None:
        table = self.threads_table
        first = self._state is None
        self._state = state
        # Updated in place: a row never moves under the cursor. New threads join at the end.
        self._threads = {draw.row_key(thread): thread for thread in state["threads"]}
        for key in [row.value for row in table.rows if row.value not in self._threads]:
            table.remove_row(key)
        present = {row.value for row in table.rows}
        for key, thread in self._threads.items():
            cells = draw.thread_cells(thread, state["mode"]["decider"], state["now"])
            if key not in present:
                table.add_row(*cells, key=key)
            elif table.get_row(key) != list(cells):  # most rows of most ticks have not changed
                for column, cell in zip(draw.THREAD_COLUMNS, cells, strict=True):
                    table.update_cell(key, column, cell, update_width=True)
        if first and table.row_count:
            table.move_cursor(row=table.row_count - 1)  # the most recently heard of
            self._selected = draw.row_key(state["threads"][-1])
        self._show_header()
        self._show_detail()

    def _show_header(self) -> None:
        self.header_bar.update(draw.header_text(self._state, self._live))

    def _show_detail(self) -> None:
        table, thread = self.detail_table, self._threads.get(self._selected)
        table.clear()
        if thread is None:
            table.border_title = "no thread selected"
            self.about_box.update("")
            return
        judge = self._judge_name()
        view = thread["judges"][judge]
        table.border_title = escape(printable(f"{thread['label']} · {judge}"))
        for qid, stat in view["questions"].items():
            table.add_row(*draw.question_cells(qid, stat, view["evidence"].get(qid)))
        self.about_box.update(draw.about(thread, view))

    def _show_feed(self) -> None:
        self.feed_log.clear()
        self._title_view()
        for record in self._records:
            if self._wanted(record):
                self.feed_log.write(render_line(record))

    async def _show_view(self) -> None:
        """Draw the chart that is up. The feed draws itself as records arrive."""
        view, thread = self.views.current, self._threads.get(self._selected)
        if view == "evidence":
            history = None
            if thread is not None:
                history = await self.client.history(thread["session_id"], thread["agent_id"])
            draw.draw_evidence(self.evidence_plot, history, self._judge_name())
        elif view == "timeline":
            buckets = max(self.timeline_box.size.width - draw.TIMELINE_LABEL - 6, 10)
            timeline = await self.client.timeline(self._minutes, buckets, self._judge_name())
            self.timeline_box.update(draw.timeline_text(timeline, self.selected_label))
        elif view == "judges":
            draw.draw_judges(self.judges_plot, self._state["judges"])

    def _title_view(self) -> None:
        label, view = self.selected_label, self.views.current
        if view == "feed":
            title = f"feed · {label if self._narrowed and label else 'all threads'}"
        elif view == "evidence":
            title = f"evidence, as a share of each rule's limit · {label or 'no thread'}"
        elif view == "timeline":
            title = f"timeline · last {draw.span(self._minutes)} · t for another window"
        else:
            title = "judges · latency, and lag from hook to verdict, of the last judgments"
        self.views.border_title = escape(printable(title))

    @property
    def _minutes(self) -> int:
        return WINDOWS_MIN[self._window % len(WINDOWS_MIN)]

    def _judge_name(self) -> str | None:
        """The judge that `j` has chosen."""
        names = [judge["name"] for judge in self._state["judges"]] if self._state else []
        return names[self._judge % len(names)] if names else None

    def _wanted(self, record: dict) -> bool:
        return not self._narrowed or record["surface"] == self.selected_label

    # --- keys --------------------------------------------------------------------------------

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table.id != "threads" or event.row_key.value == self._selected:
            return
        self._selected = event.row_key.value
        self._show_detail()
        if self._narrowed:
            self._show_feed()
        self._title_view()
        self._dirty = True  # the evidence chart is this thread's

    def action_filter(self) -> None:
        self._narrowed = not self._narrowed
        self._show_feed()

    def action_judge(self) -> None:
        self._judge += 1
        self._show_detail()
        self._dirty = True  # the charts are one judge's view

    def action_view(self, view: str) -> None:
        self.views.current = view
        self._title_view()
        self._dirty = True

    def action_window(self) -> None:
        self._window += 1
        self._title_view()
        self._dirty = True

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
            except (Refused, Unreachable) as exc:
                self.notify(printable(str(exc)), severity="error", markup=False)
            self._dirty = True

        self.run_worker(do())
