from collections.abc import Iterator
from typing import Any

import pytest

from agent.application.worker import AgentWorker, Worker
from agent.config import Settings
from agent.domain.budget import task_deadline
from agent.domain.models import TaskStatus, TextBox
from agent.observability import log_setup
from tests.fakes import FakeClock, FakeModel, FakeReader, FakeScreen, FakeSettler, FakeTrace, tool_decision

PASSWORD = "redaction-fixture-value"
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
          trace: FakeTrace | None = None) -> tuple[AgentWorker, FakeScreen, FakeModel, FakeModel]:
    screen = screen or FakeScreen()
    clock = FakeClock()
    fast_model = FakeModel(fast)
    strong_model = FakeModel(strong)
    worker = AgentWorker(
        INSTRUCTION,
        URL,
        PASSWORD,
        Settings(time_limit_sec=255),
        fast_model,
        strong_model,
        FakeReader([HOY, GUARDAR]),
        lambda: screen,
        clock=clock,
        sleep=clock.sleep,
        settler_factory=lambda s: FakeSettler(s),  # type: ignore[arg-type]
        trace_factory=(lambda: trace) if trace is not None else None,
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
    assert screen.connected == (URL, PASSWORD)
    assert len(strong.requests) == 1
    assert trace.steps
    assert PASSWORD not in summary and URL not in summary


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
        tool_decision(("finish", {"estado": "fallida", "resumen": f"clave {PASSWORD} en {URL}"})),
    ]
    worker, _, _, _ = build(fast, [])
    _, summary = await worker.run()
    assert PASSWORD not in summary
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
