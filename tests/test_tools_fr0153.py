"""FR-0153 (theme v4.2): exact button labels, commits counted on click, outcome-based commit detection."""

from typing import Any

import pytest
from PIL import Image, ImageDraw

from agent.domain.guard import Guard
from agent.domain.models import TextBox, ToolCall
from agent.domain.tools import COMMIT_SETTLE, ToolBox, button_bounds
from tests.fakes import FakeClock, FakeReader, FakeScreen, FakeSettler, FakeTrace, overlaps

Rect = tuple[int, int, int, int]
WHITE = (255, 255, 255)
PAGE = (216, 221, 230)
LIGHT_BORDER = (109, 122, 144)
DARK_TEXT = (33, 37, 41)
LIGHT_TEXT = (245, 245, 245)

# v4.2 form footer: "Cerrar" (dark text on light) | 10 px gap | "Aplicar" (light text on green).
CERRAR: Rect = (757, 365, 66, 23)
APLICAR: Rect = (833, 365, 68, 23)
# v4.2 client list toolbar: "Exportar" | "Crear cliente".
EXPORTAR: Rect = (1068, 174, 82, 23)
CREAR_CLIENTE: Rect = (1156, 174, 109, 23)
TOAST: Rect = (1105, 744, 156, 37)
TITLE: Rect = (14, 74, 200, 20)


def gradient_button(image: Image.Image, rect: Rect, top: tuple[int, int, int], bottom: tuple[int, int, int],
                    border: tuple[int, int, int], text: str, text_color: tuple[int, int, int]) -> None:
    x, y, w, h = rect
    draw = ImageDraw.Draw(image)
    for row in range(h):
        t = row / max(1, h - 1)
        color = tuple(round(a + (b - a) * t) for a, b in zip(top, bottom))
        draw.line([(x, y + row), (x + w - 1, y + row)], fill=color)
    draw.rectangle([x, y, x + w - 1, y + h - 1], outline=border)
    draw.text((x + 8, y + 6), text, fill=text_color)


def form_footer() -> Image.Image:
    image = Image.new("RGB", (1280, 800), WHITE)
    gradient_button(image, CERRAR, (252, 252, 252), (225, 230, 237), (208, 212, 219), "Cerrar", DARK_TEXT)
    gradient_button(image, APLICAR, (89, 161, 126), (50, 125, 89), (36, 96, 63), "Aplicar", LIGHT_TEXT)
    return image


def list_toolbar() -> Image.Image:
    image = Image.new("RGB", (1280, 800), PAGE)
    gradient_button(image, EXPORTAR, (251, 251, 252), (225, 229, 237), LIGHT_BORDER, "Exportar", DARK_TEXT)
    gradient_button(image, CREAR_CLIENTE, (251, 251, 252), (225, 229, 237), LIGHT_BORDER, "Crear cliente",
                    DARK_TEXT)
    return image


def center(rect: Rect) -> tuple[int, int]:
    return rect[0] + rect[2] // 2, rect[1] + rect[3] // 2


def inside(inner: Rect, outer: Rect) -> bool:
    return (outer[0] <= inner[0] and outer[1] <= inner[1]
            and inner[0] + inner[2] <= outer[0] + outer[2] and inner[1] + inner[3] <= outer[1] + outer[3])


def covers_most(found: Rect, button: Rect) -> bool:
    return found[2] >= button[2] - 4 and found[3] >= button[3] - 4


# --- button_bounds: pure image function --------------------------------------------------------------


@pytest.mark.parametrize("button,neighbor", [(APLICAR, CERRAR), (CERRAR, APLICAR)])
def test_bounds_of_adjacent_form_buttons(button: Rect, neighbor: Rect) -> None:
    found = button_bounds(form_footer(), *center(button))
    assert found is not None
    assert inside(found, button) and covers_most(found, button)
    assert not overlaps(found, neighbor)


@pytest.mark.parametrize("button,neighbor", [(EXPORTAR, CREAR_CLIENTE), (CREAR_CLIENTE, EXPORTAR)])
def test_bounds_of_adjacent_toolbar_buttons(button: Rect, neighbor: Rect) -> None:
    found = button_bounds(list_toolbar(), *center(button))
    assert found is not None
    assert inside(found, button) and covers_most(found, button)
    assert not overlaps(found, neighbor)


def test_bounds_of_dark_blue_button_with_white_text() -> None:
    image = Image.new("RGB", (1280, 800), (216, 221, 230))
    gradient_button(image, TOAST, (36, 71, 122), (36, 71, 122), (16, 40, 70), "Cambios aplicados", WHITE)
    found = button_bounds(image, *center(TOAST))
    assert found is not None and inside(found, TOAST) and covers_most(found, TOAST)


