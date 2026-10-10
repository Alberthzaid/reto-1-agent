import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from agent.application.worker import AgentWorker, Worker
from agent.config import Settings
from agent.domain.budget import task_deadline
from agent.domain.models import Decision, Procedure, TaskStatus, TextBox
from agent.infra.sqlite_memory import SqliteMemory
from agent.observability import log_setup
from PIL import Image

from tests.fakes import (
    FakeClock, FakeModel, FakeReader, FakeScreen, FakeSettler, FakeTrace, RoutedModel, overlaps,
    tool_decision,
)

AUTH_FIXTURE = "redaction-fixture-value"
URL = "wss://desktop.example.test/websockify"
INSTRUCTION = "Registra al cliente Papelería Los Andes, NIT 901234567."
HOY = TextBox(text="Hoy: 14/10/2026", x=1000, y=10, w=150, h=20)
GUARDAR = TextBox(text="Guardar", x=600, y=700, w=80, h=20)
PLAN_INPUT: dict[str, Any] = {
    "kind": "crear", "entity": "cliente", "target": "Papelería Los Andes",
    "values": [{"field": "nombre", "value": "Papelería Los Andes"}], "preconditions": [],
    "planned_commits": 1, "feasible": True, "reason": "", "expected_evidence": "Cliente visible en la lista",
}


@pytest.fixture(autouse=True)
def _clear_registry() -> Iterator[None]:
    log_setup.clear_secrets()
    yield
    log_setup.clear_secrets()


def build(fast: list[Any], strong: list[Any], screen: FakeScreen | None = None,
          trace: FakeTrace | None = None,
          reader: FakeReader | None = None, memory: Any = None,
          fast_model: FakeModel | None = None) -> tuple[AgentWorker, FakeScreen, FakeModel, FakeModel]:
    screen = screen or FakeScreen()
    clock = FakeClock()
    fast_model = fast_model or FakeModel(fast)
    strong_model = FakeModel(strong)
    worker = AgentWorker(
        INSTRUCTION,
        URL,
        AUTH_FIXTURE,
        Settings(time_limit_sec=255),
        fast_model,
        strong_model,
        reader or FakeReader([HOY, GUARDAR]),
        lambda: screen,
        clock=clock,
        sleep=clock.sleep,
        settler_factory=lambda s: FakeSettler(s),  # type: ignore[arg-type]
        trace_factory=(lambda: trace) if trace is not None else None,
        memory=memory,
    )
    return worker, screen, fast_model, strong_model


HAPPY_FAST = [
    tool_decision(("submit_plan", PLAN_INPUT)),
    tool_decision(("click", {"x": 640, "y": 710})),
    tool_decision(("finish", {"estado": "terminada", "resumen": "Cliente creado."})),
]


@pytest.mark.asyncio
async def test_scripted_flow_ends_done_when_verifier_confirms() -> None:
    trace = FakeTrace()
    worker, screen, _, strong = build(
        HAPPY_FAST, [tool_decision(("verdict", {"confirmed": True, "evidence": "Se ve el cliente"}))], trace=trace
    )
    assert isinstance(worker, Worker)
    status, summary = await worker.run()
    assert status is TaskStatus.terminada
    assert "Cliente creado." in summary
    assert "Guardar" in summary
    assert screen.connected == (URL, AUTH_FIXTURE)
    assert len(strong.requests) == 1
    assert trace.steps
    assert AUTH_FIXTURE not in summary and URL not in summary


@pytest.mark.asyncio
async def test_verifier_rejection_turns_result_into_failure() -> None:
    worker, _, _, _ = build(HAPPY_FAST, [tool_decision(("verdict", {"confirmed": False, "evidence": "No aparece"}))])
    status, summary = await worker.run()
    assert status is TaskStatus.fallida
    assert "No aparece" in summary
    assert "Guardar" in summary


@pytest.mark.asyncio
async def test_failed_finish_skips_verifier() -> None:
    fast = [
        tool_decision(("submit_plan", PLAN_INPUT)),
        tool_decision(("finish", {"estado": "fallida", "resumen": "Hay dos clientes iguales.",
                                  "datos_razon": ["52345678", "1019876543"]})),
    ]
    worker, _, _, strong = build(fast, [])
    status, summary = await worker.run()
    assert status is TaskStatus.fallida
    assert "52345678" in summary and "1019876543" in summary
    assert strong.requests == []


@pytest.mark.asyncio
async def test_infeasible_plan_fails_without_writing() -> None:
    fast = [tool_decision(("submit_plan", {**PLAN_INPUT, "feasible": False, "reason": "Pide borrar todos"}))]
    worker, screen, _, _ = build(fast, [])
    status, summary = await worker.run()
    assert status is TaskStatus.fallida
    assert "todos" in summary
    assert screen.write_actions == []


