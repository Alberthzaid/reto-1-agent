"""SQLite persistence for task state. Credentials are intentionally never stored."""

import asyncio
import sqlite3
from datetime import datetime
from pathlib import Path

from agent.domain.models import Task, TaskStatus


class SqliteMemory:
    """Stores the task projection used by the HTTP API."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    instruction TEXT NOT NULL,
                    desktop_url TEXT NOT NULL,
                    estado TEXT NOT NULL,
                    resumen TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._path)

    async def save_task(self, task: Task) -> None:
        await asyncio.to_thread(self._save_task, task)

    def _save_task(self, task: Task) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO tasks (id, instruction, desktop_url, estado, resumen, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET estado = excluded.estado, resumen = excluded.resumen
                """,
                (
                    task.id,
                    task.instruction,
                    task.desktop_url,
                    task.estado.value,
                    task.resumen,
                    task.created_at.isoformat(),
                ),
            )

    async def load_task(self, task_id: str) -> Task | None:
        return await asyncio.to_thread(self._load_task, task_id)

    def _load_task(self, task_id: str) -> Task | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT id, instruction, desktop_url, estado, resumen, created_at FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
        if row is None:
            return None
        return Task(
            id=row[0],
            instruction=row[1],
            desktop_url=row[2],
            estado=TaskStatus(row[3]),
            resumen=row[4],
            created_at=datetime.fromisoformat(row[5]),
        )

    async def mark_stale_running_as_failed(self) -> int:
        return await asyncio.to_thread(self._mark_stale_running_as_failed)

    def _mark_stale_running_as_failed(self) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE tasks SET estado = ?, resumen = ? WHERE estado = ?",
                (TaskStatus.fallida.value, "interrumpida al reiniciar", TaskStatus.en_curso.value),
            )
            return cursor.rowcount