def test_click_on_a_text_pixel_still_finds_the_button() -> None:
    image = form_footer()
    x, y = APLICAR[0] + 9, APLICAR[1] + 8
    ImageDraw.Draw(image).rectangle([x - 1, y - 1, x + 1, y + 1], fill=LIGHT_TEXT)
    found = button_bounds(image, x, y)
    assert found is not None and inside(found, APLICAR) and not overlaps(found, CERRAR)


@pytest.mark.parametrize("point", [(400, 600), (5, 5)])
def test_plain_background_has_no_button(point: tuple[int, int]) -> None:
    assert button_bounds(form_footer(), *point) is None


def test_tiny_or_huge_areas_are_not_buttons() -> None:
    image = Image.new("RGB", (1280, 800), WHITE)
    ImageDraw.Draw(image).rectangle([100, 100, 110, 106], fill=(0, 120, 0))  # 11x7: icon, too small
    ImageDraw.Draw(image).rectangle([200, 200, 900, 230], fill=(0, 0, 120))  # 701 px wide: a banner
    assert button_bounds(image, 105, 103) is None
    assert button_bounds(image, 500, 215) is None


# --- ToolBox: the guard reads only the clicked button ------------------------------------------------


def make(
    image: Image.Image,
    region_fn: Any,
    planned_commits: int = 1,
    boxes: list[TextBox] | None = None,
    changed: bool = True,
    trace: FakeTrace | None = None,
) -> tuple[ToolBox, FakeScreen, FakeReader, Guard, FakeSettler]:
    screen = FakeScreen(image)
    reader = FakeReader(boxes or [], region_fn=region_fn)
    guard = Guard({"alt+F4"}, planned_commits)
    clock = FakeClock()
    settler = FakeSettler(screen, changed)
    toolbox = ToolBox(screen, reader, settler, guard, clock, trace=trace, deadline=clock.now + 200, sleep=clock.sleep)
    toolbox.prime(image.copy(), list(reader.boxes))
    return toolbox, screen, reader, guard, settler


def label_reader(buttons: dict[str, Rect], extra: dict[str, Rect] | None = None) -> Any:
    """Region OCR like Tesseract on the real screen: a crop over two buttons reads both, merged."""

    def read(img: Image.Image, region: Rect) -> list[TextBox]:
        hits = [(name, rect) for name, rect in {**buttons, **(extra or {})}.items() if overlaps(region, rect)]
        if not hits:
            return []
        text = " | ".join(name for name, _ in sorted(hits, key=lambda item: item[1][0]))
        x, y, w, h = region
        return [TextBox(text=text, x=x, y=y, w=w, h=h)]

    return read


def call(name: str, **args: Any) -> ToolCall:
    return ToolCall(id="toolu_1", name=name, input=args)


@pytest.mark.asyncio
async def test_apply_click_reads_only_the_apply_button() -> None:
    toolbox, screen, reader, guard, _ = make(form_footer(), label_reader({"Cerrar": CERRAR, "Aplicar": APLICAR}))
    guard.note_data_entry()
    outcome = await toolbox.execute(call("click", x=864, y=376))
    assert not outcome.is_error
    first_region = next(region for region in reader.calls if region is not None)
    assert overlaps(first_region, APLICAR) and not overlaps(first_region, CERRAR)
    assert guard.commits == ("Aplicar",)


@pytest.mark.asyncio
async def test_close_click_is_not_a_commit() -> None:
    toolbox, _, _, guard, _ = make(form_footer(), label_reader({"Cerrar": CERRAR, "Aplicar": APLICAR}))
    await toolbox.execute(call("click", x=790, y=376))
    assert guard.commits == ()


@pytest.mark.asyncio
async def test_create_client_on_the_toolbar_is_navigation() -> None:
    toolbox, screen, _, guard, _ = make(
        list_toolbar(), label_reader({"Exportar": EXPORTAR, "Crear cliente": CREAR_CLIENTE})
    )
    outcome = await toolbox.execute(call("click", x=1209, y=185))
    assert not outcome.is_error
    assert screen.actions == [("click", 1209, 185, "left", False)]
    assert guard.commits == ()
    assert "confirmación registrada" not in outcome.text


@pytest.mark.asyncio
async def test_unknown_button_falls_back_to_a_narrow_crop() -> None:
    toolbox, _, reader, _, _ = make(Image.new("RGB", (1280, 800), WHITE), lambda img, region: [])
    await toolbox.execute(call("click", x=640, y=400))
    x, y, w, h = next(region for region in reader.calls if region is not None)
    assert (w, h) == (100, 28) and x <= 640 < x + w and y <= 400 < y + h


# --- commits count on click; repeats are refused with a hint to look instead -----------------------------