@pytest.mark.asyncio
async def test_no_first_frame_fails() -> None:
    worker, _, fast, _ = build(HAPPY_FAST, [], screen=FakeScreen(change_ts=0.0))
    status, summary = await worker.run()
    assert status is TaskStatus.fallida
    assert summary
    assert fast.requests == []


@pytest.mark.asyncio
async def test_secrets_never_reach_the_summary() -> None:
    fast = [
        tool_decision(("submit_plan", PLAN_INPUT)),
        tool_decision(("finish", {"estado": "fallida", "resumen": f"clave {AUTH_FIXTURE} en {URL}"})),
    ]
    worker, _, _, _ = build(fast, [])
    _, summary = await worker.run()
    assert AUTH_FIXTURE not in summary
    assert "desktop.example.test" not in summary


@pytest.mark.asyncio
async def test_partial_summary_lists_commits() -> None:
    fast = [tool_decision(("submit_plan", PLAN_INPUT)), tool_decision(("click", {"x": 640, "y": 710})), RuntimeError()]
    worker, _, _, _ = build(fast, [])
    await worker.run()
    partial = worker.partial_summary("tiempo agotado")
    assert "tiempo agotado" in partial and "Guardar" in partial


def test_partial_summary_before_run() -> None:
    worker, _, _, _ = build([], [])
    assert "tiempo agotado" in worker.partial_summary("tiempo agotado")


@pytest.mark.asyncio
async def test_close_is_idempotent() -> None:
    worker, screen, _, _ = build(HAPPY_FAST, [tool_decision(("verdict", {"confirmed": True, "evidence": "ok"}))])
    await worker.run()
    await worker.close()
    await worker.close()
    assert screen.close_calls == 1


@pytest.mark.asyncio
async def test_close_without_run_is_safe() -> None:
    worker, screen, _, _ = build([], [])
    await worker.close()
    assert screen.close_calls == 0


@pytest.mark.asyncio
async def test_run_sets_task_deadline_for_adapters() -> None:
    seen: list[float | None] = []

    class SpyModel(FakeModel):
        async def decide(self, system: str, messages: list[Any], tools: list[Any]) -> Any:
            seen.append(task_deadline.get())
            return await super().decide(system, messages, tools)

    worker, _, _, _ = build([], [])
    worker._model_fast = SpyModel([tool_decision(("submit_plan", {**PLAN_INPUT, "feasible": False}))])
    await worker.run()
    assert seen and seen[0] is not None


TOAST_RECT = (1057, 744, 204, 37)
GARBLED_GUARDAR = TextBox(text="h", x=636, y=705, w=8, h=10)


class ToastScreen(FakeScreen):
    """Clicking 'Guardar' shows a toast in the next frame only; later frames no longer show it."""

    def __init__(self) -> None:
        super().__init__()
        self._toast_frames = 0

    async def click(self, x: int, y: int, button: str = "left", double: bool = False) -> None:
        await super().click(x, y, button, double)
        if overlaps((x, y, 1, 1), (GUARDAR.x, GUARDAR.y, GUARDAR.w, GUARDAR.h)):
            self._toast_frames = 1

    async def screenshot(self) -> Image.Image:
        frame = self.image.copy()
        if self._toast_frames:
            self._toast_frames -= 1
            x, y, w, h = TOAST_RECT
            frame.paste((20, 30, 120), (x, y, x + w, y + h))
        return frame


def _sigecom_reader() -> FakeReader:
    def region_fn(img: Image.Image, region: tuple[int, int, int, int]) -> list[TextBox]:
        if overlaps(region, TOAST_RECT) and img.getpixel((1100, 760)) == (20, 30, 120):
            return [TextBox(text="Cliente CL-0007 guardado", x=1065, y=752, w=180, h=20)]
        if overlaps(region, (GUARDAR.x, GUARDAR.y, GUARDAR.w, GUARDAR.h)):
            return [GUARDAR]
        return []

    return FakeReader([HOY, GARBLED_GUARDAR], region_fn=region_fn)


@pytest.mark.asyncio
async def test_toast_seen_only_after_commit_reaches_the_verifier() -> None:
    worker, _, _, strong = build(
        HAPPY_FAST,
        [tool_decision(("verdict", {"confirmed": True, "evidence": "Toast Cliente CL-0007 guardado"}))],
        screen=ToastScreen(),
        reader=_sigecom_reader(),
    )
    status, summary = await worker.run()
    assert status is TaskStatus.terminada
    assert "Acciones confirmadas: Guardar" in summary
    assert "| h" not in summary and "Exportar" not in summary
    content = strong.requests[0]["messages"][0]["content"]
    assert len([block for block in content if block["type"] == "image"]) == 2
    texts = " ".join(block["text"] for block in content if block["type"] == "text")
    assert "Cliente CL-0007 guardado" in texts


