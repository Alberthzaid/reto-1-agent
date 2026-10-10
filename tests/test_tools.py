from typing import Any

import pytest
from PIL import Image

from agent.domain.guard import Guard
from agent.domain.models import TextBox, ToolCall
from agent.domain.tools import TOOL_SPECS, ToolBox, image_block, ocr_listing
from tests.fakes import FakeClock, FakeReader, FakeScreen, FakeSettler, FakeTrace

BLOCKED = {"ctrl+w", "alt+F4", "F5"}
BUSCAR = TextBox(text="Buscar", x=100, y=100, w=60, h=20)
GUARDAR = TextBox(text="Guardar", x=600, y=700, w=80, h=20)
NOMBRE = TextBox(text="Nombre:", x=200, y=300, w=70, h=20)


def make(
    boxes: list[TextBox] | None = None,
    planned_commits: int = 1,
    changed: bool = True,
    region_boxes: list[TextBox] | None = None,
    trace: FakeTrace | None = None,
) -> tuple[ToolBox, FakeScreen, FakeReader, Guard, FakeClock]:
    screen = FakeScreen()
    reader = FakeReader(boxes if boxes is not None else [BUSCAR, GUARDAR, NOMBRE], region_boxes)
    guard = Guard(BLOCKED, planned_commits)
    clock = FakeClock()
    toolbox = ToolBox(
        screen, reader, FakeSettler(screen, changed), guard, clock,
        trace=trace, deadline=clock.now + 200, sleep=clock.sleep,
    )
    toolbox.prime(screen.image, list(reader.boxes))
    return toolbox, screen, reader, guard, clock


def call(name: str, **args: Any) -> ToolCall:
    return ToolCall(id="toolu_1", name=name, input=args)


def test_tool_specs_are_strict_and_closed() -> None:
    names = {spec["name"] for spec in TOOL_SPECS}
    assert names == {"click", "type_into", "key", "scroll", "wait", "zoom", "fill_form", "finish"}
    for spec in TOOL_SPECS:
        assert spec["strict"] is True
        schema = spec["input_schema"]
        assert schema["type"] == "object" and schema["additionalProperties"] is False
        assert set(schema["required"]) <= set(schema["properties"])


def test_image_block_is_base64_png() -> None:
    block = image_block(Image.new("RGB", (4, 4)))
    assert block["type"] == "image"
    assert block["source"]["media_type"] == "image/png"
    assert block["source"]["type"] == "base64"
    assert isinstance(block["source"]["data"], str) and block["source"]["data"]


def test_ocr_listing_uses_centers_and_caps() -> None:
    boxes = [TextBox(text=f"t{i}", x=0, y=i * 2, w=10, h=2) for i in range(200)]
    listing = ocr_listing(boxes, cap=150)
    assert listing.splitlines()[0] == "t0 @5,1"
    assert len([line for line in listing.splitlines() if "@" in line]) == 150
    assert "50 omitidos" in listing


@pytest.mark.asyncio
async def test_click_non_commit_returns_observation_with_image() -> None:
    toolbox, screen, _, _, _ = make()
    outcome = await toolbox.execute(call("click", x=130, y=110))
    assert not outcome.is_error
    assert screen.actions == [("click", 130, 110, "left", False)]
    assert outcome.image is not None and outcome.image.size == (1280, 800)
    assert "Buscar @130,110" in outcome.text
    assert "Tiempo restante" in outcome.text


@pytest.mark.asyncio
@pytest.mark.parametrize("x,y", [(-1, 10), (1280, 10), (10, 800), (True, 10)])
async def test_click_out_of_range_is_rejected_without_touching_screen(x: Any, y: int) -> None:
    toolbox, screen, _, _, _ = make()
    outcome = await toolbox.execute(call("click", x=x, y=y))
    assert outcome.is_error
    assert screen.actions == []


@pytest.mark.asyncio
async def test_commit_click_is_recorded_and_second_one_blocked() -> None:
    toolbox, screen, _, guard, _ = make(planned_commits=1)
    first = await toolbox.execute(call("click", x=640, y=710))
    assert not first.is_error
    assert guard.commits == ("Guardar",)
    assert "Guardar" in first.text
    await toolbox.execute(call("zoom", x=0, y=0, w=100, h=100))
    second = await toolbox.execute(call("click", x=640, y=710))
    assert second.is_error and "bloqueado" in second.text.lower()
    assert len([a for a in screen.actions if a[0] == "click"]) == 1


@pytest.mark.asyncio
async def test_type_into_clicks_selects_types_and_rereads_field() -> None:
    field = TextBox(text="Nombre: Papelería Los Andes", x=200, y=300, w=300, h=20)
    toolbox, screen, reader, _, _ = make(region_boxes=[field])
    outcome = await toolbox.execute(call("type_into", x=400, y=310, text="Papelería Los Andes"))
    assert not outcome.is_error
    assert screen.actions == [
        ("click", 400, 310, "left", False),
        ("key", "ctrl+a"),
        ("type_text", "Papelería Los Andes"),
    ]
    strip = [c for c in reader.calls if c is not None]
    assert strip and strip[0][1] <= 310 <= strip[0][1] + strip[0][3]
    assert "Papelería Los Andes" in outcome.text


