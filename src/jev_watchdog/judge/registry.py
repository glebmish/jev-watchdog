"""Judge spec -> judge. Adding a backend = one class + one entry here.

A spec is `name` or `name:model`, e.g. `jev`, `claude:claude-haiku-4-5`.
"""

from collections.abc import Callable
from dataclasses import dataclass, replace

from jev_watchdog.judge.base import Judge
from jev_watchdog.judge.fake import FakeJudge


@dataclass(frozen=True)
class JudgeConfig:
    api_key: str | None = None  # jev
    model: str | None = None  # from the spec; None = the backend's default
    thinking: bool = False  # claude


# Backends import their SDK lazily so an unused one costs nothing at startup.
def _make_jev(config: JudgeConfig) -> Judge:
    from jev_watchdog.judge.jev import DEFAULT_MODEL, JevJudge

    return JevJudge(api_key=config.api_key, model=config.model or DEFAULT_MODEL)


def _make_claude(config: JudgeConfig) -> Judge:
    from jev_watchdog.judge.claude_agent import DEFAULT_MODEL, ClaudeAgentJudge

    return ClaudeAgentJudge(model=config.model or DEFAULT_MODEL, thinking=config.thinking)


JUDGES: dict[str, Callable[[JudgeConfig], Judge]] = {
    "jev": _make_jev,
    "claude": _make_claude,
    "fake": lambda config: FakeJudge(name=f"fake:{config.model}" if config.model else "fake"),
}


def backend_of(spec: str) -> str:
    return spec.partition(":")[0]


def make_judge(spec: str, config: JudgeConfig) -> Judge:
    name, _, model = spec.partition(":")
    try:
        factory = JUDGES[name]
    except KeyError:
        raise ValueError(f"unknown judge {name!r}; available: {sorted(JUDGES)}") from None
    return factory(replace(config, model=model or None))