@pytest.mark.asyncio
async def test_no_evidence_and_no_confirmation_still_fails() -> None:
    no_commit = [HAPPY_FAST[0], HAPPY_FAST[2]]
    worker, _, _, strong = build(
        no_commit, [tool_decision(("verdict", {"confirmed": False, "evidence": "No se ve el cliente"}))]
    )
    status, summary = await worker.run()
    assert status is TaskStatus.fallida
    assert "No se ve el cliente" in summary
    content = strong.requests[0]["messages"][0]["content"]
    assert len([block for block in content if block["type"] == "image"]) == 1


# --- procedure memory ----------------------------------------------------------------------------

INTENT = "crear:cliente"
CONFIRM = tool_decision(("verdict", {"confirmed": True, "evidence": "Se ve el cliente"}))
REJECT = tool_decision(("verdict", {"confirmed": False, "evidence": "No aparece"}))
CLIENTES = TextBox(text="Clientes", x=20, y=120, w=100, h=20)
CLIENTE_ROW = TextBox(text="Papelería Los Andes", x=400, y=400, w=200, h=20)


def dump_procedures(path: Path) -> str:
    with sqlite3.connect(path) as connection:
        return repr(connection.execute("SELECT * FROM procedures").fetchall())


@pytest.mark.asyncio
async def test_verified_success_saves_the_procedure(tmp_path: Path) -> None:
    memory = SqliteMemory(tmp_path / "agent.db")
    worker, _, _, _ = build(HAPPY_FAST, [CONFIRM], memory=memory)
    status, _ = await worker.run()
    assert status is TaskStatus.terminada
    procs = await memory.procedures(memory.fingerprint([HOY, GUARDAR]), INTENT)
    assert [(p.steps, p.exitos) for p in procs] == [(("click 'Guardar'",), 1)]


@pytest.mark.asyncio
async def test_verifier_rejection_saves_nothing(tmp_path: Path) -> None:
    memory = SqliteMemory(tmp_path / "agent.db")
    worker, _, _, _ = build(HAPPY_FAST, [REJECT], memory=memory)
    status, _ = await worker.run()
    assert status is TaskStatus.fallida
    assert dump_procedures(tmp_path / "agent.db") == "[]"


@pytest.mark.asyncio
async def test_failure_with_hints_penalizes_them(tmp_path: Path) -> None:
    memory = SqliteMemory(tmp_path / "agent.db")
    app = memory.fingerprint([HOY, GUARDAR])
    await memory.save_procedure(app, Procedure(app, INTENT, ("click 'Guardar'",), "Guardar abajo."))
    fast = [
        tool_decision(("submit_plan", PLAN_INPUT)),
        tool_decision(("finish", {"estado": "fallida", "resumen": "No se pudo."})),
    ]
    worker, _, fast_model, _ = build(fast, [], memory=memory)
    status, _ = await worker.run()
    assert status is TaskStatus.fallida
    assert "Hints from previous tasks" in str(fast_model.requests[1]["messages"][0]["content"])
    assert await memory.procedures(app, INTENT) == []


@pytest.mark.asyncio
async def test_verifier_rejection_with_hints_penalizes_them(tmp_path: Path) -> None:
    memory = SqliteMemory(tmp_path / "agent.db")
    app = memory.fingerprint([HOY, GUARDAR])
    await memory.save_procedure(app, Procedure(app, INTENT, ("click 'Guardar'",)))
    worker, _, _, _ = build(HAPPY_FAST, [REJECT], memory=memory)
    await worker.run()
    assert await memory.procedures(app, INTENT) == []


class BrokenMemory:
    def fingerprint(self, boxes: list[TextBox]) -> str:
        raise RuntimeError("disk on fire")

    async def procedures(self, app: str, intent: str) -> list[Procedure]:
        raise RuntimeError("disk on fire")

    async def save_procedure(self, app: str, procedure: Procedure) -> None:
        raise RuntimeError("disk on fire")

    async def penalize(self, app: str, intent: str, procedures: Any) -> None:
        raise RuntimeError("disk on fire")


@pytest.mark.asyncio
async def test_memory_errors_never_change_the_result() -> None:
    worker, _, _, _ = build(HAPPY_FAST, [CONFIRM], memory=BrokenMemory())
    status, summary = await worker.run()
    assert status is TaskStatus.terminada and "Cliente creado." in summary


