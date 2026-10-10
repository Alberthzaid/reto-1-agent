"""Mutable runtime knobs read per task, so the control panel can change them without a restart.

Secrets (API key, team token, desktop password) are deliberately never part of this config.
"""

import threading
from collections.abc import Iterable
from dataclasses import dataclass, fields, replace
from pathlib import Path
from typing import Any

from agent.config import Settings
from agent.domain.policy import DEFAULT_MAX_TURNS, PROMPTS_DIR


@dataclass(frozen=True, slots=True)
class RuntimeValues:
    model_fast: str
    model_strong: str
    time_limit_sec: int
    max_turns: int
    falco_enabled: bool
    blocked_combos: tuple[str, ...]
    prompts_dir: Path
    replay_min_successes: int = 1  # verified successes a learned script needs before it is replayed


_FIELDS = frozenset(f.name for f in fields(RuntimeValues))


def _clean_combos(combos: Iterable[str]) -> tuple[str, ...]:
    return tuple(combo.strip() for combo in combos if combo and combo.strip())


def _validated(values: RuntimeValues) -> RuntimeValues:
    for name in ("model_fast", "model_strong"):
        if not getattr(values, name).strip():
            raise ValueError(f"{name} no puede estar vacío")
    if values.time_limit_sec <= 0:
        raise ValueError("time_limit_sec debe ser mayor que cero")
    if values.max_turns <= 0:
        raise ValueError("max_turns debe ser mayor que cero")
    if values.replay_min_successes <= 0:
        raise ValueError("replay_min_successes debe ser mayor que cero")
    return replace(
        values,
        model_fast=values.model_fast.strip(),
        model_strong=values.model_strong.strip(),
        time_limit_sec=int(values.time_limit_sec),
        max_turns=int(values.max_turns),
        replay_min_successes=int(values.replay_min_successes),
        falco_enabled=bool(values.falco_enabled),
        blocked_combos=_clean_combos(values.blocked_combos),
        prompts_dir=Path(values.prompts_dir),
    )


class RuntimeConfig:
    """Thread-safe holder of the current `RuntimeValues` and the `.env` defaults."""

    def __init__(self, defaults: RuntimeValues) -> None:
        self._defaults = _validated(defaults)
        self._current = self._defaults
        self._lock = threading.Lock()

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        blocked_combos: Iterable[str],
        prompts_dir: Path = PROMPTS_DIR,
        max_turns: int = DEFAULT_MAX_TURNS,
    ) -> "RuntimeConfig":
        return cls(
            RuntimeValues(
                model_fast=settings.model_fast,
                model_strong=settings.model_strong,
                time_limit_sec=settings.time_limit_sec,
                max_turns=max_turns,
                falco_enabled=settings.falco_enabled,
                blocked_combos=tuple(sorted(blocked_combos)),
                prompts_dir=prompts_dir,
            )
        )

    @property
    def defaults(self) -> RuntimeValues:
        return self._defaults

    def current(self) -> RuntimeValues:
        with self._lock:
            return self._current

    def apply(self, **changes: Any) -> RuntimeValues:
        """Replace the given fields for the next task; invalid input leaves the config untouched."""
        unknown = set(changes) - _FIELDS
        if unknown:
            raise ValueError(f"campos desconocidos: {', '.join(sorted(unknown))}")
        if "blocked_combos" in changes:
            changes["blocked_combos"] = tuple(changes["blocked_combos"])
        with self._lock:
            self._current = _validated(replace(self._current, **changes))
            return self._current

    def restore(self) -> RuntimeValues:
        """Go back to the values loaded from the environment."""
        with self._lock:
            self._current = self._defaults
            return self._current

    def __repr__(self) -> str:
        return f"RuntimeConfig(current={self.current()!r})"
