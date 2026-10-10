import logging
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent.api.app import create_app
from agent.application.manager import TaskManager
from agent.application.runtime import RuntimeConfig
from agent.application.worker import DummyWorker
from agent.config import Settings
from agent.infra.sqlite_memory import SqliteMemory
from agent.observability.log_setup import MemoryLogHandler
from agent.ui.mount import mount_ui
from agent.ui.views import PanelContext

TEAM_TOKEN = "team-test-token"
LOGIN_USER = "admin"
WRONG_LOGIN = "not-the-team-fixture"


def build(tmp_path: Path) -> tuple[FastAPI, PanelContext]:
    memory = SqliteMemory(tmp_path / "agent.db")
    manager = TaskManager(memory, lambda _id, _i, _u, _p: DummyWorker(), 5.0, 2)
    context = PanelContext(
        manager=manager,
        runtime=RuntimeConfig.from_settings(Settings(), blocked_combos=["F5"]),
        logs=MemoryLogHandler(),
        data_dir=tmp_path,
        uses_llm=False,
        desktop_url=None,
        desktop_password=None,
        procedures=memory,
    )
    return create_app(manager, TEAM_TOKEN), context


def test_ui_not_mounted_when_disabled(tmp_path: Path) -> None:
    app, context = build(tmp_path)
    assert mount_ui(app, context, enabled=False, team_token=TEAM_TOKEN) is False
    with TestClient(app) as client:
        assert client.get("/ui").status_code == 404
        assert client.get("/ui/").status_code == 404


def test_ui_refuses_to_mount_without_team_token(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    app, context = build(tmp_path)
    with caplog.at_level(logging.WARNING):
        assert mount_ui(app, context, enabled=True, team_token=None) is False
    assert "TEAM_TOKEN" in caplog.text


def test_ui_mounted_requires_login(tmp_path: Path) -> None:
    pytest.importorskip("gradio")
    app, context = build(tmp_path)
    assert mount_ui(app, context, enabled=True, team_token=TEAM_TOKEN) is True
    with TestClient(app) as client:
        page = client.get("/ui/", follow_redirects=True)
        assert page.status_code == 200
        assert client.get("/ui/config").status_code == 401
        bad = client.post("/ui/login", data={"username": LOGIN_USER, "password": WRONG_LOGIN})
        assert bad.status_code in {400, 401}
        assert client.get("/ui/config").status_code == 401
        good = client.post("/ui/login", data={"username": LOGIN_USER, "password": TEAM_TOKEN})
        assert good.status_code == 200
        config = client.get("/ui/config")
        assert config.status_code == 200
        labels = {c.get("props", {}).get("label") for c in config.json()["components"]}
        assert {"Ejecutar", "Tareas", "Registros", "Configuración", "Pruebas", "Memoria"} <= labels
        assert client.get("/health", headers={"Authorization": f"Bearer {TEAM_TOKEN}"}).status_code == 200
