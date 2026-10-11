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
    assert mount_ui(app, context, enabled=False) is False
    with TestClient(app) as client:
        assert client.get("/ui").status_code == 404
        assert client.get("/ui/").status_code == 404


def test_ui_is_open_without_login_and_api_keeps_team_token(tmp_path: Path) -> None:
    pytest.importorskip("gradio")
    app, context = build(tmp_path)
    assert mount_ui(app, context, enabled=True) is True
    with TestClient(app) as client:
        assert client.get("/ui/", follow_redirects=True).status_code == 200
        config = client.get("/ui/config")
        assert config.status_code == 200
        labels = {c.get("props", {}).get("label") for c in config.json()["components"]}
        assert {"Ejecutar", "Tareas", "Registros", "Configuración", "Pruebas", "Memoria"} <= labels
        # Opening the panel never opens the task API.
        assert client.get("/tasks/any-id").status_code == 401
        assert client.get("/tasks/any-id", headers={"Authorization": f"Bearer {TEAM_TOKEN}"}).status_code == 404


def test_event_file_urls_keep_the_mount_path_behind_a_proxy(tmp_path: Path) -> None:
    """Event outputs (gallery screenshots) resolve their URL from the Gradio app's root_path only.

    Behind Caddy (X-Forwarded-Host) an empty root_path drops /ui and every screenshot 404s.
    """
    pytest.importorskip("gradio")
    from gradio import route_utils
    from starlette.requests import Request

    app, context = build(tmp_path)
    mount_ui(app, context, enabled=True)
    gradio_app = next(route.app for route in app.routes if getattr(route, "path", None) == "/ui")
    request = Request({
        "type": "http", "method": "POST", "scheme": "http", "server": ("agent", 8000),
        "path": "/ui/gradio_api/queue/join", "root_path": "", "query_string": b"",
        "headers": [(b"host", b"agent:8000"), (b"x-forwarded-host", b"agent.example.com"),
                    (b"x-forwarded-proto", b"https")],
    })
    root = route_utils.get_root_url(request, route_path="/gradio_api/queue/join", root_path=gradio_app.root_path)
    assert root == "https://agent.example.com/ui"
