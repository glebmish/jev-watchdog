from pathlib import Path

import pytest

from jev_watchdog.cli import DEFAULT_PORT, build_parser, resolve_api_key


def test_run_defaults():
    args = build_parser().parse_args(["run"])
    assert args.port == DEFAULT_PORT == 8787
    assert args.judge == "jev"
    assert args.pack == Path("pack.toml")
    assert args.key_file == Path("prototype-throwaway-key")
    assert args.log is None


def test_run_overrides():
    args = build_parser().parse_args(
        ["run", "--port", "9000", "--judge", "fake", "--pack", "p.toml", "--log", "out.jsonl"]
    )
    assert (args.port, args.judge, args.pack, args.log) == (
        9000,
        "fake",
        Path("p.toml"),
        Path("out.jsonl"),
    )


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
