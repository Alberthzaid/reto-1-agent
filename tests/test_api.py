import asyncio
import time
from collections.abc import Callable
from pathlib import Path

from fastapi.testclient import TestClient

from agent.api.app import create_app
from agent.application.manager import TaskManager
from agent.application.worker import Worker
from agent.domain.models import TaskStatus
from agent.infra.sqlite_memory import SqliteMemory

TEAM_TOKEN = "team-test-token"
HEADERS = {"Authorization": f"Bearer {TEAM_TOKEN}"}
PASSWORD_FIXTURE = "redaction-fixture-value"
DESKTOP = {"url": "wss://desktop.test/websockify", "password": PASSWORD_FIXTURE}


class TimedWorker:
    def __init__(self, delay: float) -> None:
        self.delay = delay
        self.closed = False

    async def run(self) -> tuple[TaskStatus, str]:
        await asyncio.sleep(self.delay)
        return TaskStatus.terminada, "completada"

    def partial_summary(self, reason: str) -> str:
        return reason

    async def close(self) -> None:
        self.closed = True


def make_client(tmp_path: Path, delay: float = 0.1, time_limit: float = 1.0) -> TestClient:
    factory: Callable[[str, str, str, str], Worker] = (
        lambda _task_id, _instruction, _url, _password: TimedWorker(delay)
    )
    manager = TaskManager(SqliteMemory(tmp_path / "tasks.db"), factory, time_limit, 2)
    return TestClient(create_app(manager, TEAM_TOKEN))


def wait_for_status(client: TestClient, task_id: str, expected: str) -> dict[str, str]:
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        response = client.get(f"/tasks/{task_id}", headers=HEADERS)
        if response.status_code == 200 and response.json()["estado"] == expected:
            return response.json()
        time.sleep(0.01)
    raise AssertionError(f"la tarea {task_id} no llegó a {expected}")


def test_auth_is_required(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        assert client.get("/health").status_code == 401
        assert client.post("/tasks", json={"tarea": "x", "escritorio": DESKTOP}).status_code == 401


def test_post_returns_immediately_and_task_finishes(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        started = time.monotonic()
        response = client.post("/tasks", headers=HEADERS, json={"tarea": "probar", "escritorio": DESKTOP})
        assert time.monotonic() - started < 2
        assert response.status_code == 200
        task_id = response.json()["task_id"]
        assert task_id
        running = client.get(f"/tasks/{task_id}", headers=HEADERS)
        assert running.json()["estado"] in {"pendiente", "en_curso"}
        assert wait_for_status(client, task_id, "terminada")["resumen"] == "completada"


def test_second_task_replaces_previous_task_for_same_desktop(tmp_path: Path) -> None:
    with make_client(tmp_path, delay=0.2) as client:
        first = client.post("/tasks", headers=HEADERS, json={"tarea": "primera", "escritorio": DESKTOP}).json()
        time.sleep(0.02)
        second = client.post("/tasks", headers=HEADERS, json={"tarea": "segunda", "escritorio": DESKTOP}).json()
        failed = wait_for_status(client, first["task_id"], "fallida")
        assert failed["resumen"] == "reemplazada"
        assert wait_for_status(client, second["task_id"], "terminada")["resumen"] == "completada"


def test_timeout_fails_task(tmp_path: Path) -> None:
    with make_client(tmp_path, delay=0.2, time_limit=0.01) as client:
        response = client.post("/tasks", headers=HEADERS, json={"tarea": "lenta", "escritorio": DESKTOP})
        result = wait_for_status(client, response.json()["task_id"], "fallida")
        assert result["resumen"] == "tiempo agotado"


def test_unknown_task_is_not_found(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        assert client.get("/tasks/inexistente", headers=HEADERS).status_code == 404
