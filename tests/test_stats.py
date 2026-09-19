import pytest

from jev_watchdog.judge.base import Answer, Verdict
from jev_watchdog.pack import Question
from jev_watchdog.stats import ChoiceStat, GlobalStats, NumericStat, SurfaceStats


def test_numeric_stat_tracks_moments_and_ewma():
    stat = NumericStat()
    for value in (1.0, 0.0, 0.5):
        stat.add(value, flagged=False)
    assert stat.n == 3 and stat.last == 0.5
    assert stat.mean == pytest.approx(0.5)
    assert (stat.min, stat.max) == (0.0, 1.0)
    # ewma: 1.0 -> 0.3*0 + 0.7*1 = 0.7 -> 0.3*0.5 + 0.7*0.7 = 0.64
    assert stat.ewma == pytest.approx(0.64)


def test_streaks_reset_and_remember_longest():
    stat = NumericStat()
    for flagged in (True, True, False, True):
        stat.add(0.9, flagged)
    assert stat.streak == 1 and stat.longest_streak == 2


def test_choice_stat_counts():
    stat = ChoiceStat()
    for value, flagged in (("a", False), ("stuck", True), ("stuck", True)):
        stat.add(value, flagged)
    assert stat.counts == {"a": 1, "stuck": 2}
    assert stat.last == "stuck" and stat.streak == 2 and stat.longest_streak == 2


def test_surface_stats_records_verdict_and_returns_flagged():
    questions = [
        Question("exfil", "noul", "i", flag_threshold=0.7),
        Question("serves_goal", "noul", "i", flag_below=0.3),
        Question(
            "activity", "choice", "i", criteria={"ok": "", "stuck": ""}, flag_choices=("stuck",)
        ),
        Question("unanswered", "noul", "i"),
    ]
    verdict = Verdict(
        {"exfil": Answer(0.95), "serves_goal": Answer(0.8), "activity": Answer("stuck")},
        latency_ms=600,
        input_tokens=1000,
        judge="fake",
    )
    stats = SurfaceStats()
    assert stats.record_verdict(questions, verdict) == {"exfil", "activity"}
    assert stats.judgments == 1
    assert isinstance(stats.questions["exfil"], NumericStat)
    assert isinstance(stats.questions["activity"], ChoiceStat)
    assert "unanswered" not in stats.questions


def test_surface_stats_events_and_errors():
    stats = SurfaceStats()
    stats.record_event("PostToolUse")
    stats.record_event("PostToolUse")
    stats.record_error("over_limit")
    assert stats.events == {"PostToolUse": 2} and stats.errors == {"over_limit": 1}


def test_global_stats_latency_tokens_cost():
    stats = GlobalStats()
    assert stats.latency_percentile(50) is None
    for latency in (100, 200, 300, 400):
        stats.record_verdict(Verdict({}, latency_ms=latency, input_tokens=250_000, judge="fake"))
    stats.record_verdict(Verdict({}, latency_ms=500, input_tokens=None, judge="fake"))
    assert stats.judgments == 5
    assert stats.latency_percentile(50) == 300
    assert stats.latency_percentile(95) == 500
    assert stats.input_tokens == 1_000_000
    assert stats.cost_usd == pytest.approx(0.042)
