import logging
from typing import Any

import pytest

from agent.domain.guard import Guard
from agent.domain.dates import NO_DATE_NOTE
from agent.domain.models import Plan, Procedure, TaskStatus, TextBox
from agent.domain.policy import Orientation, Policy, describe_orientation
from agent.domain.tools import ToolBox
from tests.fakes import (
    FakeClock,
    FakeModel,
    FakeReader,
    FakeScreen,
    FakeSettler,
    text_decision,
    tool_decision,
)

HOY = TextBox(text="Hoy: 14/10/2026", x=1000, y=10, w=150, h=20)
GUARDAR = TextBox(text="Guardar", x=600, y=700, w=80, h=20)
BUSCAR = TextBox(text="Buscar", x=100, y=100, w=60, h=20)
INSTRUCTION = "Registra al cliente Papelería Los Andes, NIT 901234567."

PLAN_INPUT: dict[str, Any] = {
    "kind": "crear",
    "entity": "cliente",
    "target": "Papelería Los Andes",
    "values": [{"field": "nombre", "value": "Papelería Los Andes"}, {"field": "nit", "value": "901234567"}],
    "preconditions": ["el cliente no existe"],
    "planned_commits": 1,
    "feasible": True,
    "reason": "",
    "expected_evidence": "La lista muestra Papelería Los Andes con NIT 901234567",
}


def make(script: list[Any], boxes: list[TextBox] | None = None, time_budget: float = 200.0,
         cost: float = 0.0, max_turns: int = 40) -> tuple[Policy, FakeModel, FakeScreen, Guard, FakeClock]:
    clock = FakeClock()
    screen = FakeScreen()
    reader = FakeReader(boxes if boxes is not None else [HOY, BUSCAR, GUARDAR])
    guard = Guard({"alt+F4"}, 0)
    deadline = clock.now + time_budget
    toolbox = ToolBox(screen, reader, FakeSettler(screen), guard, clock, deadline=deadline, sleep=clock.sleep)
    toolbox.prime(screen.image, list(reader.boxes))
    model = FakeModel(script, clock=clock, cost=cost)
    policy = Policy(model, toolbox, guard, clock, deadline, max_turns=max_turns)
    return policy, model, screen, guard, clock


async def start(policy: Policy, screen: FakeScreen, boxes: list[TextBox]) -> tuple[Orientation, Plan]:
    orientation = await policy.orient(screen.image, boxes)
    plan = await policy.plan(INSTRUCTION, orientation, screen.image, boxes)
    return orientation, plan


@pytest.mark.asyncio
async def test_orient_reads_today_from_ocr_without_model() -> None:
    policy, model, screen, _, _ = make([])
    orientation = await policy.orient(screen.image, [HOY])
    assert orientation.today == "14/10/2026"
    assert model.requests == []


@pytest.mark.asyncio
async def test_orient_falls_back_to_one_model_call() -> None:
    decision = tool_decision(("report_orientation", {"app": "SIGECOM", "module": "Clientes", "today": "14/10/2026"}))
    policy, model, screen, _, _ = make([decision])
    orientation = await policy.orient(screen.image, [BUSCAR])
    assert orientation == Orientation(today="14/10/2026", app="SIGECOM", module="Clientes")
    assert len(model.requests) == 1


@pytest.mark.asyncio
async def test_orient_survives_model_errors() -> None:
    policy, _, screen, _, _ = make([RuntimeError("boom")])
    assert (await policy.orient(screen.image, [BUSCAR])).today is None


@pytest.mark.asyncio
async def test_plan_maps_tool_input_and_sets_guard_budget() -> None:
    policy, model, screen, guard, _ = make([tool_decision(("submit_plan", PLAN_INPUT))])
    _, plan = await start(policy, screen, [HOY])
    assert plan.feasible and plan.kind == "crear"
    assert plan.values == (("nombre", "Papelería Los Andes"), ("nit", "901234567"))
    assert plan.planned_commits == 1
    assert guard.planned_commits == 1
    assert INSTRUCTION in str(model.requests[0]["messages"])


