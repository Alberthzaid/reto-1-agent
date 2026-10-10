"""Field reread after typing: only the clicked input is read, delayed rendering is awaited, and the
reread is compared with what was typed (SIGECOM v4.2 run: NIT reread picked up the phone digits)."""

from typing import Any

import pytest
from PIL import Image, ImageDraw

from agent.domain.guard import Guard
from agent.domain.models import TextBox, ToolCall
from agent.domain.tools import ToolBox, field_region
from tests.fakes import FakeClock, FakeReader, FakeScreen, FakeSettler, overlaps

Rect = tuple[int, int, int, int]
WHITE = (255, 255, 255)
BORDER = (206, 212, 218)
TEXT = (33, 37, 41)

# v4.2 create-client form: NIT and Teléfono side by side on one row, Razón social above.
RAZON: Rect = (24, 122, 880, 28)
NIT: Rect = (24, 166, 430, 28)
TELEFONO: Rect = (474, 166, 430, 28)
NIT_POINT = (239, 180)
TEL_POINT = (688, 180)


def form(values: dict[Rect, str] | None = None) -> Image.Image:
    image = Image.new("RGB", (1280, 800), WHITE)
    draw = ImageDraw.Draw(image)
    for rect in (RAZON, NIT, TELEFONO):
        x, y, w, h = rect
        draw.rectangle([x, y, x + w - 1, y + h - 1], outline=BORDER)
        text = (values or {}).get(rect)
        if text:
            draw.text((x + 8, y + 8), text, fill=TEXT)
    return image


def has_ink(image: Image.Image, rect: Rect) -> bool:
    x, y, w, h = rect
    crop = image.convert("L").crop((x + 2, y + 2, x + w - 2, y + h - 2))
    return crop.getextrema()[0] < 128


def ocr_like(texts: dict[Rect, str]) -> Any:
    """Region OCR like Tesseract: every input with rendered text that the region overlaps is read."""

    def read(image: Image.Image, region: tuple[int, int, int, int]) -> list[TextBox]:
        found = []
        for rect, text in texts.items():
            if overlaps(region, rect) and has_ink(image, rect):
                found.append(TextBox(text=text, x=rect[0] + 8, y=rect[1] + 8, w=8 * len(text), h=12))
        return found

    return read


class DelayedScreen(FakeScreen):
    """Shows `before` for the first `delay` screenshots, then `after` (text rendered late)."""

    def __init__(self, before: Image.Image, after: Image.Image, delay: int) -> None:
        super().__init__(before)
        self.after = after
        self.delay = delay
        self.shots = 0

    async def screenshot(self) -> Image.Image:
        self.shots += 1
        return (self.image if self.shots <= self.delay else self.after).copy()


def make(screen: FakeScreen, region_fn: Any) -> tuple[ToolBox, FakeReader, FakeClock]:
    reader = FakeReader([], region_fn=region_fn)
    clock = FakeClock()
    toolbox = ToolBox(
        screen, reader, FakeSettler(screen), Guard({"alt+F4"}, 1), clock,
        deadline=clock.now + 200, sleep=clock.sleep,
    )
    toolbox.prime(screen.image.copy(), [])
    return toolbox, reader, clock


def call(name: str, **args: Any) -> ToolCall:
    return ToolCall(id="toolu_1", name=name, input=args)


TEXTS = {RAZON: "Papelería Los Andes", NIT: "901234567", TELEFONO: "3157654321"}


# --- field_region: pure image function ----------------------------------------------------------------


@pytest.mark.parametrize("point,field,neighbor", [(NIT_POINT, NIT, TELEFONO), (TEL_POINT, TELEFONO, NIT)])
def test_field_region_covers_only_the_clicked_input(point: tuple[int, int], field: Rect, neighbor: Rect) -> None:
    region = field_region(form(TEXTS), *point)
    assert not overlaps(region, neighbor)
    assert region[0] <= field[0] + 2 and region[0] + region[2] >= field[0] + field[2] - 2
    assert region[1] <= point[1] < region[1] + region[3]
    assert region[3] <= 60


def test_field_region_covers_a_full_width_input() -> None:
    # Razón social spans the whole v4.2 form (~873 px); its text starts at the left edge.
    region = field_region(form(TEXTS), 463, 136)
    assert region[0] <= RAZON[0] + 2 and region[0] + region[2] >= RAZON[0] + RAZON[2] - 2
    assert not overlaps(region, NIT) and region[3] <= 60


