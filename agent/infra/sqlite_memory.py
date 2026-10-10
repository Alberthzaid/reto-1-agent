"""SQLite persistence for task state and learned procedures. Credentials are intentionally never stored."""

import asyncio
import hashlib
import json
import logging
import re
import sqlite3
import unicodedata
from collections.abc import Sequence
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from agent.domain.models import Procedure, ScriptStep, Task, TaskStatus, TextBox

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
            columns = {row[1] for row in connection.execute("PRAGMA table_info(procedures)")}
            if "app_version" not in columns:
                # Older databases: add the column in place; existing rows keep NULL (version unknown).
                connection.execute("ALTER TABLE procedures ADD COLUMN app_version TEXT")
            if "script" not in columns:
                # Replayable scripts came later: older rows have none and are used as text hints only.
                connection.execute("ALTER TABLE procedures ADD COLUMN script TEXT NOT NULL DEFAULT '[]'")
            if "fallos_replay" not in columns:
                connection.execute("ALTER TABLE procedures ADD COLUMN fallos_replay INTEGER NOT NULL DEFAULT 0")

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
        return _task_from_row(row)

    async def list_tasks(self, limit: int = 50) -> list[Task]:
        return await asyncio.to_thread(self._list_tasks, limit)

    def _list_tasks(self, limit: int) -> list[Task]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT id, instruction, desktop_url, estado, resumen, created_at FROM tasks "
                "ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [_task_from_row(row) for row in rows]

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
        if not words:
            return ""
        return hashlib.sha1(" ".join(sorted(words)).encode("utf-8")).hexdigest()[:16]

    async def procedures(self, app: str, intent: str) -> list[Procedure]:
        return await asyncio.to_thread(self._procedures, app, intent)

    def _procedures(self, app: str, intent: str) -> list[Procedure]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT app, intent, steps, aprendizaje, exitos, app_version, script, fallos_replay FROM procedures
                WHERE app = ? AND intent = ? AND exitos > 0
                ORDER BY exitos DESC, updated_at DESC
                LIMIT ?
                """,
                (app, intent, TOP_PROCEDURES),
            ).fetchall()
        return [_procedure_from_row(row) for row in rows]

    async def save_procedure(self, app: str, procedure: Procedure) -> None:
        await asyncio.to_thread(self._save_procedure, app, procedure)

    def _save_procedure(self, app: str, procedure: Procedure) -> None:
        if any(_DATA_RE.search(text) for text in (*procedure.steps, procedure.aprendizaje)):
            log.warning("procedimiento descartado: contiene datos que no deben guardarse")
            return
        steps = json.dumps(list(procedure.steps), ensure_ascii=False)
        script = procedure.script
        if any(_DATA_RE.search(text) for step in script for text in (step.anchor, step.field_label, step.field)):
            log.warning("guion descartado: contiene datos que no deben guardarse")
            script = ()
        script_json = _script_json(script)
        with self._connect() as connection:
            # A new recording replaces the script; the replay failure count survives only while the script
            # stays the same (a successful replay resets it through record_replay).
            connection.execute(
                """
                INSERT INTO procedures (app, intent, steps, aprendizaje, exitos, updated_at, app_version, script,
                                        fallos_replay)
                VALUES (?, ?, ?, ?, 1, ?, ?, ?, 0)
                ON CONFLICT(app, intent, steps) DO UPDATE SET
                    exitos = CASE WHEN procedures.app_version IS excluded.app_version
                        THEN MAX(procedures.exitos, 0) + 1 ELSE 1 END,
                    aprendizaje = excluded.aprendizaje,
                    updated_at = excluded.updated_at,
                    app_version = excluded.app_version,
                    fallos_replay = CASE WHEN procedures.script = excluded.script
                        THEN procedures.fallos_replay ELSE 0 END,
                    script = excluded.script
                """,
                (app, procedure.intent, steps, procedure.aprendizaje, _now(), procedure.app_version, script_json),
            )

    async def record_replay(self, app: str, intent: str, procedure: Procedure, success: bool) -> None:
        await asyncio.to_thread(self._record_replay, app, intent, procedure, success)

    def _record_replay(self, app: str, intent: str, procedure: Procedure, success: bool) -> None:
        update = "fallos_replay = 0" if success else "fallos_replay = fallos_replay + 1"
        with self._connect() as connection:
            connection.execute(
                f"UPDATE procedures SET {update} WHERE app = ? AND intent = ? AND steps = ?",
                (app, intent, json.dumps(list(procedure.steps), ensure_ascii=False)),
            )

    async def penalize(self, app: str, intent: str, procedures: Sequence[Procedure]) -> None:
        await asyncio.to_thread(self._penalize, app, intent, procedures)

    def _penalize(self, app: str, intent: str, procedures: Sequence[Procedure]) -> None:
        with self._connect() as connection:
            connection.executemany(
                "UPDATE procedures SET exitos = exitos - 1 WHERE app = ? AND intent = ? AND steps = ?",
                [(app, intent, json.dumps(list(p.steps), ensure_ascii=False)) for p in procedures],
            )

    # --- control panel (read-only listing and manual deletion) ------------------------------------

    def has_procedures_table(self) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'procedures'"
            ).fetchone()
        return row is not None

    async def list_procedures(self, limit: int = 200) -> list[Procedure]:
        return await asyncio.to_thread(self._list_procedures, limit)

    def _list_procedures(self, limit: int) -> list[Procedure]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT app, intent, steps, aprendizaje, exitos, app_version, script, fallos_replay FROM procedures "
                "ORDER BY exitos DESC, updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [_procedure_from_row(row) for row in rows]

    async def delete_procedure(self, app: str, intent: str, steps: Sequence[str]) -> bool:
        return await asyncio.to_thread(self._delete_procedure, app, intent, steps)

    def _delete_procedure(self, app: str, intent: str, steps: Sequence[str]) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM procedures WHERE app = ? AND intent = ? AND steps = ?",
                (app, intent, json.dumps(list(steps), ensure_ascii=False)),
            )
            return cursor.rowcount > 0


def _script_json(script: Sequence[ScriptStep]) -> str:
    return json.dumps([asdict(step) for step in script], ensure_ascii=False, sort_keys=True)


def _script_from_json(text: str | None) -> tuple[ScriptStep, ...]:
    """Stored script; an unreadable one is treated as absent (the procedure stays a text hint)."""
    try:
        return tuple(ScriptStep(**item) for item in json.loads(text or "[]"))
    except (TypeError, ValueError):
        log.warning("guion guardado ilegible: se ignora")
        return ()


def _procedure_from_row(row: Sequence[object]) -> Procedure:
    return Procedure(
        app=str(row[0]),
        intent=str(row[1]),
        steps=tuple(json.loads(str(row[2]))),
        aprendizaje=str(row[3]),
        exitos=int(row[4]),  # type: ignore[call-overload]
        app_version=row[5] if row[5] is None else str(row[5]),
        script=_script_from_json(row[6] if row[6] is None else str(row[6])),
        fallos_replay=int(row[7] or 0),  # type: ignore[call-overload]
    )


def _task_from_row(row: Sequence[str]) -> Task:
    return Task(
        id=row[0],
        instruction=row[1],
        desktop_url=row[2],
        estado=TaskStatus(row[3]),
        resumen=row[4],
        created_at=datetime.fromisoformat(row[5]),
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
