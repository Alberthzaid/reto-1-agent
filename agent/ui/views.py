"""Pure, Gradio-free helpers for the control panel: every text shown to the user passes through `redact`."""

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from agent.application.manager import TaskManager
from agent.application.runtime import RuntimeConfig, RuntimeValues
from agent.domain.models import Procedure, Task, TaskStatus
from agent.observability.log_setup import MemoryLogHandler, redact
from agent.observability.traces import TraceStep

TASK_HEADERS = ["id", "estado", "instrucción", "fecha"]
STEP_HEADERS = ["paso", "acción", "nota"]
PROCEDURE_HEADERS = ["app", "intención", "éxitos", "pasos"]
RESULT_HEADERS = ["id", "pasa", "fallos", "estado", "segundos"]
HISTORY_HEADERS = ["reporte", "aciertos", "falsos éxitos", "tiempo medio (s)"]
LOG_LEVELS = ["TODOS", "DEBUG", "INFO", "WARNING", "ERROR"]
PROMPT_NAMES = ("orient", "plan", "system", "verify")
MAX_INSTRUCTION_CHARS = 80
MAX_NOTE_CHARS = 300
ACTIVE_STATES = frozenset({TaskStatus.pendiente, TaskStatus.en_curso})


class ProcedureStore(Protocol):
    def has_procedures_table(self) -> bool: ...

    async def list_procedures(self, limit: int = 200) -> list[Procedure]: ...

    async def delete_procedure(self, app: str, intent: str, steps: Sequence[str]) -> bool: ...


@dataclass(frozen=True)
class PanelContext:
    """Everything the panel needs. The desktop password stays server-side and is never rendered."""

    manager: TaskManager
    runtime: RuntimeConfig
    logs: MemoryLogHandler
    data_dir: Path
    uses_llm: bool
    desktop_url: str | None
    desktop_password: str | None = field(default=None, repr=False)
    procedures: ProcedureStore | None = None


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "…"


def task_rows(tasks: Sequence[Task]) -> list[list[str]]:
    """Table rows; the desktop URL is intentionally left out."""
    return [
        [
            task.id,
            task.estado.value,
            _truncate(redact(task.instruction), MAX_INSTRUCTION_CHARS),
            task.created_at.astimezone().strftime("%Y-%m-%d %H:%M:%S"),
        ]
        for task in tasks
    ]


def is_active(task: Task | None) -> bool:
    return task is not None and task.estado in ACTIVE_STATES


def task_detail(task: Task | None) -> str:
    if task is None:
        return "Sin tarea seleccionada."
    summary = redact(task.resumen) or "(sin resumen todavía)"
    return (
        f"**Tarea:** `{task.id}`  \n**Estado:** {task.estado.value}\n\n"
        f"**Instrucción:** {redact(task.instruction)}\n\n**Resumen:**\n\n```text\n{summary}\n```"
    )


