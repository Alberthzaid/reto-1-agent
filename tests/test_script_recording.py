"""ToolBox records a replayable script: guard labels and plan field NAMES, never the typed values."""

import pytest

from agent.domain.guard import Guard
from agent.domain.models import Plan, ScriptStep, TextBox, ToolCall
from agent.domain.procedures import sanitize_script
from agent.domain.tools import ToolBox
from tests.fakes import FakeClock, FakeScreen, FakeSettler, TypingReader

CLIENTES = TextBox(text="Clientes", x=100, y=100, w=60, h=20)
NIT_LABEL = TextBox(text="NIT *", x=240, y=270, w=40, h=14)
NOMBRE_LABEL = TextBox(text="Nombre *", x=240, y=330, w=60, h=14)
GUARDAR = TextBox(text="Guardar", x=600, y=700, w=80, h=20)
VALUES = (("nombre", "Papelería Los Andes"), ("nit", "901234567"))
PLAN = Plan(kind="crear", entity="cliente", target="Papelería Los Andes", values=VALUES, planned_commits=1)


def make(boxes: list[TextBox] | None = None, plan_values: tuple[tuple[str, str], ...] = VALUES) -> ToolBox:
    screen = FakeScreen()
    reader = TypingReader(screen, boxes or [CLIENTES, NIT_LABEL, NOMBRE_LABEL, GUARDAR])
    clock = FakeClock()
    toolbox = ToolBox(screen, reader, FakeSettler(screen), Guard({"alt+F4"}, 1), clock,
                      deadline=clock.now + 200, sleep=clock.sleep, plan_values=plan_values)
    toolbox.prime(screen.image, list(reader.boxes))
    return toolbox


def call(name: str, **args: object) -> ToolCall:
    return ToolCall(id="toolu_1", name=name, input=dict(args))


@pytest.mark.asyncio
async def test_script_maps_typed_values_to_plan_field_names() -> None:
    toolbox = make()
    await toolbox.execute(call("click", x=130, y=110))
    await toolbox.execute(call("fill_form", fields=[{"x": 400, "y": 300, "value": "901234567"},
                                                    {"x": 400, "y": 360, "value": "papelería los andes "}]))
    await toolbox.execute(call("key", combo="ctrl+a"))
    await toolbox.execute(call("key", combo="tab"))
    await toolbox.execute(call("click", x=640, y=710))
    script = toolbox.script
    assert [(s.kind, s.anchor, s.field_label, s.field) for s in script] == [
        ("click", "Clientes", "", ""),
        ("fill", "", "NIT *", "nit"),
        ("fill", "", "Nombre *", "nombre"),
        ("key", "tab", "", ""),
        ("click", "Guardar", "", ""),
    ]
    assert script[-1].commit and not script[0].commit
    assert (script[0].x, script[0].y) == (130, 110)
    # Offset from the label center to the field point, so a replay can find the input again.
    assert (script[1].dx, script[1].dy) == (400 - 260, 300 - 277)
    dump = repr(script)
    assert "901234567" not in dump and "Papeler" not in dump


@pytest.mark.asyncio
async def test_type_into_is_recorded_as_a_fill_step() -> None:
    toolbox = make()
    await toolbox.execute(call("type_into", x=400, y=300, text="901234567"))
    assert [(s.kind, s.field_label, s.field) for s in toolbox.script] == [("fill", "NIT *", "nit")]


@pytest.mark.asyncio
async def test_text_that_is_not_a_plan_value_leaves_no_script() -> None:
    toolbox = make()
    await toolbox.execute(call("click", x=130, y=110))
    await toolbox.execute(call("type_into", x=400, y=300, text="algo que no está en el plan"))
    await toolbox.execute(call("click", x=640, y=710))
    assert toolbox.script == ()
    assert toolbox.steps  # the text hints are still recorded


@pytest.mark.asyncio
async def test_without_plan_values_typing_leaves_no_script() -> None:
    toolbox = make(plan_values=())
    await toolbox.execute(call("type_into", x=400, y=300, text="901234567"))
    assert toolbox.script == ()


@pytest.mark.asyncio
async def test_plan_values_can_be_set_after_construction() -> None:
    toolbox = make(plan_values=())
    toolbox.set_plan_values(VALUES)
    await toolbox.execute(call("type_into", x=400, y=300, text="901234567"))
    assert [s.field for s in toolbox.script] == ["nit"]


@pytest.mark.asyncio
async def test_unlabeled_click_leaves_no_script() -> None:
    toolbox = make()
    await toolbox.execute(call("click", x=1200, y=500))  # nothing readable there
    assert toolbox.script == ()


@pytest.mark.asyncio
async def test_unlabeled_field_is_kept_at_its_position() -> None:
    toolbox = make(boxes=[CLIENTES, GUARDAR])
    await toolbox.execute(call("type_into", x=400, y=300, text="901234567"))  # no field label nearby
    [step] = toolbox.script
    assert (step.kind, step.field, step.field_label, step.x, step.y) == ("fill", "nit", "", 400, 300)


@pytest.mark.asyncio
async def test_single_character_keys_count_as_typed_data() -> None:
    toolbox = make()
    await toolbox.execute(call("key", combo="5"))
    assert toolbox.script == ()


def test_sanitize_script_drops_the_whole_script_when_a_label_holds_task_data() -> None:
    clean = (ScriptStep(kind="click", anchor="Clientes"), ScriptStep(kind="fill", field_label="NIT *", field="nit"))
    assert sanitize_script(clean, PLAN, []) == clean
    for leaked in (ScriptStep(kind="click", anchor="Papelería Los Andes"),
                   ScriptStep(kind="click", anchor="Fila 12"),
                   ScriptStep(kind="fill", field_label="ventas@andes.co", field="correo"),
                   ScriptStep(kind="click", anchor="clave-secreta")):
        assert sanitize_script((*clean, leaked), PLAN, ["clave-secreta"]) == ()
