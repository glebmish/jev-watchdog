import asyncio

from textual.widgets import DataTable, Input, RichLog, Static

from jev_watchdog.attach import AttachError, ControlError
from jev_watchdog.tui import WatchdogApp, header_text, thread_status, uptime

MAIN = "aaaaaa/main"
SUB = "aaaaaa/bbbbbb:Explore"


def thread(label: str, agent_id: str, **over) -> dict:
    numeric = {
        "kind": "numeric", "n": 3, "last": 0.1, "mean": 0.05, "ewma": 0.06,
        "min": 0.0, "max": 0.1, "streak": 0, "longest": 1,
    }  # fmt: skip
    choice = {"kind": "choice", "counts": {"on_task": 3}, "last": "on_task", "streak": 0,
              "longest": 0}  # fmt: skip
    view = {
        "judgments": 3,
        "errors": {},
        "tripped": False,
        "evidence": {"exfil": {"value": 0.14, "limit": 0.2}},
        "questions": {"exfil": numeric, "activity": choice},
    }
    base = {
        "label": label,
        "session_id": "aaaaaa-session",
        "agent_id": agent_id,
        "agent_type": None,
        "cwd": "/work",
        "last_event": "PostToolUse",
        "last_seen": "2026-09-20T09:59:50",
        "events": 7,
        "judgments": 3,
        "context": None,
        "quarantine": None,
        "judges": {"jev": view, "claude": view | {"judgments": 1}},
    }
    return base | over


def make_state(*threads: dict, boot: str = "boot-1", enforce: bool = True) -> dict:
    return {
        "boot": boot,
        "pid": 42,
        "started_at": "2026-09-20T07:46:00",
        "now": "2026-09-20T10:00:00",
        "port": 8787,
        "log": "runs/x.jsonl",
        "mode": {"enforce": enforce, "rules": ["exfil"], "decider": "jev"},
        "judges": [{"name": "jev", "judgments": 412}, {"name": "claude", "judgments": 9}],
        "totals": {"surfaces": 2, "events": 900, "judgments": 421, "errors": {},
                   "quarantines": 0, "rejected": 0, "cost_usd": 0.3123},
        "threads": list(threads),
    }  # fmt: skip


def note(seq: int, surface: str, message: str) -> dict:
    return {"seq": seq, "ts": "2026-09-20T09:59:50", "kind": "note", "surface": surface,
            "message": message}  # fmt: skip


class FakeClient:
    """A watchdog as the dashboard sees it. `end()` breaks the stream; `boot` may then change."""

    def __init__(self, state: dict, records: list[dict]) -> None:
        self.current, self.records, self.calls = state, list(records), []
        self.refuse: str | None = None
        self.down = False
        self._wake = asyncio.Event()
        self._ended = False

    async def state(self) -> dict:
        if self.down:
            raise AttachError("down")
        return self.current

    async def events(self, since: int = 0):
        if self.down:
            raise AttachError("down")
        self._ended = False
        yield "hello", {"boot": self.current["boot"]}
        sent = since
        while not self._ended:
            for record in [r for r in self.records if r["seq"] > sent]:
                sent = record["seq"]
                yield "record", record
            self._wake.clear()
            await self._wake.wait()

    def publish(self, record: dict) -> None:
        self.records.append(record)
        self._wake.set()

    def end(self) -> None:
        self._ended = True
        self._wake.set()

    async def quarantine(self, target: str, reason: str) -> dict:
        return self._control("quarantine", target, reason)

    async def release(self, target: str) -> dict:
        return self._control("release", target)

    async def context(self, target: str, text: str) -> dict:
        return self._control("context", target, text)

    def _control(self, *call) -> dict:
        self.calls.append(call)
        if self.refuse:
            raise ControlError(self.refuse)
        return {}


def make_app(client: FakeClient, **options) -> WatchdogApp:
    return WatchdogApp(client, tick_s=0.01, retry_s=0.01, **options)


async def until(pilot, condition, what: str) -> None:
    for _ in range(200):
        if condition():
            return
        await pilot.pause(0.01)
    app = pilot.app
    raise AssertionError(
        f"never happened: {what}; header={str(app.header_bar.render())!r} live={app._live} "
        f"workers={[(w.name, w.state) for w in app.workers]}"
    )


def feed_lines(app: WatchdogApp) -> list[str]:
    return [strip.text.rstrip() for strip in app.query_one("#feed", RichLog).lines]


def thread_labels(app: WatchdogApp) -> list[str]:
    table = app.query_one("#threads", DataTable)
    return [str(table.get_row_at(index)[0]) for index in range(table.row_count)]


def two_threads() -> FakeClient:
    # /state lists the most recently seen first; the table is oldest first, like the feed.
    state = make_state(thread(SUB, "bbbbbb", agent_type="Explore"), thread(MAIN, "main"))
    return FakeClient(state, [note(1, MAIN, "from main"), note(2, SUB, "from sub")])


