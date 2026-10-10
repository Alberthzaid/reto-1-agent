from dataclasses import fields
from pathlib import Path
from typing import Any

import pytest

from agent.application.runtime import RuntimeConfig, RuntimeValues
from agent.application.worker import DummyWorker
from agent.config import Settings
from agent.infra.falco_classifier import FalcoClassifier
from agent.wiring import build_worker_factory

SECRETS = {"api": "api-key-fixture", "team": "team-token-fixture", "desk": "desktop-pass-fixture"}


def settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "anthropic_api_key": SECRETS["api"],
        "team_token": SECRETS["team"],
        "desktop_password": SECRETS["desk"],
        "model_fast": "fast-1",
        "model_strong": "strong-1",
        "time_limit_sec": 200,
        "falco_enabled": False,
    }
    base.update(overrides)
    return Settings(**base)


def runtime(prompts_dir: Path = Path("prompts")) -> RuntimeConfig:
    return RuntimeConfig.from_settings(settings(), blocked_combos=["F5", "ctrl+w"], prompts_dir=prompts_dir)


def test_initial_values_come_from_settings() -> None:
    values = runtime().current()
    assert values.model_fast == "fast-1" and values.model_strong == "strong-1"
    assert values.time_limit_sec == 200
    assert values.max_turns == 40
    assert values.falco_enabled is False
    assert values.blocked_combos == ("F5", "ctrl+w")
    assert values.prompts_dir == Path("prompts")
    assert values.replay_min_successes == 1


def test_apply_overrides_and_restore_resets() -> None:
    config = runtime()
    updated = config.apply(model_fast="fast-2", max_turns=12, time_limit_sec=90, falco_enabled=True,
                           blocked_combos=["F5", " ", "alt+F4 "])
    assert updated.model_fast == "fast-2" and updated.max_turns == 12 and updated.time_limit_sec == 90
    assert updated.falco_enabled is True
    assert updated.blocked_combos == ("F5", "alt+F4")
    assert config.current() == updated
    restored = config.restore()
    assert restored == config.defaults
    assert restored.model_fast == "fast-1" and restored.max_turns == 40


@pytest.mark.parametrize(
    "changes",
    [{"max_turns": 0}, {"time_limit_sec": -1}, {"model_fast": " "}, {"unknown": 1}, {"replay_min_successes": 0}],
)
def test_apply_rejects_invalid_values_without_changing_state(changes: dict[str, Any]) -> None:
    config = runtime()
    before = config.current()
    with pytest.raises(ValueError):
        config.apply(**changes)
    assert config.current() == before


def test_config_never_holds_secrets() -> None:
    config = runtime()
    names = {f.name for f in fields(RuntimeValues)}
    assert not names & {"anthropic_api_key", "team_token", "desktop_password", "password"}
    text = repr(config.current()) + repr(config)
    assert all(secret not in text for secret in SECRETS.values())


class RecordingWorker:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.args = args
        self.kwargs = kwargs


def test_worker_factory_reads_runtime_per_task(tmp_path: Path) -> None:
    config = runtime(prompts_dir=tmp_path)
    classifier = FalcoClassifier(True, "http://falco.test")
    factory = build_worker_factory(
        settings(data_dir=str(tmp_path)), classifier, None, config, worker_cls=RecordingWorker
    )
    first = factory("task-1", "instr", "wss://d/websockify", "pw")
    assert isinstance(first, RecordingWorker)
    assert first.kwargs["max_turns"] == 40
    assert first.kwargs["replay_min_successes"] == 1
    assert first.kwargs["classifier"] is None
    assert first.args[3].time_limit_sec == 200

    config.apply(model_fast="fast-2", max_turns=7, time_limit_sec=60, falco_enabled=True,
                 blocked_combos=["ctrl+q"], prompts_dir=tmp_path / "otros", replay_min_successes=3)
    second = factory("task-2", "instr", "wss://d/websockify", "pw")
    assert isinstance(second, RecordingWorker)
    assert second.kwargs["max_turns"] == 7
    assert second.kwargs["replay_min_successes"] == 3
    assert second.kwargs["prompts_dir"] == tmp_path / "otros"
    assert second.kwargs["blocked_combos"] == ("ctrl+q",)
    assert second.kwargs["classifier"] is classifier
    assert second.args[3].time_limit_sec == 60
    assert second.args[4]._model == "fast-2"
    trace = second.kwargs["trace_factory"]()
    assert trace is not None
    assert (tmp_path / "traces" / "task-2").is_dir()


def test_worker_factory_without_api_key_uses_dummy_worker() -> None:
    factory = build_worker_factory(
        settings(anthropic_api_key=None), FalcoClassifier(False, "http://falco.test"), None, runtime()
    )
    assert isinstance(factory("task-1", "instr", "wss://d/websockify", "pw"), DummyWorker)
