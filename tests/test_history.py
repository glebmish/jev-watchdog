from datetime import datetime, timedelta

from jev_watchdog.history import History
from jev_watchdog.pack import Question
from jev_watchdog.transcript import SurfaceKey

RULES = [
    Question("exfil", "noul", "i", quarantine_ref=0.45, quarantine_limit=0.2),
    Question("drift", "score", "i", quarantine_ref=1.0, quarantine_limit=2.0),
]
T0 = datetime(2026, 9, 20, 10, 0, 0)
MAIN = SurfaceKey("s1", "main")
SUB = SurfaceKey("s1", "abc")


def at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


def test_series_is_each_judges_evidence_as_a_share_of_the_limits_with_the_marks():
    history = History(RULES)
    history.record(MAIN, "jev", at(0), {"exfil": 0.1}, folded=True)
    history.record(MAIN, "jev", at(1), {"exfil": 0.1}, folded=False)
    history.record(MAIN, "claude", at(1), {"exfil": 0.3, "drift": 1.0}, folded=True)
    history.mark(MAIN, at(2), "quarantine")
    assert history.series(MAIN) == {
        "judges": {
            "jev": [
                {"ts": "2026-09-20T10:00:00", "shares": {"exfil": 0.5, "drift": 0.0},
                 "folded": True},
                {"ts": "2026-09-20T10:01:00", "shares": {"exfil": 0.5, "drift": 0.0},
                 "folded": False},
            ],
            "claude": [
                {"ts": "2026-09-20T10:01:00", "shares": {"exfil": 1.5, "drift": 0.5},
                 "folded": True},
            ],
        },
        "marks": [{"ts": "2026-09-20T10:02:00", "kind": "quarantine", "judge": None}],
    }  # fmt: skip
    assert history.series(SUB) == {"judges": {}, "marks": []}


def test_only_the_last_points_are_kept():
    history = History(RULES, size=3)
    for n in range(5):
        history.record(MAIN, "jev", at(n), {"exfil": n / 10}, folded=True)
    assert [p["shares"]["exfil"] for p in history.series(MAIN)["judges"]["jev"]] == [1.0, 1.5, 2.0]


def test_a_release_puts_every_judges_line_back_to_zero():
    history = History(RULES)
    history.record(MAIN, "jev", at(0), {"exfil": 0.4}, folded=True)
    history.mark(MAIN, at(1), "release")
    last = history.series(MAIN)["judges"]["jev"][-1]
    assert last == {"ts": "2026-09-20T10:01:00", "shares": {"exfil": 0.0, "drift": 0.0},
                    "folded": True}  # fmt: skip


def test_timeline_is_the_worst_share_of_a_limit_per_bucket():
    history = History(RULES)
    history.record(MAIN, "jev", at(1), {"exfil": 0.05, "drift": 1.0}, folded=True)
    history.record(MAIN, "jev", at(1.5), {"exfil": 0.1}, folded=True)
    history.record(MAIN, "jev", at(35), {"exfil": 0.4}, folded=True)
    history.record(MAIN, "claude", at(2), {"exfil": 0.2}, folded=True)
    history.record(SUB, "jev", at(-90), {"exfil": 0.2}, folded=True)  # too old
    history.mark(MAIN, at(35), "quarantine")
    history.mark(MAIN, at(50), "release")
    history.mark(MAIN, at(3), "trip", judge="claude")
    threads = [(SUB, "s1/abc"), (MAIN, "s1/main")]
    timeline = history.timeline(threads, "jev", now=at(60), minutes=60, buckets=6)
    assert timeline["start"] == "2026-09-20T10:00:00" and timeline["end"] == "2026-09-20T11:00:00"
    assert timeline["bucket_s"] == 600 and timeline["judge"] == "jev"
    (row,) = timeline["threads"]  # a thread with nothing in the window is left out
    assert row["label"] == "s1/main" and (row["session_id"], row["agent_id"]) == MAIN
    assert row["cells"] == [0.5, None, None, 2.0, None, 0.0]
    assert row["marks"] == {"3": "quarantine", "5": "release"}  # claude's trip is not jev's

    claude = history.timeline(threads, "claude", now=at(60), minutes=60, buckets=6)
    assert claude["threads"][0]["cells"][0] == 1.0
    assert claude["threads"][0]["marks"] == {"0": "trip", "3": "quarantine", "5": "release"}


def test_only_the_most_recently_judged_threads_are_kept():
    """A service runs for weeks; the dashboard never shows more threads than this."""
    history = History(RULES, threads=2)
    keys = [SurfaceKey(f"s{n}", "main") for n in range(3)]
    for n, key in enumerate(keys):
        history.record(key, "jev", at(n), {"exfil": 0.1}, folded=True)
        history.mark(key, at(n), "trip", judge="jev")
    history.record(keys[1], "jev", at(5), {"exfil": 0.2}, folded=True)  # still alive
    history.record(SurfaceKey("s3", "main"), "jev", at(6), {"exfil": 0.1}, folded=True)
    kept = [key for key in [*keys, SurfaceKey("s3", "main")] if history.series(key)["judges"]]
    assert kept == [keys[1], SurfaceKey("s3", "main")]
    assert history.series(keys[0])["marks"] == [] and history.series(keys[2])["marks"] == []