async def test_threads_are_listed_oldest_first_with_the_newest_selected():
    app = make_app(two_threads())
    async with app.run_test(size=(140, 40)) as pilot:
        await until(pilot, lambda: len(thread_labels(app)) == 2, "threads listed")
        assert thread_labels(app) == [MAIN, SUB]
        await until(pilot, lambda: app.selected_label == SUB, "newest selected")
        assert "● live" in str(app.query_one("#header", Static).render())


async def test_the_feed_shows_everything_until_f_narrows_it_to_the_selected_thread():
    client = two_threads()
    app = make_app(client)
    async with app.run_test(size=(140, 40)) as pilot:
        await until(pilot, lambda: len(feed_lines(app)) == 2, "backlog shown")
        await until(pilot, lambda: app.selected_label == SUB, "newest selected")
        await pilot.press("f")
        await until(pilot, lambda: len(feed_lines(app)) == 1, "narrowed")
        assert "from sub" in feed_lines(app)[0]
        await pilot.press("up")
        await until(pilot, lambda: "from main" in "".join(feed_lines(app)), "follows selection")
        client.publish(note(3, SUB, "sub again"))
        client.publish(note(4, MAIN, "main again"))
        await until(pilot, lambda: len(feed_lines(app)) == 2, "only the selected thread's")
        assert "main again" in feed_lines(app)[1]
        await pilot.press("f")
        await until(pilot, lambda: len(feed_lines(app)) == 4, "everything again")


async def test_x_asks_for_a_reason_and_quarantines_the_selected_thread():
    client = two_threads()
    app = make_app(client)
    async with app.run_test(size=(140, 40)) as pilot:
        await until(pilot, lambda: app.selected_label == SUB, "selected")
        await pilot.press("x")
        await until(pilot, lambda: bool(app.screen.query(Input)), "asked")
        await pilot.press(*"odd", "enter")
        await until(pilot, lambda: client.calls == [("quarantine", SUB, "odd")], "quarantined")


async def test_an_empty_reason_is_manual_and_escape_does_nothing():
    client = two_threads()
    app = make_app(client)
    async with app.run_test(size=(140, 40)) as pilot:
        await until(pilot, lambda: app.selected_label == SUB, "selected")
        await pilot.press("x")
        await until(pilot, lambda: bool(app.screen.query(Input)), "asked")
        await pilot.press("escape")
        await pilot.press("x")
        await until(pilot, lambda: bool(app.screen.query(Input)), "asked again")
        await pilot.press("enter")
        await until(pilot, lambda: client.calls == [("quarantine", SUB, "manual")], "quarantined")


async def test_r_releases_and_c_sets_or_clears_the_context():
    client = two_threads()
    app = make_app(client)
    async with app.run_test(size=(140, 40)) as pilot:
        await until(pilot, lambda: app.selected_label == SUB, "selected")
        await pilot.press("r")
        await until(pilot, lambda: client.calls == [("release", SUB)], "released")
        await pilot.press("c")
        await until(pilot, lambda: bool(app.screen.query(Input)), "asked")
        await pilot.press(*"ok", "enter")
        await until(pilot, lambda: client.calls[-1] == ("context", SUB, "ok"), "context set")
        await pilot.press("c")
        await until(pilot, lambda: bool(app.screen.query(Input)), "asked")
        await pilot.press("enter")
        await until(pilot, lambda: client.calls[-1] == ("context", SUB, ""), "context cleared")


async def test_a_refusal_is_a_notification_and_the_dashboard_lives_on():
    client = two_threads()
    client.refuse = "aaaaaa/main is not [quarantined]"
    app = make_app(client)
    async with app.run_test(size=(140, 40), notifications=True) as pilot:
        await until(pilot, lambda: app.selected_label == SUB, "selected")
        await pilot.press("r")
        await until(pilot, lambda: len(app._notifications) == 1, "notified")
        assert "is not [quarantined]" in next(iter(app._notifications)).message
        assert app.is_running


async def test_a_restarted_watchdog_clears_the_feed_and_the_header_is_live_again():
    client = two_threads()
    app = make_app(client)
    async with app.run_test(size=(140, 40)) as pilot:
        await until(pilot, lambda: len(feed_lines(app)) == 2, "backlog shown")
        await until(pilot, lambda: len(thread_labels(app)) == 2, "threads listed")
        client.down = True
        client.end()
        header = app.query_one("#header", Static)
        await until(pilot, lambda: "○ reconnecting" in str(header.render()), "loss shown")
        client.current = make_state(thread(MAIN, "main"), boot="boot-2")
        client.records = [note(1, MAIN, "a new life")]
        client.down = False
        await until(pilot, lambda: "● live" in str(header.render()), "live again")
        await until(pilot, lambda: len(feed_lines(app)) == 1, "old feed gone")
        assert "a new life" in feed_lines(app)[0]
        await until(pilot, lambda: thread_labels(app) == [MAIN], "old threads gone")


