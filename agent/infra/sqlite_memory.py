"""SQLite persistence for task state and learned procedures. Credentials are intentionally never stored."""

import asyncio
import hashlib
import json
import logging
import re
import sqlite3
import unicodedata
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path

from agent.domain.models import Procedure, Task, TaskStatus, TextBox

log = logging.getLogger(__name__)

FINGERPRINT_BAND_Y = 90  # header band
FINGERPRINT_BAND_X = 200  # left menu
MIN_WORD_CHARS = 3
TOP_PROCEDURES = 3
# Defense in depth: learned text must never carry identifiers (NIT, phones, amounts) or emails.
_DATA_RE = re.compile(r"\d{3,}|[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


def _plain_words(text: str) -> list[str]:
    decomposed = unicodedata.normalize("NFKD", text)
    plain = "".join(ch for ch in decomposed if not unicodedata.combining(ch)).lower()
    return [word for word in re.findall(r"[a-z]+", plain) if len(word) >= MIN_WORD_CHARS]


class SqliteMemory:
    """Stores the task projection used by the HTTP API and the navigation procedures that worked."""

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
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS procedures (
                    app TEXT NOT NULL,
                    intent TEXT NOT NULL,
                    steps TEXT NOT NULL,
                    aprendizaje TEXT NOT NULL,
                    exitos INTEGER NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (app, intent, steps)
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

    # --- procedures ------------------------------------------------------------------------------

    def fingerprint(self, boxes: list[TextBox]) -> str:
        """Hash of the header/menu words: stable across screens of one app, ignores body, dates and numbers."""
        words = {
            word
            for box in boxes
            if box.y < FINGERPRINT_BAND_Y or box.x < FINGERPRINT_BAND_X
            for word in _plain_words(box.text)
        }
        return hashlib.sha1(" ".join(sorted(words)).encode("utf-8")).hexdigest()[:16]

    async def procedures(self, app: str, intent: str) -> list[Procedure]:
        return await asyncio.to_thread(self._procedures, app, intent)

    def _procedures(self, app: str, intent: str) -> list[Procedure]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT app, intent, steps, aprendizaje, exitos FROM procedures
                WHERE app = ? AND intent = ? AND exitos > 0
                ORDER BY exitos DESC, updated_at DESC
                LIMIT ?
                """,
                (app, intent, TOP_PROCEDURES),
            ).fetchall()
        return [
            Procedure(app=row[0], intent=row[1], steps=tuple(json.loads(row[2])), aprendizaje=row[3], exitos=row[4])
            for row in rows
        ]

    async def save_procedure(self, app: str, procedure: Procedure) -> None:
        await asyncio.to_thread(self._save_procedure, app, procedure)

    def _save_procedure(self, app: str, procedure: Procedure) -> None:
        if any(_DATA_RE.search(text) for text in (*procedure.steps, procedure.aprendizaje)):
            log.warning("procedimiento descartado: contiene datos que no deben guardarse")
            return
        steps = json.dumps(list(procedure.steps), ensure_ascii=False)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO procedures (app, intent, steps, aprendizaje, exitos, updated_at)
                VALUES (?, ?, ?, ?, 1, ?)
                ON CONFLICT(app, intent, steps) DO UPDATE SET
                    exitos = MAX(procedures.exitos, 0) + 1,
                    aprendizaje = excluded.aprendizaje,
                    updated_at = excluded.updated_at
                """,
                (app, procedure.intent, steps, procedure.aprendizaje, _now()),
            )

    async def penalize(self, app: str, intent: str, procedures: Sequence[Procedure]) -> None:
        await asyncio.to_thread(self._penalize, app, intent, procedures)

    def _penalize(self, app: str, intent: str, procedures: Sequence[Procedure]) -> None:
        with self._connect() as connection:
            connection.executemany(
                "UPDATE procedures SET exitos = exitos - 1 WHERE app = ? AND intent = ? AND steps = ?",
                [(app, intent, json.dumps(list(p.steps), ensure_ascii=False)) for p in procedures],
            )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