@pytest.mark.asyncio
async def test_commit_counts_even_when_the_screen_did_not_change() -> None:
    toolbox, screen, _, guard, settler = make(
        form_footer(), label_reader({"Cerrar": CERRAR, "Aplicar": APLICAR}), changed=False
    )
    first = await toolbox.execute(call("click", x=864, y=376))
    assert guard.commits == ("Aplicar",)
    assert "wait" in first.text or "zoom" in first.text
    await toolbox.execute(call("wait", seconds=1))
    second = await toolbox.execute(call("click", x=864, y=376))
    assert second.is_error and "título" in second.text
    assert len([a for a in screen.actions if a[0] == "click"]) == 1


@pytest.mark.asyncio
async def test_commit_click_settles_longer() -> None:
    toolbox, _, _, _, settler = make(form_footer(), label_reader({"Cerrar": CERRAR, "Aplicar": APLICAR}))
    await toolbox.execute(call("click", x=790, y=376))
    await toolbox.execute(call("click", x=864, y=376))
    assert settler.kwargs[0] == {}
    assert settler.kwargs[1] == COMMIT_SETTLE
    assert COMMIT_SETTLE["max_wait"] >= 3.0 and COMMIT_SETTLE["quiet"] >= 0.4


class RepaintScreen(FakeScreen):
    """After the n-th click, the next screenshots show `after` (title change, toast, new screen)."""

    def __init__(self, image: Image.Image, after: Image.Image, on_click: int = 1) -> None:
        super().__init__(image)
        self.after = after
        self.on_click = on_click

    async def click(self, x: int, y: int, button: str = "left", double: bool = False) -> None:
        await super().click(x, y, button, double)
        if len([a for a in self.actions if a[0] == "click"]) == self.on_click:
            self.image = self.after


def with_title_and_toast(image: Image.Image) -> Image.Image:
    out = image.copy()
    draw = ImageDraw.Draw(out)
    draw.rectangle([TITLE[0], TITLE[1], TITLE[0] + 150, TITLE[1] + 15], fill=(30, 30, 30))
    draw.rectangle([TOAST[0], TOAST[1], TOAST[0] + TOAST[2], TOAST[1] + TOAST[3]], fill=(36, 71, 122))
    return out


def outcome_reader(title: str, toast: str | None) -> Any:
    buttons = label_reader({"Cerrar": CERRAR, "Aplicar": APLICAR})

    def read(img: Image.Image, region: Rect) -> list[TextBox]:
        changed = img.getpixel((TITLE[0] + 2, TITLE[1] + 2)) == (30, 30, 30)
        if changed and overlaps(region, TITLE):
            return [TextBox(text=title, x=TITLE[0], y=TITLE[1], w=150, h=16)]
        if changed and toast and overlaps(region, TOAST):
            return [TextBox(text=toast, x=TOAST[0] + 10, y=TOAST[1] + 10, w=130, h=16)]
        return buttons(img, region)

    return read


def make_repaint(region_fn: Any, planned_commits: int = 1, garbled: bool = False) -> tuple[ToolBox, RepaintScreen,
                                                                                            FakeReader, Guard]:
    before = form_footer()
    screen = RepaintScreen(before, with_title_and_toast(before))
    if garbled:
        def read(img: Image.Image, region: Rect) -> list[TextBox]:
            texts = region_fn(img, region)
            return [TextBox(text="errar |", x=b.x, y=b.y, w=b.w, h=b.h) if "Aplicar" in b.text else b for b in texts]
        reader = FakeReader([TextBox(text="Crear cliente", x=14, y=74, w=120, h=16)], region_fn=read)
    else:
        reader = FakeReader([TextBox(text="Crear cliente", x=14, y=74, w=120, h=16)], region_fn=region_fn)
    guard = Guard({"alt+F4"}, planned_commits)
    clock = FakeClock()
    toolbox = ToolBox(screen, reader, FakeSettler(screen), guard, clock, deadline=clock.now + 200, sleep=clock.sleep)
    toolbox.prime(before.copy(), list(reader.boxes))
    return toolbox, screen, reader, guard


@pytest.mark.asyncio
async def test_success_toast_marks_an_unrecognized_click_as_commit() -> None:
    toolbox, screen, _, guard = make_repaint(outcome_reader("Cliente CL-0141", "Cambios aplicados"), garbled=True)
    guard.note_data_entry()
    outcome = await toolbox.execute(call("click", x=864, y=376))
    assert len(guard.commits) == 1
    assert any("Cambios aplicados" in " ".join(e.texts) for e in toolbox.commit_evidence)
    assert "Cambios aplicados" in outcome.text
    # The duplicate the old code allowed: a retry on the same button is refused.
    await toolbox.execute(call("wait", seconds=1))
    retry = await toolbox.execute(call("click", x=864, y=376))
    assert retry.is_error
    assert len([a for a in screen.actions if a[0] == "click"]) == 1


