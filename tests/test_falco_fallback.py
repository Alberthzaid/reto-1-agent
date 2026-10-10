import asyncio

import pytest
from PIL import Image

from agent.application.worker import AgentWorker
from agent.config import Settings
from agent.domain.guard import Guard
from agent.domain.models import Classification, TaskStatus, TextBox
from agent.domain.policy import Orientation, Policy
from agent.domain.tools import ToolBox
from agent.infra.falco_classifier import FalcoClassifier
from tests.fakes import FakeClock, FakeModel, FakeReader, FakeScreen, FakeSettler, tool_decision


class StubFalco:
    def __init__(self, result: dict | Exception | None = None, delay: float = 0.0) -> None:
        self.result = result or {}
        self.delay = delay

    def prediction(self, state: str, questions: dict) -> dict:
        if self.delay:
            import time

            time.sleep(self.delay)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


async def loaded_classifier(model: StubFalco, clock=lambda: 0.0) -> FalcoClassifier:
    classifier = FalcoClassifier(True, "unused", loader=lambda _path: model, clock=clock)
    task = classifier.start()
    assert task is not None
    await task
    return classifier


@pytest.mark.asyncio
async def test_disabled_or_missing_falco_never_changes_the_flow() -> None:
    classifier = FalcoClassifier(False, "missing-model")
    assert await classifier.classify("crear cliente") is None


@pytest.mark.asyncio
async def test_enabled_falco_without_optional_dependencies_degrades_to_none() -> None:
    classifier = FalcoClassifier(True, "missing-model")
    load_task = classifier.start()
    assert load_task is not None
    await load_task
    assert await classifier.classify("crear cliente") is None


@pytest.mark.asyncio
async def test_slow_falco_is_discarded_after_300ms() -> None:
    classifier = await loaded_classifier(StubFalco(delay=0.5))
    assert await classifier.classify("crear cliente") is None


@pytest.mark.asyncio
async def test_exception_is_logged_as_absent_hint() -> None:
    classifier = await loaded_classifier(StubFalco(RuntimeError("falló Falco")))
    assert await classifier.classify("crear cliente") is None


@pytest.mark.asyncio
async def test_low_confidence_fields_are_discarded() -> None:
    classifier = await loaded_classifier(
        StubFalco(
            {
                "tipo": {"type": "choice", "choice": "crear", "confidence": 0.5},
                "destructiva": {"type": "noul", "noul": 0.55},
                "dos_datos": {"type": "noul", "noul": 0.4},
            }
        )
    )
    assert await classifier.classify("crear cliente") is None


@pytest.mark.asyncio
async def test_circuit_breaker_stops_calls_for_ten_minutes() -> None:
    now = [100.0]
    model = StubFalco(RuntimeError("falló Falco"))
    classifier = await loaded_classifier(model, clock=lambda: now[0])
    for _ in range(3):
        assert await classifier.classify("eliminar cliente") is None
    assert not classifier.available
    assert await classifier.classify("eliminar cliente") is None
    now[0] += 601
    assert await classifier.classify("eliminar cliente") is None


@pytest.mark.asyncio
async def test_high_confidence_hint_is_exposed_without_changing_worker_contract() -> None:
    classifier = await loaded_classifier(
        StubFalco(
            {
                "tipo": {"type": "choice", "choice": "eliminar", "confidence": 0.9},
                "destructiva": {"type": "noul", "noul": 0.95},
                "dos_datos": {"type": "noul", "noul": 0.9},
            }
        )
    )
    hint = await classifier.classify("eliminar dos clientes")
    assert hint is not None
    assert hint.task_type == "eliminar"
    assert hint.destructive is True
    assert hint.two_data is True


@pytest.mark.asyncio
async def test_falco_hints_only_add_caution_to_policy() -> None:
    clock = FakeClock()
    screen = FakeScreen()
    guard = Guard(set(), 0)
    toolbox = ToolBox(
        screen,
        FakeReader([TextBox(text="Buscar", x=1, y=1, w=20, h=10)]),
        FakeSettler(screen),
        guard,
        clock,
        deadline=clock.now + 100,
        sleep=clock.sleep,
    )
    plan_input = {
        "kind": "eliminar",
        "entity": "cliente",
        "target": "Duplicado",
        "values": [],
        "preconditions": ["hay una coincidencia exacta"],
        "planned_commits": 1,
        "feasible": True,
        "reason": "",
        "expected_evidence": "el cliente ya no aparece",
    }
    model = FakeModel([tool_decision(("submit_plan", plan_input))])
    policy = Policy(
        model,
        toolbox,
        guard,
        clock,
        clock.now + 100,
        classification=Classification(
            app="",
            intent="eliminar",
            task_type="eliminar",
            destructive=True,
            two_data=True,
        ),
    )

    plan = await policy.plan("Eliminar el cliente duplicado", Orientation(), Image.new("RGB", (4, 4)), [])

    assert plan.planned_commits == 1
    assert "confirmar en pantalla el alcance exacto antes de cualquier commit" in plan.preconditions
    request = model.requests[0]
    assert "do not relax any precondition or verification" in request["system"]
    assert "A; B" in request["system"]


class StaticClassifier:
    def __init__(self, hint: Classification | None) -> None:
        self.hint = hint

    async def classify(self, instruction: str) -> Classification | None:
        return self.hint


@pytest.mark.asyncio
async def test_worker_result_is_unchanged_with_or_without_falco_hint() -> None:
    plan_input = {
        "kind": "crear",
        "entity": "cliente",
        "target": "Papelería",
        "values": [],
        "preconditions": [],
        "planned_commits": 0,
        "feasible": True,
        "reason": "",
        "expected_evidence": "cliente visible",
    }

    async def run_with(classifier: StaticClassifier) -> tuple[TaskStatus, str]:
        clock = FakeClock()
        fast = FakeModel(
            [
                tool_decision(("submit_plan", plan_input)),
                tool_decision(("finish", {"estado": "terminada", "resumen": "Cliente visible."})),
            ]
        )
        strong = FakeModel([tool_decision(("verdict", {"confirmed": True, "evidence": "visible"}))])
        screen = FakeScreen()
        worker = AgentWorker(
            "Crear cliente Papelería",
            "wss://desktop.example.test/websockify",
            "redaction-fixture-value",
            Settings(time_limit_sec=255),
            fast,
            strong,
            FakeReader([TextBox(text="Hoy: 14/10/2026", x=0, y=0, w=100, h=20)]),
            lambda: screen,
            clock=clock,
            sleep=clock.sleep,
            settler_factory=lambda value: FakeSettler(value),  # type: ignore[arg-type]
            classifier=classifier,
        )
        result = await worker.run()
        assert len(strong.requests) == 1
        return result

    absent = await run_with(StaticClassifier(None))
    hinted = await run_with(
        StaticClassifier(
            Classification(
                app="",
                intent="crear",
                task_type="crear",
                destructive=False,
                two_data=True,
            )
        )
    )
    assert absent == hinted == (TaskStatus.terminada, "Cliente visible.")