@pytest.mark.asyncio
async def test_plan_with_too_many_commits_is_infeasible() -> None:
    policy, _, screen, guard, _ = make([tool_decision(("submit_plan", {**PLAN_INPUT, "planned_commits": 4}))])
    _, plan = await start(policy, screen, [HOY])
    assert not plan.feasible
    assert guard.planned_commits == 0


@pytest.mark.asyncio
async def test_plan_without_tool_call_is_nudged_then_infeasible() -> None:
    policy, model, screen, _, _ = make([text_decision("hmm"), text_decision("hmm")])
    _, plan = await start(policy, screen, [HOY])
    assert not plan.feasible
    assert len(model.requests) == 2


@pytest.mark.asyncio
async def test_impossible_plan_ends_failed_without_writing() -> None:
    infeasible = {**PLAN_INPUT, "feasible": False, "planned_commits": 0,
                  "reason": "La instrucción pide borrar todos los clientes"}
    policy, model, screen, _, _ = make([tool_decision(("submit_plan", infeasible))])
    orientation, plan = await start(policy, screen, [HOY])
    result = await policy.run_plan(INSTRUCTION, plan, orientation, screen.image, [HOY])
    assert result.estado is TaskStatus.fallida
    assert "todos" in result.resumen
    assert screen.write_actions == []
    assert len(model.requests) == 1


@pytest.mark.asyncio
async def test_execute_loop_finishes_with_tool_results_and_images() -> None:
    script = [
        tool_decision(("submit_plan", PLAN_INPUT)),
        tool_decision(("click", {"x": 130, "y": 110}), ("zoom", {"x": 0, "y": 0, "w": 100, "h": 50})),
        tool_decision(("click", {"x": 640, "y": 710})),
        tool_decision(("finish", {"estado": "terminada", "resumen": "Cliente creado."})),
    ]
    policy, model, screen, _, _ = make(script)
    orientation, plan = await start(policy, screen, [HOY, BUSCAR, GUARDAR])
    result = await policy.run_plan(INSTRUCTION, plan, orientation, screen.image, [HOY, BUSCAR, GUARDAR])
    assert result.estado is TaskStatus.terminada
    assert result.commits == ("Guardar",)
    first = model.requests[1]["messages"][0]
    assert first["role"] == "user"
    assert any(block["type"] == "image" for block in first["content"])
    assert "Hoy" in str(first["content"]) or "14/10/2026" in str(first["content"])
    # Second execute request: assistant raw then ONE user message with both tool results.
    messages = model.requests[2]["messages"]
    assert messages[1]["role"] == "assistant"
    assert messages[1]["content"][0]["type"] == "thinking"
    results = messages[2]["content"]
    assert messages[2]["role"] == "user"
    assert [block["type"] for block in results] == ["tool_result", "tool_result"]
    for block in results:
        kinds = [part["type"] for part in block["content"]]
        assert kinds == ["text", "image"]
    assert results[0]["tool_use_id"] == "toolu_0_click"


@pytest.mark.asyncio
async def test_history_is_append_only() -> None:
    script = [
        tool_decision(("submit_plan", PLAN_INPUT)),
        tool_decision(("click", {"x": 130, "y": 110})),
        tool_decision(("key", {"combo": "tab"})),
        tool_decision(("wait", {"seconds": 1})),
        tool_decision(("finish", {"estado": "fallida", "resumen": "No se pudo."})),
    ]
    policy, model, screen, _, _ = make(script)
    orientation, plan = await start(policy, screen, [HOY])
    await policy.run_plan(INSTRUCTION, plan, orientation, screen.image, [HOY])
    execute_requests = model.requests[1:]
    for earlier, later in zip(execute_requests, execute_requests[1:]):
        before = earlier["messages"]
        after = later["messages"]
        assert len(after) > len(before)
        assert after[: len(before)] == before
    assert all(request["system"] == execute_requests[0]["system"] for request in execute_requests)
    assert all(request["tools"] is execute_requests[0]["tools"] for request in execute_requests)


