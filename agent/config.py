"""Immutable, env-driven runtime settings. Secrets are never exposed in repr/str."""

import os
from collections.abc import Mapping
from dataclasses import dataclass, fields

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_SECRET_FIELDS = frozenset({"anthropic_api_key", "team_token", "desktop_password"})


class ConfigError(Exception):
    """Invalid or missing configuration. Messages name the variable, never its value."""


def _parse_int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        raise ConfigError(f"{name} debe ser un entero") from None


def _parse_bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = env.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in _TRUE_VALUES


def _optional(env: Mapping[str, str], name: str) -> str | None:
    value = env.get(name)
    return value if value else None


_OPENROUTER_MODELS = {"fast": "anthropic/claude-sonnet-5.5", "strong": "anthropic/claude-opus-5.5"}


def _is_openrouter(base_url: str | None) -> bool:
    return base_url is not None and "openrouter.ai" in base_url


@dataclass(frozen=True)
class Settings:
    anthropic_api_key: str | None = None
    anthropic_base_url: str | None = None
    team_token: str | None = None
    model_fast: str = "claude-sonnet-5-5"
    model_strong: str = "claude-opus-5-5"
    time_limit_sec: int = 255
    data_dir: str = "data"
    tesseract_cmd: str | None = None
    falco_enabled: bool = False
    falco_base_url: str = "http://127.0.0.1:8000"
    max_concurrent_tasks: int = 8
    desktop_url: str | None = None
    desktop_password: str | None = None
    log_level: str = "INFO"
    ui_enabled: bool = False

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        source: Mapping[str, str] = os.environ if env is None else env
        base_url = _optional(source, "ANTHROPIC_BASE_URL")
        openrouter = _is_openrouter(base_url)
        default_fast = _OPENROUTER_MODELS["fast"] if openrouter else cls.model_fast
        default_strong = _OPENROUTER_MODELS["strong"] if openrouter else cls.model_strong
        return cls(
            anthropic_api_key=_optional(source, "ANTHROPIC_API_KEY"),
            anthropic_base_url=base_url,
            team_token=_optional(source, "TEAM_TOKEN"),
            model_fast=source.get("MODEL_FAST") or default_fast,
            model_strong=source.get("MODEL_STRONG") or default_strong,
            time_limit_sec=_parse_int(source, "TIME_LIMIT_SEC", cls.time_limit_sec),
            data_dir=source.get("DATA_DIR") or cls.data_dir,
            tesseract_cmd=_optional(source, "TESSERACT_CMD"),
            falco_enabled=_parse_bool(source, "FALCO_ENABLED", cls.falco_enabled),
            falco_base_url=source.get("FALCO_BASE_URL") or cls.falco_base_url,
            max_concurrent_tasks=_parse_int(source, "MAX_CONCURRENT_TASKS", cls.max_concurrent_tasks),
            desktop_url=_optional(source, "DESKTOP_URL"),
            desktop_password=_optional(source, "DESKTOP_PASSWORD"),
            log_level=source.get("LOG_LEVEL") or cls.log_level,
            ui_enabled=_parse_bool(source, "UI_ENABLED", cls.ui_enabled),
        )

    def require_desktop(self) -> tuple[str, str]:
        """Return (url, password) or raise ConfigError naming the missing variable."""
        if not self.desktop_url:
            raise ConfigError("Falta la variable DESKTOP_URL")
        if not self.desktop_password:
            raise ConfigError("Falta la variable DESKTOP_PASSWORD")
        return self.desktop_url, self.desktop_password

    def __repr__(self) -> str:
        parts = []
        for f in fields(self):
            value = getattr(self, f.name)
            if f.name in _SECRET_FIELDS and value is not None:
                value = "***"
            parts.append(f"{f.name}={value!r}")
        return f"Settings({', '.join(parts)})"

    __str__ = __repr__