@pytest.mark.asyncio
async def test_type_into_without_replace_does_not_select_all() -> None:
    toolbox, screen, _, _, _ = make()
    await toolbox.execute(call("type_into", x=400, y=310, text="x", replace=False))
    assert ("key", "ctrl+a") not in screen.actions


@pytest.mark.asyncio
async def test_type_into_on_blocked_commit_does_nothing() -> None:
    toolbox, screen, _, _, _ = make(planned_commits=0)
    outcome = await toolbox.execute(call("type_into", x=640, y=710, text="x"))
    assert outcome.is_error
    assert screen.actions == []


@pytest.mark.asyncio
async def test_blocked_key_is_rejected() -> None:
    toolbox, screen, _, _, _ = make()
    outcome = await toolbox.execute(call("key", combo="alt+F4"))
    assert outcome.is_error
    assert screen.actions == []
    ok = await toolbox.execute(call("key", combo="enter"))
    assert not ok.is_error
    assert screen.actions == [("key", "enter")]


@pytest.mark.asyncio
async def test_scroll_direction_maps_to_sign() -> None:
    toolbox, screen, _, _, _ = make()
    await toolbox.execute(call("scroll", x=600, y=400, direction="down", steps=3))
    await toolbox.execute(call("scroll", x=600, y=400, direction="up", steps=2))
    assert screen.actions == [("scroll", 600, 400, -3), ("scroll", 600, 400, 2)]


@pytest.mark.asyncio
async def test_wait_is_capped() -> None:
    toolbox, _, _, _, clock = make()
    assert (await toolbox.execute(call("wait", seconds=5))).is_error
    outcome = await toolbox.execute(call("wait", seconds=1.5))
    assert not outcome.is_error
    assert clock.sleeps == [1.5]


@pytest.mark.asyncio
async def test_zoom_crops_upscales_and_reads_region() -> None:
    toolbox, screen, reader, _, _ = make()
    outcome = await toolbox.execute(call("zoom", x=90, y=90, w=200, h=50))
    assert not outcome.is_error
    assert outcome.image is not None and outcome.image.size == (400, 100)
    assert (90, 90, 200, 50) in reader.calls
    assert "Buscar" in outcome.text
    assert screen.write_actions == []


@pytest.mark.asyncio
async def test_zoom_result_width_is_capped() -> None:
    toolbox, _, _, _, _ = make()
    outcome = await toolbox.execute(call("zoom", x=0, y=0, w=1000, h=100))
    assert outcome.image is not None and outcome.image.width <= 1280


@pytest.mark.asyncio
async def test_zoom_outside_screen_is_rejected() -> None:
    toolbox, _, _, _, _ = make()
    assert (await toolbox.execute(call("zoom", x=1200, y=0, w=200, h=100))).is_error


@pytest.mark.asyncio
async def test_fill_form_types_each_field() -> None:
    toolbox, screen, _, _, _ = make()
    outcome = await toolbox.execute(
        call("fill_form", fields=[{"x": 400, "y": 310, "value": "A"}, {"x": 400, "y": 350, "value": "B"}])
    )
    assert not outcome.is_error
    assert screen.actions == [
        ("click", 400, 310, "left", False), ("key", "ctrl+a"), ("type_text", "A"),
        ("click", 400, 350, "left", False), ("key", "ctrl+a"), ("type_text", "B"),
    ]


@pytest.mark.asyncio
async def test_fill_form_with_a_commit_field_does_nothing() -> None:
    toolbox, screen, _, _, _ = make(planned_commits=0)
    outcome = await toolbox.execute(
        call("fill_form", fields=[{"x": 400, "y": 310, "value": "A"}, {"x": 640, "y": 710, "value": "B"}])
    )
    assert outcome.is_error
    assert screen.actions == []


@pytest.mark.asyncio
async def test_finish_returns_payload_without_touching_screen() -> None:
    toolbox, screen, _, _, _ = make()
    outcome = await toolbox.execute(call("finish", estado="terminada", resumen="hecho", respuesta=["45"]))
    assert outcome.finish == {"estado": "terminada", "resumen": "hecho", "respuesta": ["45"]}
    assert screen.actions == []
    bad = await toolbox.execute(call("finish", estado="quizas", resumen="x"))
    assert bad.is_error and bad.finish is None


@pytest.mark.asyncio
async def test_unknown_tool_is_an_error() -> None:
    toolbox, _, _, _, _ = make()
    outcome = await toolbox.execute(call("drag", x=1, y=1))
    assert outcome.is_error


@pytest.mark.asyncio
async def test_observation_reports_changes_and_commits() -> None:
    toolbox, screen, _, _, _ = make()
    screen.image = Image.new("RGB", (1280, 800), "white")
    screen.image.paste((0, 0, 0), (10, 10, 50, 30))
    outcome = await toolbox.execute(call("click", x=640, y=710))
    assert "(10,10,40,20)" in outcome.text
    assert "Confirmaciones hechas: 1" in outcome.text


@pytest.mark.asyncio
async def test_steps_are_traced() -> None:
    trace = FakeTrace()
    toolbox, _, _, _, _ = make(trace=trace)
    await toolbox.execute(call("click", x=130, y=110))
    await toolbox.execute(call("key", combo="tab"))
    assert [step[0] for step in trace.steps] == [1, 2]
    assert trace.steps[0][1]["tool"] == "click"
