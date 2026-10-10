import pytest

from agent.domain.guard import Guard
from agent.domain.models import TextBox, ToolCall
from agent.domain.tools import MAX_RECORDED_STEPS, ToolBox
from tests.fakes import FakeClock, FakeReader, FakeScreen, FakeSettler

CLIENTES = TextBox(text="Clientes", x=100, y=100, w=60, h=20)
GUARDAR = TextBox(text="Guardar", x=600, y=700, w=80, h=20)


def make(planned_commits: int = 1) -> ToolBox:
    screen = FakeScreen()
    reader = FakeReader([CLIENTES, GUARDAR])
    clock = FakeClock()
    toolbox = ToolBox(screen, reader, FakeSettler(screen), Guard({"alt+F4"}, planned_commits), clock,
                      deadline=clock.now + 200, sleep=clock.sleep)
    toolbox.prime(screen.image, list(reader.boxes))
    return toolbox


def call(name: str, **args: object) -> ToolCall:
    return ToolCall(id="toolu_1", name=name, input=dict(args))


@pytest.mark.asyncio
async def test_records_navigation_without_typed_values() -> None:
    toolbox = make()
    await toolbox.execute(call("click", x=130, y=110))
    await toolbox.execute(call("type_into", x=400, y=400, text="Papelería Los Andes"))
    await toolbox.execute(call("fill_form", fields=[{"x": 400, "y": 400, "value": "901234567"},
                                                    {"x": 400, "y": 450, "value": "secreto"}]))
    await toolbox.execute(call("key", combo="enter"))
    await toolbox.execute(call("scroll", x=600, y=400, direction="down", steps=3))
    await toolbox.execute(call("click", x=640, y=710))
    assert toolbox.steps == [
        "click 'Clientes'", "type into a field", "fill form (2 fields)", "key enter", "scroll down", "click 'Guardar'",
    ]
    joined = " ".join(toolbox.steps)
    assert "Papelería" not in joined and "901234567" not in joined and "secreto" not in joined


@pytest.mark.asyncio
async def test_unlabeled_clicks_blocked_actions_and_reads_are_not_recorded() -> None:
    toolbox = make(planned_commits=0)
    await toolbox.execute(call("click", x=1200, y=500))  # no label nearby
    await toolbox.execute(call("click", x=640, y=710))  # blocked by the guard
    await toolbox.execute(call("key", combo="alt+F4"))  # blocked combo
    await toolbox.execute(call("zoom", x=0, y=0, w=100, h=100))
    await toolbox.execute(call("wait", seconds=1))
    assert toolbox.steps == []


@pytest.mark.asyncio
async def test_consecutive_duplicates_collapse_and_list_is_capped() -> None:
    toolbox = make()
    await toolbox.execute(call("key", combo="tab"))
    await toolbox.execute(call("key", combo="tab"))
    assert toolbox.steps == ["key tab"]
    for i in range(MAX_RECORDED_STEPS + 5):
        await toolbox.execute(call("scroll", x=600, y=400, direction="down" if i % 2 else "up", steps=1))
    assert len(toolbox.steps) == MAX_RECORDED_STEPS == 15
