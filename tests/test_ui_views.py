import json
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agent.application.runtime import RuntimeValues
from agent.domain.models import Procedure, Task, TaskStatus
from agent.observability import log_setup
from agent.observability.traces import TraceStep
from agent.ui import views

REDACTION_FIXTURE = "redaction-fixture-value"
CREATED = datetime(2026, 10, 10, 12, 30, 5, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _registry() -> Iterator[None]:
    log_setup.clear_secrets()
    log_setup.register_secret(REDACTION_FIXTURE)
    yield
    log_setup.clear_secrets()


def task(**overrides: object) -> Task:
    base: dict[str, object] = {
        "id": "t1", "instruction": f"crear cliente con clave {REDACTION_FIXTURE}", "desktop_url": "wss://d",
        "estado": TaskStatus.en_curso, "resumen": f"resumen {REDACTION_FIXTURE}", "created_at": CREATED,
    }
    base.update(overrides)
    return Task(**base)  # type: ignore[arg-type]


def test_task_rows_are_redacted_and_truncated() -> None:
    long_task = task(id="t2", instruction="x" * 200, estado=TaskStatus.terminada)
    rows = views.task_rows([task(), long_task])
    assert views.TASK_HEADERS == ["id", "estado", "instrucción", "fecha"]
    assert rows[0] == ["t1", "en_curso", "crear cliente con clave ***",
                       CREATED.astimezone().strftime("%Y-%m-%d %H:%M:%S")]
    assert len(rows[1][2]) <= views.MAX_INSTRUCTION_CHARS + 1 and rows[1][2].endswith("…")


def test_task_rows_never_include_desktop_url() -> None:
    rows = views.task_rows([task(desktop_url="wss://privado.example/websockify")])
    assert "privado" not in json.dumps(rows)


def test_task_detail_redacts_summary_and_handles_missing_task() -> None:
    text = views.task_detail(task())
    assert "en_curso" in text and "resumen ***" in text and REDACTION_FIXTURE not in text
    assert views.task_detail(None) == "Sin tarea seleccionada."


def test_is_active() -> None:
    assert views.is_active(task(estado=TaskStatus.pendiente))
    assert views.is_active(task(estado=TaskStatus.en_curso))
    assert not views.is_active(task(estado=TaskStatus.cancelada))
    assert not views.is_active(None)


def test_format_action_and_step_rows() -> None:
    action = {"tool": "type_text", "input": {"text": REDACTION_FIXTURE, "x": 3}, "is_error": True}
    assert views.format_action({"tool": "click", "input": {"x": 640, "y": 710}, "is_error": False}) == \
        "click(x=640, y=710)"
    steps = [TraceStep(step=1, image=Path("001.png"), action=action, note=f"nota {REDACTION_FIXTURE}")]
    rows = views.step_rows(steps)
    assert views.STEP_HEADERS == ["paso", "acción", "nota"]
    assert rows == [[1, '[error] type_text(text="***", x=3)', "nota ***"]]


def test_format_action_falls_back_to_json() -> None:
    assert views.format_action({"otro": 1}) == '{"otro": 1}'


def test_gallery_items_skip_missing_images() -> None:
    steps = [
        TraceStep(step=1, image=Path("001.png"), action={"tool": "key", "input": {"combo": "Enter"}}, note=""),
        TraceStep(step=2, image=None, action={"tool": "key"}, note=""),
    ]
    assert views.gallery_items(steps) == [(str(Path("001.png")), 'Paso 1: key(combo="Enter")')]


def test_header_markdown() -> None:
    text = views.header_markdown(ready=True, model="fast / strong", running=2)
    assert "listo" in text and "fast / strong" in text and "2" in text
    assert "no listo" in views.header_markdown(ready=False, model="m", running=0)


def test_model_label() -> None:
    values = RuntimeValues("fast-1", "strong-1", 200, 40, False, (), Path("p"))
    assert views.model_label(values, uses_llm=True) == "fast-1 / strong-1"
    assert "DummyWorker" in views.model_label(values, uses_llm=False)


def test_log_text_formats_and_redacts() -> None:
    records = [
        {"ts": "2026-10-10 12:00:00", "level": "INFO", "logger": "a", "message": f"pw {REDACTION_FIXTURE}",
         "task_id": "t1"},
        {"ts": "2026-10-10 12:00:01", "level": "ERROR", "logger": "b", "message": "fallo", "task_id": None,
         "exc": "Traceback..."},
    ]
    text = views.log_text(records)
    assert text.splitlines()[0] == "2026-10-10 12:00:00 INFO     a [t1] pw ***"
    assert "ERROR    b fallo" in text and "Traceback..." in text


def test_level_filter() -> None:
    assert views.level_filter("TODOS") is None
    assert views.level_filter("WARNING") == "WARNING"
    assert views.LOG_LEVELS[0] == "TODOS"


def test_procedure_rows_redacted() -> None:
    procs = [Procedure(app="app1", intent="crear:cliente", steps=("abrir Clientes", REDACTION_FIXTURE), exitos=3)]
    assert views.procedure_rows(procs) == [["app1", "crear:cliente", 3, "abrir Clientes > ***"]]


def test_parse_combos_and_ids() -> None:
    assert views.parse_combos("F5, ctrl+w\nalt+F4,,") == ["F5", "ctrl+w", "alt+F4"]
    assert views.parse_ids("e1, E2 ") == ["E1", "E2"]
    assert views.parse_ids(" ") is None


def test_prompt_read_and_save_keeps_backup(tmp_path: Path) -> None:
    (tmp_path / "plan.md").write_text("original", encoding="utf-8")
    assert views.read_prompt(tmp_path, "plan") == "original"
    path = views.save_prompt(tmp_path, "plan", "nuevo")
    assert path == tmp_path / "plan.md"
    assert path.read_text(encoding="utf-8") == "nuevo"
    assert (tmp_path / "plan.md.bak").read_text(encoding="utf-8") == "original"


@pytest.mark.parametrize(("name", "text"), [("../x", "a"), ("otro", "a"), ("plan", "  ")])
def test_save_prompt_rejects_unknown_names_and_empty_text(tmp_path: Path, name: str, text: str) -> None:
    with pytest.raises(ValueError):
        views.save_prompt(tmp_path, name, text)


def test_resolve_desktop_falls_back_to_defaults() -> None:
    assert views.resolve_desktop("", "", "wss://d/websockify", "pw") == ("wss://d/websockify", "pw")
    assert views.resolve_desktop("ws://otro", "x", "wss://d", "pw") == ("ws://otro", "x")
    with pytest.raises(ValueError):
        views.resolve_desktop("", "", None, "pw")
    with pytest.raises(ValueError):
        views.resolve_desktop("http://d", "", None, "pw")
    with pytest.raises(ValueError):
        views.resolve_desktop("wss://d", "", "wss://d", None)


def test_result_rows_and_report_history(tmp_path: Path) -> None:
    results = [{"id": "E1", "pass": False, "checks": {"check": True, "estado": False}, "estado": "fallida",
                "segundos": 12.34}]
    assert views.result_rows(results) == [["E1", "no", "estado", "fallida", "12.3"]]
    reports = tmp_path / "reports"
    reports.mkdir()
    (reports / "2026-10-09_100000.json").write_text(
        json.dumps({"resultados": [], "totales": {"total": 5, "aciertos": 4, "falsos_exitos": 0,
                                                   "tiempo_medio": 30.0}}), encoding="utf-8")
    (reports / "2026-10-10_100000.json").write_text(
        json.dumps({"resultados": [], "totales": {"total": 5, "aciertos": 5, "falsos_exitos": 0,
                                                   "tiempo_medio": 20.5}}), encoding="utf-8")
    (reports / "roto.json").write_text("{", encoding="utf-8")
    assert views.report_history(reports) == [
        ["2026-10-10_100000.json", "5/5", "0", "20.5"],
        ["2026-10-09_100000.json", "4/5", "0", "30.0"],
    ]
    assert views.report_history(tmp_path / "missing") == []
