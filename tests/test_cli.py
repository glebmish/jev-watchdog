from pathlib import Path

import pytest

from jev_watchdog.cli import DEFAULT_PORT, build_parser, main, resolve_api_key


def test_run_defaults():
    args = build_parser().parse_args(["run"])
    assert args.port == DEFAULT_PORT == 8787
    assert args.judge == ["jev"]
    assert args.claude_thinking is False
    assert args.pack == Path("pack.toml")
    assert args.key_file == Path("prototype-throwaway-key")
    assert args.log is None


def test_run_overrides():
    args = build_parser().parse_args(
        ["run", "--port", "9000", "--judge", "fake", "--pack", "p.toml", "--log", "out.jsonl"]
    )
    assert args.judge == ["fake"]
    assert (args.port, args.pack, args.log) == (9000, Path("p.toml"), Path("out.jsonl"))


def test_unknown_judge_is_rejected():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["run", "--judge", "nope"])


def test_env_key_wins(tmp_path):
    key_file = tmp_path / "key"
    key_file.write_text("apikey_file\n")
    assert resolve_api_key({"TYPESAFE_API_KEY": "apikey_env"}, key_file) == "apikey_env"


def test_key_file_fallback_is_stripped(tmp_path):
    key_file = tmp_path / "key"
    key_file.write_text("apikey_file\n")
    assert resolve_api_key({}, key_file) == "apikey_file"


def test_missing_key_exits_with_a_clear_message(tmp_path):
    with pytest.raises(SystemExit) as err:
        resolve_api_key({}, tmp_path / "absent")
    assert "TYPESAFE_API_KEY" in str(err.value)


async def test_port_in_use_exits_1_without_a_summary(capsys):
    import io
    import socket

    from rich.console import Console

    from jev_watchdog.cli import _serve
    from jev_watchdog.judge.fake import FakeJudge
    from jev_watchdog.printer import Printer
    from jev_watchdog.surfaces import SurfaceRegistry

    out = io.StringIO()
    printer = Printer(Console(file=out, width=200, color_system=None))
    registry = SurfaceRegistry([FakeJudge()], [], printer)
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        assert await _serve(registry, printer, busy.getsockname()[1], "banner") == 1
    assert "cannot listen on 127.0.0.1" in capsys.readouterr().err
    assert out.getvalue() == ""


def test_judge_is_repeatable_and_takes_a_model():
    args = build_parser().parse_args(
        ["run", "--judge", "jev", "--judge", "claude:claude-haiku-4-5", "--claude-thinking"]
    )
    assert args.judge == ["jev", "claude:claude-haiku-4-5"]
    assert args.claude_thinking is True


def test_duplicate_judges_are_rejected():
    from jev_watchdog.cli import main

    with pytest.raises(SystemExit) as err:
        main(["run", "--judge", "fake", "--judge", "fake"])
    assert "more than once" in str(err.value)


def test_unknown_backend_in_a_spec_is_rejected():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["run", "--judge", "gpt:4"])


def test_replay_runs_cases_offline_and_reports(tmp_path, capsys, monkeypatch):
    from jev_watchdog.cli import main

    repo = Path(__file__).resolve().parent.parent
    case = tmp_path / "tiny.jsonl"
    case.write_text('{"type":"user","message":{"role":"user","content":"fix the test"}}\n')
    (tmp_path / "tiny.expect.toml").write_text('[[expect]]\nstep = 1\nclear = ["exfil"]\n')
    monkeypatch.setenv("COLUMNS", "200")
    code = main(
        ["replay", str(case), "--judge", "fake", "--pack", str(repo / "pack.toml"),
         "--log", str(tmp_path / "run.jsonl")]
    )  # fmt: skip
    out = capsys.readouterr().out
    assert code == 0
    assert "tiny/main" in out and "expectations:" in out and "judges" in out


def test_replay_needs_at_least_one_case():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["replay"])


def test_enforce_is_off_by_default():
    assert build_parser().parse_args(["run"]).enforce is False
    assert build_parser().parse_args(["run", "--enforce"]).enforce is True


def test_control_commands_parse():
    args = build_parser().parse_args(["quarantine", "1d8e7c/main", "--reason", "x", "--port", "9"])
    assert (args.target, args.reason, args.port) == ("1d8e7c/main", "x", 9)
    assert build_parser().parse_args(["release", "1d8e7c"]).target == "1d8e7c"
    assert build_parser().parse_args(["status"]).port == DEFAULT_PORT


def test_control_commands_report_a_missing_watchdog(capsys):
    assert main(["status", "--port", "1"]) == 1
    assert "no watchdog listening on port 1" in capsys.readouterr().err
