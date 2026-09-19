from pathlib import Path

import pytest

from jev_watchdog.pack import PackError, Question, load_pack

REPO = Path(__file__).resolve().parent.parent


def write(tmp_path, body: str) -> Path:
    path = tmp_path / "pack.toml"
    path.write_text(body, encoding="utf-8")
    return path


def test_default_pack_loads():
    questions = load_pack(REPO / "pack.toml")
    assert [q.id for q in questions] == [
        "denied_target",
        "denied_reroute",
        "serves_goal",
        "repeat_failed",
        "exfil",
        "bypass_intent",
        "goal_drift",
        "activity",
    ]
    by_id = {q.id: q for q in questions}
    assert by_id["serves_goal"].flag_below == 0.3
    assert by_id["goal_drift"].kind == "score" and len(by_id["goal_drift"].criteria) == 5
    assert by_id["activity"].flag_choices == ("stuck", "off_task")


def test_flags():
    assert Question("q", "noul", "i", flag_threshold=0.7).flags(0.7)
    assert not Question("q", "noul", "i", flag_threshold=0.7).flags(0.69)
    assert Question("q", "noul", "i", flag_below=0.3).flags(0.3)
    assert not Question("q", "noul", "i", flag_below=0.3).flags(0.31)
    assert not Question("q", "noul", "i").flags(1.0)
    choice = Question("q", "choice", "i", criteria={"a": "A", "b": "B"}, flag_choices=("b",))
    assert choice.flags("b") and not choice.flags("a")


@pytest.mark.parametrize(
    "body, fragment",
    [
        ("", "no questions"),
        ('[questions.q]\nkind = "bool"\ninstructions = "x"', "kind"),
        ('[questions.q]\nkind = "noul"', "instructions"),
        ('[questions.q]\nkind = "noul"\ninstructions = "x"\ncriteria = ["a","b"]', "criteria"),
        ('[questions.q]\nkind = "score"\ninstructions = "x"\ncriteria = ["only"]', "criteria"),
        ('[questions.q]\nkind = "choice"\ninstructions = "x"\ncriteria = ["a","b"]', "criteria"),
        (
            '[questions.q]\nkind = "noul"\ninstructions = "x"\nflag_threshold = 0.5\nflag_below = 0.1',
            "flag_threshold",
        ),
        (
            '[questions.q]\nkind = "choice"\ninstructions = "x"\nflag_choices = ["z"]\n'
            '[questions.q.criteria]\na = "A"\nb = "B"',
            "flag_choices",
        ),
    ],
)
def test_invalid_packs_are_rejected(tmp_path, body, fragment):
    with pytest.raises(PackError, match=fragment):
        load_pack(write(tmp_path, body))