def test_field_region_fallback_is_a_narrow_strip_clipped_at_vertical_borders() -> None:
    image = Image.new("RGB", (1280, 800), WHITE)
    draw = ImageDraw.Draw(image)
    draw.line([(540, 0), (540, 799)], fill=BORDER)  # tall separators: no closed input box
    draw.line([(700, 0), (700, 799)], fill=BORDER)
    region = field_region(image, 600, 400)
    assert region[0] >= 540 and region[0] + region[2] <= 701
    assert region[1] <= 400 < region[1] + region[3] and region[3] <= 60


def test_field_region_fallback_without_borders_is_at_most_320_px_wide() -> None:
    region = field_region(Image.new("RGB", (1280, 800), WHITE), 640, 400)
    assert region[2] <= 2 * 160 + 1
    assert region[0] <= 640 < region[0] + region[2]


# --- ToolBox: reread, delayed render, match message, replace=false refusal -----------------------------


@pytest.mark.asyncio
async def test_fill_form_rereads_each_field_without_its_neighbor() -> None:
    screen = FakeScreen(form())
    toolbox, _, _ = make(screen, ocr_like(TEXTS))
    screen.image = form(TEXTS)  # what the screen shows after typing
    toolbox.prime(form(), [])
    outcome = await toolbox.execute(call("fill_form", fields=[
        {"x": NIT_POINT[0], "y": NIT_POINT[1], "value": "901234567"},
        {"x": TEL_POINT[0], "y": TEL_POINT[1], "value": "3157654321"},
    ]))
    assert not outcome.is_error
    assert "'901234567' (coincide con lo escrito)" in outcome.text
    assert "'3157654321' (coincide con lo escrito)" in outcome.text
    assert "901234567 | 3157654321" not in outcome.text


@pytest.mark.asyncio
async def test_type_into_waits_for_delayed_rendering_before_rereading() -> None:
    screen = DelayedScreen(form(), form({NIT: "901234567"}), delay=3)
    toolbox, _, clock = make(screen, ocr_like(TEXTS))
    outcome = await toolbox.execute(call("type_into", x=NIT_POINT[0], y=NIT_POINT[1], text="901234567"))
    assert "'901234567' (coincide con lo escrito)" in outcome.text
    assert "vacío o ilegible" not in outcome.text
    assert sum(clock.sleeps) <= 2.0 + 1e-9


@pytest.mark.asyncio
async def test_field_wait_is_capped() -> None:
    screen = FakeScreen(form())  # the text never renders
    toolbox, _, clock = make(screen, ocr_like(TEXTS))
    outcome = await toolbox.execute(call("type_into", x=NIT_POINT[0], y=NIT_POINT[1], text="901234567"))
    assert sum(clock.sleeps) <= 2.0 + 1e-9
    assert "vacío o ilegible" in outcome.text


@pytest.mark.asyncio
async def test_mismatched_reread_suggests_zoom_and_replace() -> None:
    screen = FakeScreen(form({NIT: "9O1234567"}))
    toolbox, _, _ = make(screen, ocr_like({NIT: "9O1234S67"}))
    outcome = await toolbox.execute(call("type_into", x=NIT_POINT[0], y=NIT_POINT[1], text="901234567"))
    assert not outcome.is_error
    assert "'9O1234S67'" in outcome.text
    assert "coincide con lo escrito)" not in outcome.text
    assert "OCR" in outcome.text and "zoom" in outcome.text and "replace=true" in outcome.text


@pytest.mark.asyncio
async def test_match_ignores_spacing_case_and_punctuation() -> None:
    screen = FakeScreen(form({RAZON: "x"}))
    toolbox, _, _ = make(screen, ocr_like({RAZON: " papelería  los andes. "}))
    outcome = await toolbox.execute(call("type_into", x=463, y=136, text="Papelería Los Andes"))
    assert "(coincide con lo escrito)" in outcome.text


@pytest.mark.asyncio
async def test_append_refused_when_field_already_holds_the_value() -> None:
    screen = FakeScreen(form({NIT: "901234567"}))
    toolbox, _, _ = make(screen, ocr_like(TEXTS))
    outcome = await toolbox.execute(
        call("type_into", x=NIT_POINT[0], y=NIT_POINT[1], text="901234567", replace=False)
    )
    assert outcome.is_error
    assert "replace=true" in outcome.text
    assert screen.write_actions == []


@pytest.mark.asyncio
async def test_append_into_an_empty_field_still_types() -> None:
    screen = FakeScreen(form())
    toolbox, _, _ = make(screen, ocr_like(TEXTS))
    outcome = await toolbox.execute(
        call("type_into", x=NIT_POINT[0], y=NIT_POINT[1], text="901234567", replace=False)
    )
    assert not outcome.is_error
    assert ("type_text", "901234567") in screen.actions
    assert ("key", "ctrl+a") not in screen.actions