def _value(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def format_action(action: Mapping[str, Any]) -> str:
    """`tool(k=v, ...)` for ToolBox actions, JSON for anything else; always redacted."""
    tool = action.get("tool")
    if not isinstance(tool, str):
        return redact(_value(dict(action)))
    params = action.get("input") or {}
    args = ", ".join(f"{key}={_value(item)}" for key, item in params.items()) if isinstance(params, dict) else ""
    prefix = "[error] " if action.get("is_error") else ""
    return redact(f"{prefix}{tool}({args})")


def step_rows(steps: Sequence[TraceStep]) -> list[list[Any]]:
    return [[step.step, format_action(step.action), _truncate(redact(step.note), MAX_NOTE_CHARS)] for step in steps]


def gallery_items(steps: Sequence[TraceStep]) -> list[tuple[str, str]]:
    return [
        (str(step.image), f"Paso {step.step}: {format_action(step.action)}")
        for step in steps
        if step.image is not None
    ]


def model_label(values: RuntimeValues, uses_llm: bool) -> str:
    if not uses_llm:
        return "DummyWorker (sin ANTHROPIC_API_KEY)"
    return f"{values.model_fast} / {values.model_strong}"


def header_markdown(ready: bool, model: str, running: int) -> str:
    state = "listo" if ready else "no listo"
    return f"**Estado del servicio:** {state} · **Modelo:** {redact(model)} · **Tareas en curso:** {running}"


def level_filter(choice: str | None) -> str | None:
    return None if not choice or choice == "TODOS" else choice


def log_text(records: Sequence[Mapping[str, Any]]) -> str:
    lines: list[str] = []
    for record in records:
        task = f" [{record['task_id']}]" if record.get("task_id") else ""
        lines.append(f"{record.get('ts', '')} {record.get('level', ''):<8} {record.get('logger', '')}{task} "
                     f"{record.get('message', '')}")
        if record.get("exc"):
            lines.append(str(record["exc"]))
    return redact("\n".join(lines))


def procedure_rows(procedures: Sequence[Procedure]) -> list[list[Any]]:
    return [
        [redact(p.app), redact(p.intent), p.exitos, redact(" > ".join(p.steps))]
        for p in procedures
    ]


def parse_combos(text: str) -> list[str]:
    return [item.strip() for item in text.replace("\n", ",").split(",") if item.strip()]


def parse_ids(text: str) -> list[str] | None:
    ids = [item.strip().upper() for item in text.replace(" ", ",").split(",") if item.strip()]
    return ids or None


def _prompt_path(prompts_dir: Path, name: str) -> Path:
    if name not in PROMPT_NAMES:
        raise ValueError(f"prompt desconocido: {name}")
    return prompts_dir / f"{name}.md"


def read_prompt(prompts_dir: Path, name: str) -> str:
    return _prompt_path(prompts_dir, name).read_text(encoding="utf-8")


def save_prompt(prompts_dir: Path, name: str, text: str) -> Path:
    """Write the prompt and keep the previous version as `<name>.md.bak`."""
    path = _prompt_path(prompts_dir, name)
    if not text.strip():
        raise ValueError("el prompt no puede quedar vacío")
    if path.is_file():
        path.with_name(path.name + ".bak").write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    path.write_text(text, encoding="utf-8")
    return path


def resolve_desktop(
    url: str, password: str, default_url: str | None, default_password: str | None
) -> tuple[str, str]:
    """Empty fields fall back to `.env`; errors never echo the values."""
    final_url = url.strip() or (default_url or "")
    final_password = password or (default_password or "")
    if not final_url:
        raise ValueError("Falta la URL del escritorio (DESKTOP_URL)")
    if not final_url.startswith(("ws://", "wss://")):
        raise ValueError("La URL del escritorio debe empezar por ws:// o wss://")
    if not final_password:
        raise ValueError("Falta la contraseña del escritorio (DESKTOP_PASSWORD)")
    return final_url, final_password


def result_rows(results: Sequence[Mapping[str, Any]]) -> list[list[str]]:
    rows: list[list[str]] = []
    for result in results:
        failed = ",".join(name for name, ok in result.get("checks", {}).items() if not ok) or "-"
        rows.append([
            redact(str(result.get("id", "?"))),
            "si" if result.get("pass") else "no",
            failed,
            redact(str(result.get("estado", "?"))),
            f"{float(result.get('segundos', 0.0)):.1f}",
        ])
    return rows


def report_history(reports_dir: Path) -> list[list[str]]:
    """Totals of `data/reports/*.json`, newest first; unreadable reports are skipped."""
    if not reports_dir.is_dir():
        return []
    rows: list[list[str]] = []
    for path in sorted(reports_dir.glob("*.json"), reverse=True):
        try:
            totals = json.loads(path.read_text(encoding="utf-8"))["totales"]
            rows.append([
                path.name,
                f"{totals['aciertos']}/{totals['total']}",
                str(totals["falsos_exitos"]),
                f"{float(totals['tiempo_medio']):.1f}",
            ])
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return rows
