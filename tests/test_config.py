import dataclasses

import pytest

from agent.config import ConfigError, Settings


def test_defaults_with_only_secrets() -> None:
    s = Settings.from_env({"ANTHROPIC_API_KEY": "k", "TEAM_TOKEN": "t"})
    assert s.model_fast == "claude-sonnet-5-5"
    assert s.model_strong == "claude-opus-5-5"
    assert s.time_limit_sec == 255 and isinstance(s.time_limit_sec, int)
    assert s.data_dir == "data"
    assert s.tesseract_cmd is None
    assert s.falco_enabled is False
    assert s.falco_base_url == "http://127.0.0.1:8000"
    assert s.max_concurrent_tasks == 8
    assert s.desktop_url is None and s.desktop_password is None


def test_empty_env_does_not_raise() -> None:
    s = Settings.from_env({})
    assert s.anthropic_api_key is None and s.team_token is None


def test_overrides_parse_types() -> None:
    s = Settings.from_env(
        {"TIME_LIMIT_SEC": "100", "FALCO_ENABLED": "true", "MAX_CONCURRENT_TASKS": "2",
         "TESSERACT_CMD": "/usr/bin/tesseract", "LOG_LEVEL": "DEBUG"}
    )
    assert s.time_limit_sec == 100
    assert s.falco_enabled is True
    assert s.max_concurrent_tasks == 2
    assert s.tesseract_cmd == "/usr/bin/tesseract"
    assert s.log_level == "DEBUG"


def test_falco_base_url_can_be_overridden() -> None:
    assert Settings.from_env({"FALCO_BASE_URL": "http://falco.example.test:9000"}).falco_base_url == (
        "http://falco.example.test:9000"
    )


@pytest.mark.parametrize("raw,expected", [("1", True), ("YES", True), ("false", False), ("0", False), ("", False)])
def test_bool_parsing(raw: str, expected: bool) -> None:
    assert Settings.from_env({"FALCO_ENABLED": raw}).falco_enabled is expected


def test_invalid_int_names_variable_only() -> None:
    with pytest.raises(ConfigError) as exc:
        Settings.from_env({"TIME_LIMIT_SEC": "abc"})
    assert "TIME_LIMIT_SEC" in str(exc.value)


def test_frozen() -> None:
    s = Settings.from_env({})
    with pytest.raises(dataclasses.FrozenInstanceError):
        s.data_dir = "x"  # type: ignore[misc]


def test_repr_and_str_mask_secrets() -> None:
    redaction_target = "redaction-fixture-value"
    s = Settings.from_env(
        {"ANTHROPIC_API_KEY": "k-123", "TEAM_TOKEN": "t-456", "DESKTOP_PASSWORD": redaction_target}
    )
    for text in (repr(s), str(s)):
        for secret in ("k-123", "t-456", redaction_target):
            assert secret not in text
        assert "***" in text


def test_require_desktop_returns_pair() -> None:
    s = Settings.from_env({"DESKTOP_URL": "wss://h/websockify", "DESKTOP_PASSWORD": "pw"})
    assert s.require_desktop() == ("wss://h/websockify", "pw")


@pytest.mark.parametrize(
    "env,missing",
    [({"DESKTOP_PASSWORD": "pw"}, "DESKTOP_URL"), ({"DESKTOP_URL": "wss://h"}, "DESKTOP_PASSWORD")],
)
def test_require_desktop_names_variable_only(env: dict[str, str], missing: str) -> None:
    s = Settings.from_env(env)
    with pytest.raises(ConfigError) as exc:
        s.require_desktop()
    assert missing in str(exc.value)
    assert "pw" not in str(exc.value).lower().replace("desktop_password", "")


def test_base_url_defaults_to_none() -> None:
    assert Settings.from_env({}).anthropic_base_url is None


def test_openrouter_base_url_maps_default_models_to_openrouter_ids() -> None:
    s = Settings.from_env({"ANTHROPIC_BASE_URL": "https://openrouter.ai/api"})
    assert s.anthropic_base_url == "https://openrouter.ai/api"
    assert s.model_fast == "anthropic/claude-sonnet-5.5"
    assert s.model_strong == "anthropic/claude-opus-5.5"


def test_explicit_models_win_over_openrouter_defaults() -> None:
    s = Settings.from_env(
        {"ANTHROPIC_BASE_URL": "https://openrouter.ai/api", "MODEL_FAST": "anthropic/claude-haiku-5.5"}
    )
    assert s.model_fast == "anthropic/claude-haiku-5.5"
    assert s.model_strong == "anthropic/claude-opus-5.5"


def test_ui_password_falls_back_to_team_token_and_stays_out_of_repr() -> None:
    own = Settings.from_env({"TEAM_TOKEN": "t-456", "UI_PASSWORD": "panel-pass"})
    assert own.ui_password == "panel-pass"
    assert own.panel_password == "panel-pass"
    assert "panel-pass" not in repr(own)
    fallback = Settings.from_env({"TEAM_TOKEN": "t-456"})
    assert fallback.ui_password is None
    assert fallback.panel_password == "t-456"
    assert Settings.from_env({}).panel_password is None


@pytest.mark.parametrize(("raw", "expected"), [(None, False), ("1", True), ("0", False), ("true", True)])
def test_ui_enabled_flag(raw: str | None, expected: bool) -> None:
    env = {} if raw is None else {"UI_ENABLED": raw}
    assert Settings.from_env(env).ui_enabled is expected