class FailingProcedures(BrokenMemory):
    def fingerprint(self, boxes: list[TextBox]) -> str:
        return "app"


@pytest.mark.asyncio
async def test_memory_errors_after_fingerprint_never_change_the_result() -> None:
    worker, _, _, _ = build(HAPPY_FAST, [CONFIRM], memory=FailingProcedures())
    status, _ = await worker.run()
    assert status is TaskStatus.terminada


@pytest.mark.asyncio
async def test_stored_procedures_never_contain_customer_data_or_secrets(tmp_path: Path) -> None:
    db = tmp_path / "agent.db"
    memory = SqliteMemory(db)
    plan = {**PLAN_INPUT, "values": [{"field": "nombre", "value": "Papelería Los Andes"},
                                     {"field": "nit", "value": "901234567"}]}
    fast = [
        tool_decision(("submit_plan", plan)),
        tool_decision(("click", {"x": 60, "y": 130})),
        tool_decision(("click", {"x": 500, "y": 410})),
        tool_decision(("fill_form", {"fields": [{"x": 400, "y": 300, "value": "Papelería Los Andes"},
                                                {"x": 400, "y": 340, "value": "901234567"}]})),
        tool_decision(("click", {"x": 640, "y": 710})),
        tool_decision(("finish", {
            "estado": "terminada", "resumen": "Cliente creado.",
            "aprendizaje": f"Menú Clientes, escribí Papelería Los Andes (papeleria) NIT 901234567 y Guardar. "
                           f"Clave {AUTH_FIXTURE} en {URL} (desktop.example.test).",
        })),
    ]
    reader = FakeReader([HOY, CLIENTES, CLIENTE_ROW, GUARDAR])
    worker, _, _, _ = build(fast, [CONFIRM], reader=reader, memory=memory)
    status, _ = await worker.run()
    assert status is TaskStatus.terminada
    dump = dump_procedures(db)
    assert "Clientes" in dump and "Guardar" in dump
    for leaked in ("Papelería", "Papeleria", "papeleria", "901234567", AUTH_FIXTURE, URL, "desktop.example.test"):
        assert leaked not in dump


def _hint_aware_route(system: str, messages: list[Any], tools: list[Any]) -> Decision:
    """Plans, then walks a long path without hints and a short one when hints are present."""
    names = {tool["name"] for tool in tools}
    if "submit_plan" in names:
        return tool_decision(("submit_plan", PLAN_INPUT))
    turn = len(messages) // 2
    with_hints = "Hints from previous tasks" in str(messages[0]["content"])
    long_path: list[tuple[str, dict[str, Any]]] = [
        ("key", {"combo": "tab"}),
        ("wait", {"seconds": 1}),
        ("click", {"x": 60, "y": 130}),
        ("click", {"x": 640, "y": 710}),
    ]
    path = long_path[2:] if with_hints else long_path
    if turn < len(path):
        return tool_decision(path[turn])
    return tool_decision(("finish", {"estado": "terminada", "resumen": "Cliente creado.",
                                     "aprendizaje": "Abrir el menú Clientes y pulsar Guardar."}))


def _execute_turns(model: FakeModel) -> int:
    return sum(1 for request in model.requests if any(tool["name"] == "click" for tool in request["tools"]))


@pytest.mark.asyncio
async def test_second_task_uses_hints_and_needs_fewer_turns(tmp_path: Path) -> None:
    memory = SqliteMemory(tmp_path / "agent.db")
    reader = FakeReader([HOY, CLIENTES, GUARDAR])
    first_model = RoutedModel(_hint_aware_route)
    first, _, _, _ = build([], [CONFIRM], reader=reader, memory=memory, fast_model=first_model)
    assert (await first.run())[0] is TaskStatus.terminada
    second_model = RoutedModel(_hint_aware_route)
    second, _, _, _ = build([], [CONFIRM], reader=reader, memory=memory, fast_model=second_model)
    assert (await second.run())[0] is TaskStatus.terminada
    intro = str(second_model.requests[1]["messages"][0]["content"])
    assert "Hints from previous tasks" in intro and "click 'Clientes'" in intro and "click 'Guardar'" in intro
    assert "Hints from previous tasks" not in str(first_model.requests[1]["messages"][0]["content"])
    assert _execute_turns(second_model) < _execute_turns(first_model)
    procs = await memory.procedures(memory.fingerprint([HOY, CLIENTES, GUARDAR]), INTENT)
    assert procs[0].exitos >= 1
