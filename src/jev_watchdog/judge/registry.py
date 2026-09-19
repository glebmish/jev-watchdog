"""Name -> judge factory. Adding a backend = one class + one entry here."""

from collections.abc import Callable
from dataclasses import dataclass

from jev_watchdog.judge.base import Judge
from jev_watchdog.judge.fake import FakeJudge


@dataclass(frozen=True)
class JudgeConfig:
    api_key: str | None = None


def _make_jev(config: JudgeConfig) -> Judge:
    from jev_watchdog.judge.jev import JevJudge  # keeps typesafe_sdk out of --judge fake runs

    return JevJudge(api_key=config.api_key)


JUDGES: dict[str, Callable[[JudgeConfig], Judge]] = {
    "jev": _make_jev,
    "fake": lambda config: FakeJudge(),
}


def make_judge(name: str, config: JudgeConfig) -> Judge:
    try:
        factory = JUDGES[name]
    except KeyError:
        raise ValueError(f"unknown judge {name!r}; available: {sorted(JUDGES)}") from None
    return factory(config)
