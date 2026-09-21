import pytest

from jev_watchdog.core.pack import Question
from jev_watchdog.core.stats import (
    ChoiceStat,
    GlobalStats,
    JudgeStats,
    NumericStat,
    SurfaceStats,
    percentile,
)
from jev_watchdog.judge.base import Answer, Verdict


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


QUESTIONS = [
    Question("exfil", "noul", "i", flag_threshold=0.7),
    Question("serves_goal", "noul", "i", flag_below=0.3),
    Question("activity", "choice", "i", criteria={"ok": "", "stuck": ""}, flag_choices=("stuck",)),
    Question("unanswered", "noul", "i"),
]


def test_surface_stats_keeps_each_judge_apart():
    stats = SurfaceStats()
    jev = Verdict(
        {"exfil": Answer(0.95), "serves_goal": Answer(0.8), "activity": Answer("stuck")},
        latency_ms=300,
        input_tokens=1000,
        judge="jev-1.13.0",
    )
    claude = Verdict({"exfil": Answer(0.1)}, latency_ms=5000, input_tokens=4000, judge="haiku")
    assert stats.judge("jev").record_verdict(QUESTIONS, jev) == {"exfil", "activity"}
    assert stats.judge("claude:haiku").record_verdict(QUESTIONS, claude) == set()
    stats.judge("claude:haiku").record_error("timeout")

    assert list(stats.judges) == ["jev", "claude:haiku"]
    assert stats.judgments == 2
    assert stats.judges["jev"].judgments == 1
    assert isinstance(stats.judges["jev"].questions["exfil"], NumericStat)
    assert isinstance(stats.judges["jev"].questions["activity"], ChoiceStat)
    assert "unanswered" not in stats.judges["jev"].questions
    assert stats.judges["claude:haiku"].questions["exfil"].last == 0.1
    assert stats.judges["claude:haiku"].errors == {"timeout": 1}


def test_surface_stats_events_and_errors():
    stats = SurfaceStats()
    stats.record_event("PostToolUse")
    stats.record_event("PostToolUse")
    stats.record_error("transcript")
    assert stats.events == {"PostToolUse": 2} and stats.errors == {"transcript": 1}


def test_percentile_is_nearest_rank():
    assert percentile([], 50) is None
    assert percentile([400, 100, 300, 200, 500], 50) == 300
    assert percentile([400, 100, 300, 200, 500], 95) == 500
    assert percentile([7], 95) == 7


def test_judge_stats_latency_lag_tokens_cost():
    stats = JudgeStats()
    for latency in (100, 200, 300):
        verdict = Verdict({}, latency, input_tokens=1000, judge="x", cost_usd=0.01)
        stats.record_verdict(verdict, lag_ms=latency * 2)
    stats.record_verdict(Verdict({}, 400, input_tokens=None, judge="x"), lag_ms=4000)
    stats.record_error("over_limit")
    assert stats.judgments == 4 and stats.errors == {"over_limit": 1}
    assert stats.latency(50) == 200 and stats.lag(95) == 4000
    assert stats.mean_latency == pytest.approx(250)
    assert stats.input_tokens == 3000
    assert stats.cost_usd == pytest.approx(0.03)
    assert JudgeStats().mean_latency is None


def test_global_stats_totals_across_judges():
    stats = GlobalStats()
    stats.record_event("Stop")
    stats.record_error("payload")
    stats.judge("jev").record_verdict(Verdict({}, 300, 1000, "jev", cost_usd=0.001), lag_ms=310)
    stats.judge("claude").record_verdict(Verdict({}, 5000, 4000, "c", cost_usd=0.009), lag_ms=9000)
    stats.judge("claude").record_error("timeout")
    assert list(stats.judges) == ["jev", "claude"]
    assert stats.judgments == 2
    assert stats.cost_usd == pytest.approx(0.01)
    assert stats.errors == {"payload": 1}
