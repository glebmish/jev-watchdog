from pathlib import Path

import pytest

from jev_watchdog.cli import DEFAULT_PORT, build_parser, main, resolve_api_key


def test_run_defaults():
    args = build_parser().parse_args(["run"])
    assert args.port == DEFAULT_PORT == 8787
    assert args.judge == ["jev"]
    assert args.claude_thinking is False
    assert args.pack == [Path("pack.toml")]
    assert args.key_file == Path("prototype-throwaway-key")
    assert args.log is None


def test_run_overrides():
    args = build_parser().parse_args(
        ["run", "--port", "9000", "--judge", "fake", "--pack", "p.toml", "--log", "out.jsonl"]
    )
    assert args.judge == ["fake"]
    assert (args.port, args.pack, args.log) == (9000, [Path("p.toml")], Path("out.jsonl"))


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


def test_pack_is_repeatable_and_replaces_the_default():
    args = build_parser().parse_args(["run", "--pack", "pack.toml", "--pack", "packs/canary.toml"])
    assert args.pack == [Path("pack.toml"), Path("packs/canary.toml")]


def test_context_options_parse():
    assert build_parser().parse_args(["run"]).context is None
    assert build_parser().parse_args(["replay", "c.jsonl", "--context", "x"]).context == "x"
    args = build_parser().parse_args(["context", "1d8e7c", "some text", "--port", "9"])
    assert (args.target, args.text, args.clear, args.port) == ("1d8e7c", "some text", False, 9)
    assert build_parser().parse_args(["context", "1d8e7c", "--clear"]).text is None


def test_context_command_needs_text_or_clear(capsys):
    with pytest.raises(SystemExit):
        main(["context", "1d8e7c"])
    with pytest.raises(SystemExit):
        main(["context", "1d8e7c", "text", "--clear"])


def test_two_specs_of_the_same_judge_exit_with_a_message(tmp_path):
    key_file = tmp_path / "key"
    key_file.write_text("apikey_not_real\n")
    with pytest.raises(SystemExit) as err:
        main(
            ["run", "--judge", "jev", "--judge", "jev:jev-latest", "--key-file", str(key_file),
             "--log", str(tmp_path / "run.jsonl")]
        )  # fmt: skip
    assert "same judge" in str(err.value) and "jev" in str(err.value)


def replay(tmp_path, *cases: Path) -> int:
    repo = Path(__file__).resolve().parent.parent
    return main(
        ["replay", *map(str, cases), "--judge", "fake", "--pack", str(repo / "pack.toml"),
         "--log", str(tmp_path / "run.jsonl")]
    )  # fmt: skip


def test_replay_rejects_two_cases_of_the_same_name(tmp_path):
    """The name is the session id: the second case would continue the first one's thread."""
    for folder in ("a", "b"):
        (tmp_path / folder).mkdir()
        line = '{"type":"user","message":{"role":"user","content":"hi"}}\n'
        (tmp_path / folder / "same.jsonl").write_text(line)
    with pytest.raises(SystemExit) as err:
        replay(tmp_path, tmp_path / "a" / "same.jsonl", tmp_path / "b" / "same.jsonl")
    assert "cannot load case" in str(err.value) and "'same'" in str(err.value)


def test_replay_closes_its_judges_when_a_case_does_not_load(tmp_path, monkeypatch):
    from jev_watchdog.judge.fake import FakeJudge

    closed = []

    async def aclose(self):
        closed.append(self.name)

    monkeypatch.setattr(FakeJudge, "aclose", aclose)
    with pytest.raises(SystemExit):
        replay(tmp_path, tmp_path / "absent.jsonl")
    assert closed == ["fake"]  # claude and codex judges keep a temp dir until closed


def tiny_case(tmp_path) -> Path:
    case = tmp_path / "tiny.jsonl"
    case.write_text('{"type":"user","message":{"role":"user","content":"fix the test"}}\n')
    return case


def test_the_run_log_is_private_to_the_operator(tmp_path):
    """It holds every prompt, tool input and tool output of the watched sessions."""
    import os
    import stat

    before = os.umask(0o022)
    try:
        assert replay(tmp_path, tiny_case(tmp_path)) == 0
    finally:
        os.umask(before)
    assert stat.S_IMODE((tmp_path / "run.jsonl").stat().st_mode) == 0o600


def test_the_api_key_goes_to_the_jev_judge_and_out_of_the_environment(tmp_path, monkeypatch):
    """claude and codex judges start child processes, which inherit the environment."""
    import os

    from jev_watchdog import cli
    from jev_watchdog.judge.fake import FakeJudge

    seen = []

    def make_judge(spec, config):
        seen.append((config.api_key, os.environ.get("TYPESAFE_API_KEY")))
        return FakeJudge()

    monkeypatch.setenv("TYPESAFE_API_KEY", "apikey_env")
    monkeypatch.setattr(cli, "make_judge", make_judge)
    repo = Path(__file__).resolve().parent.parent
    argv = ["replay", str(tiny_case(tmp_path)), "--judge", "jev", "--pack", str(repo / "pack.toml")]
    assert main([*argv, "--log", str(tmp_path / "run.jsonl")]) == 0
    assert seen == [("apikey_env", None)]


def test_log_dir_places_the_timestamped_log(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    case = tiny_case(tmp_path)
    pack = Path(__file__).resolve().parent.parent / "pack.toml"
    arguments = ["replay", str(case), "--judge", "fake", "--pack", str(pack)]
    assert main([*arguments, "--log-dir", str(tmp_path / "logs")]) == 0
    (log,) = (tmp_path / "logs").iterdir()
    assert log.suffix == ".jsonl"


# --- attach and run --tui -----------------------------------------------------------------


def test_the_dashboard_commands_parse():
    assert build_parser().parse_args(["attach"]).port == DEFAULT_PORT
    assert build_parser().parse_args(["attach", "--port", "9000"]).port == 9000
    assert build_parser().parse_args(["run", "--tui"]).tui is True
    assert build_parser().parse_args(["run"]).tui is False
    with pytest.raises(SystemExit):
        build_parser().parse_args(["replay", "x.jsonl", "--tui"])


def test_attach_with_no_watchdog_says_so(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert main(["attach", "--port", "1"]) == 1
    assert "no watchdog to attach to on port 1" in capsys.readouterr().err


def test_output_that_is_not_a_terminal_is_wide_enough_for_the_tables(monkeypatch):
    from jev_watchdog.cli import SERVICE_WIDTH, _console

    monkeypatch.setattr("sys.stdout.isatty", lambda: False)
    assert _console().width == SERVICE_WIDTH
    assert _console(quiet=True).quiet


def test_attach_to_something_that_refuses_state_says_so_without_a_traceback(monkeypatch, capsys):
    from jev_watchdog.daemon.client import Client, Refused

    async def refuses(self):
        raise Refused("HTTP 404")

    monkeypatch.setattr(Client, "state", refuses)
    assert main(["attach", "--port", "1"]) == 1
    assert "no watchdog to attach to on port 1" in capsys.readouterr().err
