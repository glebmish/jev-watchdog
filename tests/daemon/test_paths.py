import stat
from pathlib import Path

from jev_watchdog.daemon.paths import private_dir, socket_path, state_dir


def test_state_dir_follows_xdg_and_defaults_to_local_state():
    assert state_dir({"XDG_STATE_HOME": "/x"}) == Path("/x/jev-watchdog")
    assert state_dir({"XDG_STATE_HOME": "relative"}) == Path.home() / ".local/state/jev-watchdog"
    assert state_dir({}) == Path.home() / ".local/state/jev-watchdog"


def test_each_port_has_its_own_socket():
    assert socket_path(8787, {"XDG_STATE_HOME": "/x"}) == Path("/x/jev-watchdog/attach-8787.sock")


def test_private_dir_is_made_with_parents_and_closed_to_others(tmp_path):
    made = private_dir(tmp_path / "a" / "b")
    assert made.is_dir() and stat.S_IMODE(made.stat().st_mode) == 0o700
    made.chmod(0o755)
    assert stat.S_IMODE(private_dir(made).stat().st_mode) == 0o700