async def test_a_broken_stream_of_the_same_watchdog_resumes_without_repeats():
    client = two_threads()
    app = make_app(client)
    async with app.run_test(size=(140, 40)) as pilot:
        await until(pilot, lambda: len(feed_lines(app)) == 2, "backlog shown")
        client.end()
        client.publish(note(3, MAIN, "after the break"))
        await until(pilot, lambda: len(feed_lines(app)) == 3, "resumed")
        assert [line.split()[-1] for line in feed_lines(app)] == ["main", "sub", "break"]


async def test_agent_chosen_text_cannot_carry_escapes_or_markup():
    evil = "aaaaaa/[red]x\x1b[2J"
    state = make_state(thread(evil, "main", context="[bold]ctx\x1b]52;c;x\x07"))
    client = FakeClient(state, [note(1, evil, "[link=x]hi\x1b[0m")])
    app = make_app(client)
    async with app.run_test(size=(140, 40)) as pilot:
        await until(pilot, lambda: len(feed_lines(app)) == 1, "shown")
        await until(pilot, lambda: len(thread_labels(app)) == 1, "listed")
        shown = "\n".join([*feed_lines(app), *thread_labels(app)])
        shown += str(app.query_one("#context", Static).render())
        assert "\x1b" not in shown and "\x07" not in shown
        assert "[red]x�" in shown and "[link=x]hi�" in shown and "[bold]ctx�" in shown


async def test_j_shows_the_next_judges_view_of_the_thread():
    app = make_app(two_threads())
    async with app.run_test(size=(140, 40)) as pilot:
        detail = app.query_one("#detail", DataTable)
        await until(pilot, lambda: detail.row_count == 2, "questions shown")
        assert "· jev" in detail.border_title
        await pilot.press("j")
        await until(pilot, lambda: "· claude" in detail.border_title, "next judge")


async def test_q_leaves():
    app = make_app(two_threads())
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.press("q")
        await until(pilot, lambda: not app.is_running, "left")


def test_thread_status_says_what_matters_most():
    calm = thread(MAIN, "main")
    assert thread_status(calm, "jev").plain == "exfil 0.14/0.20"
    tripped = thread(MAIN, "main")
    tripped["judges"]["jev"] = tripped["judges"]["jev"] | {"tripped": True}
    assert thread_status(tripped, "jev").plain == "TRIPPED exfil 0.14/0.20"
    own = thread(MAIN, "main", quarantine={"target": MAIN, "reason": "r"})
    assert thread_status(own, "jev").plain == "QUARANTINED"
    inherited = thread(SUB, "bbbbbb", quarantine={"target": MAIN, "reason": "r"})
    assert thread_status(inherited, "jev").plain == f"QUARANTINED with {MAIN}"
    unruled = thread(MAIN, "main")
    unruled["judges"]["jev"] = unruled["judges"]["jev"] | {"evidence": {}}
    assert thread_status(unruled, "jev").plain == ""


def test_header_names_the_mode_the_judges_and_the_totals():
    text = header_text(make_state(), live=True).plain
    assert "ENFORCING on exfil (jev)" in text and "judges jev,claude" in text
    assert "up 2h14" in text and "421 judgments" in text and "$0.31" in text and "● live" in text
    dry = header_text(make_state(enforce=False), live=False).plain
    assert "dry run on exfil" in dry and "○ reconnecting" in dry
    assert "no quarantine rules" in header_text(make_state() | {"mode": {"enforce": True,
        "rules": [], "decider": "jev"}}, live=True).plain  # fmt: skip
    assert "connecting" in header_text(None, live=False).plain


def test_uptime_is_short():
    assert uptime("2026-09-20T09:59:30", "2026-09-20T10:00:00") == "30s"
    assert uptime("2026-09-20T09:46:00", "2026-09-20T10:00:00") == "14m"
    assert uptime("2026-09-20T07:46:00", "2026-09-20T10:00:00") == "2h14"
    assert uptime("2026-09-17T07:46:00", "2026-09-20T10:00:00") == "3d02h"


async def test_records_and_state_keep_arriving_while_a_question_is_open():
    """query_one searches the screen on top: looked up then, the dashboard's widgets are gone."""
    client = two_threads()
    app = make_app(client)
    async with app.run_test(size=(140, 40)) as pilot:
        await until(pilot, lambda: app.selected_label == SUB, "selected")
        await pilot.press("x")
        await until(pilot, lambda: bool(app.screen.query(Input)), "asked")
        third = thread("cccccc/main", "main", session_id="cccccc-session")
        client.current = make_state(third, *client.current["threads"])
        client.publish(note(3, "cccccc/main", "meanwhile"))
        await until(pilot, lambda: len(thread_labels(app)) == 3, "listed behind the question")
        assert len(feed_lines(app)) == 3 and app.is_running
        await pilot.press("escape")
        assert app.selected_label == SUB  # a new thread joins below; the cursor stays