@pytest.mark.asyncio
async def test_heading_with_a_new_code_marks_a_commit() -> None:
    toolbox, _, _, guard = make_repaint(outcome_reader("Cliente CL-0141", None), garbled=True)
    guard.note_data_entry()
    await toolbox.execute(call("click", x=864, y=376))
    assert len(guard.commits) == 1


@pytest.mark.asyncio
async def test_second_apply_on_a_saved_form_is_blocked_even_with_commits_left() -> None:
    toolbox, screen, reader, guard = make_repaint(outcome_reader("Cliente CL-0141", "Cambios aplicados"),
                                                  planned_commits=2)
    guard.note_data_entry()
    await toolbox.execute(call("click", x=864, y=376))
    assert guard.commits == ("Aplicar",)
    await toolbox.execute(call("zoom", x=0, y=60, w=400, h=60))
    reader.boxes = [TextBox(text="Cliente CL-0141", x=14, y=74, w=150, h=16)]
    toolbox.prime(screen.image, list(reader.boxes))
    second = await toolbox.execute(call("click", x=864, y=376))
    assert second.is_error and "CL-0141" in second.text
    assert len([a for a in screen.actions if a[0] == "click"]) == 1


@pytest.mark.asyncio
async def test_plain_navigation_text_is_not_a_commit() -> None:
    toolbox, _, _, guard = make_repaint(outcome_reader("Clientes", None))
    await toolbox.execute(call("click", x=790, y=376))
    assert guard.commits == ()
    assert toolbox.commit_evidence == []


@pytest.mark.asyncio
async def test_weak_commit_that_only_opens_a_new_screen_is_not_counted() -> None:
    before = list_toolbar()
    after = Image.new("RGB", (1280, 800), (200, 200, 200))
    screen = RepaintScreen(before, after)
    reader = FakeReader([], region_fn=label_reader({"Exportar": EXPORTAR, "Crear cliente": CREAR_CLIENTE}))
    guard = Guard({"alt+F4"}, 1)
    guard.note_data_entry()  # e.g. typed into the list filter
    clock = FakeClock()
    toolbox = ToolBox(screen, reader, FakeSettler(screen), guard, clock, deadline=clock.now + 200, sleep=clock.sleep)
    toolbox.prime(before.copy(), [])
    await toolbox.execute(call("click", x=1209, y=185))
    assert guard.commits == ()


@pytest.mark.asyncio
async def test_outcome_reads_are_bounded() -> None:
    before = Image.new("RGB", (1280, 800), WHITE)
    after = before.copy()
    draw = ImageDraw.Draw(after)
    for i in range(10):
        draw.rectangle([20 + i * 120, 600, 80 + i * 120, 620], fill=(0, 0, 0))
    screen = RepaintScreen(before, after)
    reader = FakeReader([], region_fn=lambda img, region: [])
    guard = Guard({"alt+F4"}, 1)
    clock = FakeClock()
    toolbox = ToolBox(screen, reader, FakeSettler(screen), guard, clock, deadline=clock.now + 200, sleep=clock.sleep)
    toolbox.prime(before.copy(), [])
    await toolbox.execute(call("click", x=640, y=300))
    region_reads = [region for region in reader.calls if region is not None]
    assert len(region_reads) <= 1 + 3  # the guard's crop plus at most 3 changed regions


@pytest.mark.asyncio
async def test_dismissing_the_release_modal_is_not_a_commit() -> None:
    image = Image.new("RGB", (1280, 800), (247, 248, 250))
    entendido = (795, 472, 92, 23)
    gradient_button(image, entendido, (89, 161, 126), (50, 125, 89), (36, 96, 63), "Entendido", LIGHT_TEXT)
    after = Image.new("RGB", (1280, 800), PAGE)
    screen = RepaintScreen(image, after)
    reader = FakeReader([], region_fn=label_reader({"Entendido": entendido}))
    guard = Guard({"alt+F4"}, 1)
    clock = FakeClock()
    toolbox = ToolBox(screen, reader, FakeSettler(screen), guard, clock, deadline=clock.now + 200, sleep=clock.sleep)
    toolbox.prime(image.copy(), [])
    await toolbox.execute(call("click", x=840, y=483))
    assert guard.commits == ()


@pytest.mark.asyncio
async def test_trace_event_is_saved_as_a_step() -> None:
    trace = FakeTrace()
    toolbox, _, _, _, _ = make(form_footer(), lambda img, region: [], trace=trace)
    await toolbox.trace_event("cambio de interfaz detectado: v3.9 → v4.2")
    assert trace.steps and trace.steps[0][2] == "cambio de interfaz detectado: v3.9 → v4.2"
    assert trace.steps[0][1]["tool"] == "evento"