@pytest.mark.asyncio
async def test_error_outcomes_are_marked() -> None:
    script = [
        tool_decision(("submit_plan", PLAN_INPUT)),
        tool_decision(("key", {"combo": "alt+F4"})),
        tool_decision(("finish", {"estado": "fallida", "resumen": "x"})),
    ]
    policy, model, screen, _, _ = make(script)
    orientation, plan = await start(policy, screen, [HOY])
    await policy.run_plan(INSTRUCTION, plan, orientation, screen.image, [HOY])
    result_block = model.requests[2]["messages"][2]["content"][0]
    assert result_block["is_error"] is True
    assert screen.write_actions == []


@pytest.mark.asyncio
async def test_loop_stops_when_time_runs_out() -> None:
    script: list[Any] = [tool_decision(("submit_plan", PLAN_INPUT))]
    script += [tool_decision(("click", {"x": 130, "y": 110})) for _ in range(20)]
    policy, model, screen, guard, _ = make(script, time_budget=100.0, cost=15.0)
    orientation, plan = await start(policy, screen, [HOY])
    result = await policy.run_plan(INSTRUCTION, plan, orientation, screen.image, [HOY])
    assert result.estado is TaskStatus.fallida
    assert "tiempo" in result.resumen.lower()
    assert len(model.requests) < 10
    # A finish-now instruction was appended (not edited into earlier turns).
    last = model.requests[-1]["messages"][-1]
    assert any(block.get("type") == "text" and "finish" in block["text"] for block in last["content"])


@pytest.mark.asyncio
async def test_turn_limit_ends_failed() -> None:
    script: list[Any] = [tool_decision(("submit_plan", PLAN_INPUT))]
    script += [tool_decision(("click", {"x": 130, "y": 110})) for _ in range(5)]
    policy, model, screen, _, _ = make(script, max_turns=3)
    orientation, plan = await start(policy, screen, [HOY])
    result = await policy.run_plan(INSTRUCTION, plan, orientation, screen.image, [HOY])
    assert result.estado is TaskStatus.fallida
    assert len(model.requests) == 1 + 3


@pytest.mark.asyncio
async def test_no_tool_call_is_nudged_once_then_fails() -> None:
    script = [tool_decision(("submit_plan", PLAN_INPUT)), text_decision("listo"), text_decision("listo")]
    policy, model, screen, _, _ = make(script)
    orientation, plan = await start(policy, screen, [HOY])
    result = await policy.run_plan(INSTRUCTION, plan, orientation, screen.image, [HOY])
    assert result.estado is TaskStatus.fallida
    nudge = model.requests[2]["messages"][-1]
    assert nudge["role"] == "user" and "finish" in nudge["content"][0]["text"]


@pytest.mark.asyncio
async def test_nudge_recovers_when_model_then_calls_finish() -> None:
    script = [
        tool_decision(("submit_plan", PLAN_INPUT)),
        text_decision("listo"),
        tool_decision(("finish", {"estado": "terminada", "resumen": "ok", "respuesta": ["45"]})),
    ]
    policy, _, screen, _, _ = make(script)
    orientation, plan = await start(policy, screen, [HOY])
    result = await policy.run_plan(INSTRUCTION, plan, orientation, screen.image, [HOY])
    assert result.estado is TaskStatus.terminada
    assert result.respuesta == ("45",)


@pytest.mark.asyncio
async def test_model_error_during_execute_fails_with_commits() -> None:
    script: list[Any] = [
        tool_decision(("submit_plan", PLAN_INPUT)),
        tool_decision(("click", {"x": 640, "y": 710})),
        RuntimeError("api down"),
    ]
    policy, _, screen, _, _ = make(script)
    orientation, plan = await start(policy, screen, [HOY])
    result = await policy.run_plan(INSTRUCTION, plan, orientation, screen.image, [HOY])
    assert result.estado is TaskStatus.fallida
    assert result.commits == ("Guardar",)


