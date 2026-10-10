import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from agent.application.manager import TaskManager
from agent.application.worker import Worker
from agent.domain.models import Task, TaskStatus
from agent.infra.sqlite_memory import SqliteMemory
from agent.observability.log_setup import current_task_id

URL = "wss://desktop.test/websockify"
PASSWORD_FIXTURE = "redaction-fixture-value"


class SlowWorker:
    def __init__(self, delay: float = 5.0) -> None:
        self.delay = delay
        self.closed = False
        self.seen_task_id: str | None = None

    async def run(self) -> tuple[TaskStatus, str]:
        self.seen_task_id = current_task_id.get()
        await asyncio.sleep(self.delay)
        return TaskStatus.terminada, "completada"

    def partial_summary(self, reason: str) -> str:
        return reason

    async def close(self) -> None:
        self.closed = True


async def wait_for(manager: TaskManager, task_id: str, expected: TaskStatus) -> Task:
    for _ in range(200):
        task = await manager.get(task_id)
        if task is not None and task.estado is expected:
            return task
        await asyncio.sleep(0.01)
    raise AssertionError(f"la tarea {task_id} no llegó a {expected}")


def make_manager(tmp_path: Path, delay: float = 5.0, time_limit: float = 10.0) -> tuple[TaskManager, list[Any]]:
    calls: list[tuple[str, str, str, str]] = []
    workers: list[SlowWorker] = []

    def factory(task_id: str, instruction: str, url: str, password: str) -> Worker:
        calls.append((task_id, instruction, url, password))
        worker = SlowWorker(delay)
        workers.append(worker)
        return worker

    manager = TaskManager(SqliteMemory(tmp_path / "agent.db"), factory, time_limit, 2)
    return manager, [calls, workers]


@pytest.mark.asyncio
async def test_factory_receives_the_task_id(tmp_path: Path) -> None:
    manager, (calls, _workers) = make_manager(tmp_path, delay=0.01)
    task_id = manager.create("probar", URL, PASSWORD_FIXTURE)
    await wait_for(manager, task_id, TaskStatus.terminada)
    assert calls == [(task_id, "probar", URL, PASSWORD_FIXTURE)]


@pytest.mark.asyncio
async def test_task_id_is_bound_to_the_log_context(tmp_path: Path) -> None:
    manager, (_calls, workers) = make_manager(tmp_path, delay=0.01)
    task_id = manager.create("probar", URL, PASSWORD_FIXTURE)
    await wait_for(manager, task_id, TaskStatus.terminada)
    assert workers[0].seen_task_id == task_id


@pytest.mark.asyncio
async def test_cancel_running_task_sets_cancelada(tmp_path: Path) -> None:
    manager, (_calls, workers) = make_manager(tmp_path)
    task_id = manager.create("larga", URL, PASSWORD_FIXTURE)
    await wait_for(manager, task_id, TaskStatus.en_curso)
    for _ in range(200):
        if workers:
            break
        await asyncio.sleep(0.01)
    assert manager.running_count == 1
    assert await manager.cancel(task_id) is True
    task = await wait_for(manager, task_id, TaskStatus.cancelada)
    assert task.resumen == "cancelada por el usuario"
    assert workers[0].closed
    stored = await SqliteMemory(tmp_path / "agent.db").load_task(task_id)
    assert stored is not None and stored.estado is TaskStatus.cancelada
    assert manager.running_count == 0


@pytest.mark.asyncio
async def test_cancel_unknown_or_finished_task_returns_false(tmp_path: Path) -> None:
    manager, _ = make_manager(tmp_path, delay=0.01)
    assert await manager.cancel("inexistente") is False
    task_id = manager.create("corta", URL, PASSWORD_FIXTURE)
    await wait_for(manager, task_id, TaskStatus.terminada)
    assert await manager.cancel(task_id) is False
    task = await manager.get(task_id)
    assert task is not None and task.estado is TaskStatus.terminada


@pytest.mark.asyncio
async def test_replacement_still_reports_reemplazada(tmp_path: Path) -> None:
    manager, _ = make_manager(tmp_path)
    first = manager.create("primera", URL, PASSWORD_FIXTURE)
    await wait_for(manager, first, TaskStatus.en_curso)
    second = manager.create("segunda", URL, PASSWORD_FIXTURE)
    task = await wait_for(manager, first, TaskStatus.fallida)
    assert task.resumen == "reemplazada"
    await manager.cancel(second)
    await manager.stop()


@pytest.mark.asyncio
async def test_list_tasks_merges_memory_and_database_newest_first(tmp_path: Path) -> None:
    memory = SqliteMemory(tmp_path / "agent.db")
    old = Task(
        id="old", instruction="antigua", desktop_url=URL, estado=TaskStatus.terminada,
        resumen="ok", created_at=datetime.now(timezone.utc) - timedelta(hours=1),
    )
    await memory.save_task(old)
    manager, _ = make_manager(tmp_path, delay=0.01)
    task_id = manager.create("nueva", URL, PASSWORD_FIXTURE)
    await wait_for(manager, task_id, TaskStatus.terminada)
    tasks = await manager.list_tasks()
    assert [t.id for t in tasks] == [task_id, "old"]
    assert tasks[0].estado is TaskStatus.terminada
    assert [t.id for t in await manager.list_tasks(limit=1)] == [task_id]


@pytest.mark.asyncio
async def test_time_limit_can_be_read_per_task(tmp_path: Path) -> None:
    limit = {"value": 10.0}
    manager = TaskManager(
        SqliteMemory(tmp_path / "agent.db"),
        lambda _id, _i, _u, _p: SlowWorker(0.2),
        lambda: limit["value"],
        2,
    )
    limit["value"] = 0.01
    task_id = manager.create("lenta", URL, PASSWORD_FIXTURE)
    task = await wait_for(manager, task_id, TaskStatus.fallida)
    assert task.resumen == "tiempo agotado"