@pytest.mark.asyncio
async def test_calls_after_finish_in_same_turn_are_not_executed() -> None:
    script = [
        tool_decision(("submit_plan", PLAN_INPUT)),
        tool_decision(("finish", {"estado": "fallida", "resumen": "x"}), ("click", {"x": 130, "y": 110})),
    ]
    policy, _, screen, _, _ = make(script)
    orientation, plan = await start(policy, screen, [HOY])
    await policy.run_plan(INSTRUCTION, plan, orientation, screen.image, [HOY])
    assert screen.actions == []


def test_describe_orientation_adds_relative_dates_when_today_is_known() -> None:
    text = describe_orientation(Orientation(today="10/10/2026", app="SIGECOM"))
    assert "Yesterday" in text and "2026-10-09" in text and "09/10/2026" in text
    assert NO_DATE_NOTE not in text


def test_describe_orientation_warns_when_today_is_unknown() -> None:
    assert NO_DATE_NOTE in describe_orientation(Orientation())


@pytest.mark.asyncio
async def test_orient_reads_long_header_date_without_model() -> None:
    policy, model, screen, _, _ = make([])
    header = TextBox(text="sábado, 10 de octubre de 2026", x=900, y=12, w=300, h=20)
    orientation = await policy.orient(screen.image, [header, BUSCAR])
    assert orientation.today == "10/10/2026"
    assert model.requests == []


@pytest.mark.asyncio
async def test_orient_model_fallback_normalizes_today() -> None:
    decision = tool_decision(("report_orientation", {"app": "X", "module": "Y", "today": "2026-10-10"}))
    policy, _, screen, _, _ = make([decision])
    assert (await policy.orient(screen.image, [BUSCAR])).today == "10/10/2026"


@pytest.mark.asyncio
async def test_hints_reach_the_first_execute_message(caplog: pytest.LogCaptureFixture) -> None:
    script = [
        tool_decision(("submit_plan", PLAN_INPUT)),
        tool_decision(("click", {"x": 130, "y": 110})),
        tool_decision(("finish", {"estado": "terminada", "resumen": "ok"})),
    ]
    policy, model, screen, _, _ = make(script)
    orientation, plan = await start(policy, screen, [HOY])
    hint = Procedure(app="a", intent="crear:cliente", steps=("click 'Clientes'", "click 'Nuevo'"),
                     aprendizaje="Nuevo queda arriba.", exitos=1)
    with caplog.at_level(logging.INFO, logger="agent.domain.policy"):
        await policy.run_plan(INSTRUCTION, plan, orientation, screen.image, [HOY], hints=[hint])
    first = str(model.requests[1]["messages"][0]["content"])
    assert "Hints from previous tasks" in first and "click 'Nuevo'" in first and "Nuevo queda arriba." in first
    assert "tarea terminada en 2 pasos" in caplog.text


@pytest.mark.asyncio
async def test_no_hints_block_without_hints() -> None:
    script = [tool_decision(("submit_plan", PLAN_INPUT)), tool_decision(("finish", {"estado": "fallida", "resumen": "x"}))]
    policy, model, screen, _, _ = make(script)
    orientation, plan = await start(policy, screen, [HOY])
    await policy.run_plan(INSTRUCTION, plan, orientation, screen.image, [HOY])
    assert "Hints from previous tasks" not in str(model.requests[1]["messages"][0]["content"])


@pytest.mark.asyncio
async def test_orient_reads_the_app_version_and_menu_in_code() -> None:
    policy, model, screen, _, _ = make([])
    header = [
        TextBox(text="SIGECOM · Distribuidora Andina", x=12, y=10, w=248, h=14),
        TextBox(text="v4.2", x=277, y=10, w=28, h=14),
        TextBox(text="Archivo", x=18, y=42, w=48, h=14),
        TextBox(text="Comercial", x=90, y=42, w=65, h=14),
    ]
    orientation = await policy.orient(screen.image, [HOY, *header])
    assert orientation.version == "4.2"
    assert orientation.app == "SIGECOM · Distribuidora Andina"
    assert orientation.menu == ("Archivo", "Comercial")
    assert model.requests == []
