"""LLM tool definitions and their execution against the Screen/Reader ports."""

import asyncio
import base64
import io
import logging
import re
import unicodedata
from collections.abc import Awaitable, Callable, Collection, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import numpy as np
from PIL import Image

from agent.domain.diff import changed_regions
from agent.domain.guard import COMMIT_WORDS, Guard, assigned_codes, label_at, normalize_combo, words
from agent.domain.models import ScriptStep, TextBox, ToolCall
from agent.domain.procedures import normalize
from agent.ports.interfaces import Reader, Screen

log = logging.getLogger(__name__)

SCREEN_W = 1280
SCREEN_H = 800
OCR_CAP = 150
MAX_WAIT_S = 3.0
MAX_SCROLL_STEPS = 10
MAX_FORM_FIELDS = 12
MAX_TEXT_CHARS = 500
ZOOM_FACTOR = 2
EXACT_DIFF_MAX_PIXELS = 20_000
DIFF_BLOCK = 8
DIFF_THRESHOLD = 24
MAX_REGIONS_REPORTED = 8
MAX_RECORDED_STEPS = 15
# Replayable script recorded next to the step texts (procedure memory). Typed text must be one of the
# plan values, recorded by field name; anything else voids the script.
MAX_SCRIPT_STEPS = 40
LABEL_LEFT_MAX_PX = 240  # a field label left of its input, on the same row
LABEL_ABOVE_MAX_PX = 40  # a field label above its input
LABEL_ALIGN_PX = 24  # an above label may start this far left of the input
LABEL_EDGE_TOL_PX = 4
SCRIPT_SKIPPED_COMBOS: frozenset[str] = frozenset({"ctrl+a"})  # selection only; type_into already does it
# Full-screen OCR merges adjacent buttons into one line and misses light-on-dark text, so the guard reads
# only the clicked button: its rectangle is found by color around the click point. A fixed crop read the
# neighbor button too ("Cerrar | Aplicar"), so it is only a fallback, and a narrow one.
BUTTON_MAX_W = 320
BUTTON_MAX_H = 60
BUTTON_MIN_W = 24
BUTTON_MIN_H = 14
BUTTON_STEP_TOL = 10  # max channel difference between neighboring pixels of one button face (gradients)
BUTTON_SEED_TOL = 48  # max channel difference from the sampled face color
BUTTON_SEED_HALF_W = 7
BUTTON_SEED_HALF_H = 3
BUTTON_SEED_SIMILAR = 12
BUTTON_MAX_SEEDS = 6
BUTTON_PAD_PX = 2
# Rereading a typed field: only the input box under the point (a horizontal strip read the neighbor
# input on the same row: NIT reread as "901234567 (31570"), after its delayed rendering settles.
FIELD_MAX_W = 960  # v4.2 "Razón social" spans the form (~873 px)
FIELD_MAX_H = 60
FIELD_STRIP_HALF_W = 160
FIELD_STRIP_HALF_H = 8  # band checked for vertical borders; taller than glyph stems, shorter than inputs
FIELD_STRIP_H = 32
FIELD_POLL_S = 0.1
FIELD_QUIET_S = 0.4
FIELD_MAX_WAIT_S = 2.0
EMPTY_FIELD = "(vacío o ilegible)"
FIELD_MATCH = "coincide con lo escrito"
REREAD_HINT = (
    "Si un campo no coincide, puede ser un error de OCR: usa zoom sobre ese campo antes de reescribirlo. "
    "Para reescribirlo usa type_into con replace=true (replace=false añade el texto al final)."
)
GUARD_CROP_W = 100
GUARD_CROP_H = 28
# Commits repaint slowly (title and toast after ~1.5 s), so their observation waits longer.
COMMIT_SETTLE: dict[str, float] = {"quiet": 0.4, "min_wait": 1.8, "max_wait": 3.5}
# After any other click, a few small changed regions are read to notice a save the guard did not see.
MAX_OUTCOME_REGIONS = 3
MAX_OUTCOME_AREA = SCREEN_W * SCREEN_H // 8
MIN_OUTCOME_W = 16
MIN_OUTCOME_H = 8
MAX_NOTICE_WORDS = 8
MAX_HEADING_WORDS = 4
OUTCOME_TARGET_HALF = 12  # area remembered around a click whose button could not be delimited
NAVIGATION_AREA = SCREEN_W * SCREEN_H // 4  # a change this large replaced the screen (menu, list, form)
# Clicks whose own label says they only search or close never count as commits by their outcome.
NO_OUTCOME_WORDS: frozenset[str] = frozenset({"buscar", "filtrar", "limpiar", "consultar"})
# After a commit, the text of what changed (toasts, dialogs) is read before it disappears.
MAX_EVIDENCE_REGIONS = 6
MAX_EVIDENCE_AREA = SCREEN_W * SCREEN_H // 2
EVIDENCE_PAD_PX = 4
MAX_EVIDENCE_CHARS = 300
# A success notice that appears in a changed region is evidence even when OCR garbled the button and
# the guard did not recognize the click as a commit (e.g. "Aplicar" read as "Apkcar").
SUCCESS_NOTICE_RE = re.compile(
    r"\b(guardad|cread|actualizad|aplicad|registrad|eliminad|anulad|exportad|exitos|correctamente)",
    re.IGNORECASE,
)
NOTICE_LABEL = "aviso de éxito"

_COORDS = {
    "x": {"type": "integer", "description": "Horizontal pixel, 0-1279, from the left edge."},
    "y": {"type": "integer", "description": "Vertical pixel, 0-799, from the top edge."},
}

TOOL_SPECS: list[dict[str, Any]] = [
    {
        "name": "click",
        "description": "Click at an absolute screen pixel. Clicks on buttons that save, confirm or delete are "
        "checked by a guard and may be refused.",
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                **_COORDS,
                "button": {"type": "string", "enum": ["left", "right", "middle"]},
                "double": {"type": "boolean", "description": "Double click."},
            },
            "required": ["x", "y"],
            "additionalProperties": False,
        },
    },
    {
        "name": "type_into",
        "description": "Click a text field, select its current content (when replace is true) and type text. "
        "The field is read back afterwards and compared with the text. replace=false appends to the current "
        "content and is refused when the field already holds the text.",
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                **_COORDS,
                "text": {"type": "string"},
                "replace": {"type": "boolean", "description": "Select all before typing. Default true."},
            },
            "required": ["x", "y", "text"],
            "additionalProperties": False,
        },
    },
    {
        "name": "key",
        "description": "Press a key or combo, e.g. 'enter', 'tab', 'escape', 'ctrl+a'. Combos that close or "
        "reload the app are refused.",
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {"combo": {"type": "string"}},
            "required": ["combo"],
            "additionalProperties": False,
        },
    },
    {
        "name": "scroll",
        "description": "Scroll the mouse wheel over a point (1-10 steps).",
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                **_COORDS,
                "direction": {"type": "string", "enum": ["up", "down"]},
                "steps": {"type": "integer", "description": "1-10."},
            },
            "required": ["x", "y", "direction", "steps"],
            "additionalProperties": False,
        },
    },
    {
        "name": "wait",
        "description": "Wait up to 3 seconds, then look at the screen again.",
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {"seconds": {"type": "number", "description": "0-3."}},
            "required": ["seconds"],
            "additionalProperties": False,
        },
    },
    {
        "name": "zoom",
        "description": "Magnify a screen rectangle 2x and read its text. Use it to read small text or to "
        "verify a value without acting.",
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                **_COORDS,
                "w": {"type": "integer", "description": "Width in pixels (8 or more)."},
                "h": {"type": "integer", "description": "Height in pixels (8 or more)."},
            },
            "required": ["x", "y", "w", "h"],
            "additionalProperties": False,
        },
    },
    {
        "name": "fill_form",
        "description": "Fill several text fields in one turn: for each one, click it, select all and type the "
        "value. Never use it on buttons.",
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "fields": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {**_COORDS, "value": {"type": "string"}},
                        "required": ["x", "y", "value"],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["fields"],
            "additionalProperties": False,
        },
    },
    {
        "name": "finish",
        "description": "End the task. After this no more actions are possible. estado 'terminada' only when the "
        "screen shows the task done exactly as asked; otherwise 'fallida'. Write resumen in Spanish.",
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "estado": {"type": "string", "enum": ["terminada", "fallida"]},
                "resumen": {"type": "string", "description": "1-3 short sentences in Spanish."},
                "respuesta": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Answer value(s) to a question, in the order asked. Omit if not a question "
                    "or if the answer could not be found.",
                },
                "datos_razon": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Concrete data seen on screen that explain why the task is impossible "
                    "(ids, names, values).",
                },
                "aprendizaje": {"type": "string"},
            },
            "required": ["estado", "resumen"],
            "additionalProperties": False,
        },
    },
]


class ToolInputError(ValueError):
    """Invalid tool input; the message is shown to the model."""


class TraceSink(Protocol):
    def save_step(self, n: int, image: Image.Image, action: dict[str, Any], note: str) -> Path: ...


class Settles(Protocol):
    async def settle(
        self, since: float, quiet: float = ..., min_wait: float = ..., max_wait: float = ...
    ) -> tuple[Image.Image, bool]: ...


@dataclass(frozen=True, slots=True)
class ToolOutcome:
    text: str
    image: Image.Image | None = None
    is_error: bool = False
    finish: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class CommitEvidence:
    """What the screen showed right after a commit: text of the changed regions and the screenshot."""

    label: str
    texts: tuple[str, ...]
    image: Image.Image


def guard_crop(x: int, y: int) -> tuple[int, int, int, int]:
    """GUARD_CROP_W x GUARD_CROP_H rectangle centered on the point, clamped to the screen."""
    left = min(max(0, x - GUARD_CROP_W // 2), SCREEN_W - GUARD_CROP_W)
    top = min(max(0, y - GUARD_CROP_H // 2), SCREEN_H - GUARD_CROP_H)
    return left, top, GUARD_CROP_W, GUARD_CROP_H


def _face_seeds(window: np.ndarray, px: int, py: int) -> list[tuple[int, int]]:
    """Pixels near the click with the most common smooth color around it (the button face, not its text),
    best first: the nearest one may sit inside a letter, so callers try several."""
    height, width, _ = window.shape
    top, bottom = max(0, py - BUTTON_SEED_HALF_H), min(height, py + BUTTON_SEED_HALF_H + 1)
    left, right = max(0, px - BUTTON_SEED_HALF_W), min(width, px + BUTTON_SEED_HALF_W + 1)
    patch = window[top:bottom, left:right].reshape(-1, 3)
    # How many patch pixels look like each pixel; the face wins, thin text strokes lose.
    similar = (np.max(np.abs(patch[:, None, :] - patch[None, :, :]), axis=2) <= BUTTON_SEED_SIMILAR).sum(axis=1)
    # Text and its anti-aliased edges are not smooth: some neighbor differs a lot.
    padded = np.pad(window, ((1, 1), (1, 1), (0, 0)), mode="edge")
    center = padded[1:-1, 1:-1]
    steps = np.stack(
        [np.max(np.abs(center - padded[1 + dy:height + 1 + dy, 1 + dx:width + 1 + dx]), axis=2)
         for dy, dx in ((-1, 0), (1, 0), (0, -1), (0, 1))]
    ).max(axis=0)
    smooth = (steps[top:bottom, left:right] <= BUTTON_STEP_TOL).reshape(-1)
    ys, xs = np.divmod(np.arange(patch.shape[0]), right - left)
    distance = (ys + top - py) ** 2 + (xs + left - px) ** 2
    order = np.lexsort((distance, -similar, ~smooth))
    face = patch[order[0]]
    same = np.max(np.abs(patch - face), axis=1) <= BUTTON_SEED_SIMILAR
    return [(int(xs[i]) + left, int(ys[i]) + top) for i in order if same[i] and smooth[i]] or [
        (int(xs[order[0]]) + left, int(ys[order[0]]) + top)
    ]


def _grow_face(
    window: np.ndarray, sx: int, sy: int, max_w: int = BUTTON_MAX_W, max_h: int = BUTTON_MAX_H
) -> np.ndarray | None:
    """Pixels connected to the seed through small color steps; None when it outgrows max_w x max_h."""
    face = np.max(np.abs(window - window[sy, sx]), axis=2) <= BUTTON_SEED_TOL
    h_ok = (np.max(np.abs(window[:, 1:] - window[:, :-1]), axis=2) <= BUTTON_STEP_TOL) & face[:, 1:] & face[:, :-1]
    v_ok = (np.max(np.abs(window[1:] - window[:-1]), axis=2) <= BUTTON_STEP_TOL) & face[1:] & face[:-1]
    reached = np.zeros(face.shape, dtype=bool)
    reached[sy, sx] = True
    for step in range(2 * (max_w + max_h)):
        grown = reached.copy()
        grown[:, 1:] |= reached[:, :-1] & h_ok
        grown[:, :-1] |= reached[:, 1:] & h_ok
        grown[1:] |= reached[:-1] & v_ok
        grown[:-1] |= reached[1:] & v_ok
        if np.array_equal(grown, reached):
            break
        reached = grown
        if step % 16 == 0:
            cols, rows = np.nonzero(reached.any(axis=0))[0], np.nonzero(reached.any(axis=1))[0]
            if cols[-1] - cols[0] + 1 > max_w or rows[-1] - rows[0] + 1 > max_h:
                return None
    return reached


def button_bounds(image: Image.Image, x: int, y: int) -> tuple[int, int, int, int] | None:
    """Rectangle (x, y, w, h) of the button under the point, found by its face color; None if unclear.

    The face is grown from a seed pixel through neighbors whose color steps stay small (vertical gradients
    pass, borders and text edges stop it), so text inside the button is skipped and a neighbor button
    separated by a gap or a border is never reached. Faces smaller than BUTTON_MIN_* or larger than
    BUTTON_MAX_* (page backgrounds, banners) are not buttons.
    """
    left, top = max(0, x - BUTTON_MAX_W), max(0, y - BUTTON_MAX_H)
    right, bottom = min(image.width, x + BUTTON_MAX_W + 1), min(image.height, y + BUTTON_MAX_H + 1)
    window = np.asarray(image.convert("RGB").crop((left, top, right, bottom)), dtype=np.int16)
    tried = np.zeros(window.shape[:2], dtype=bool)
    attempts = 0
    for sx, sy in _face_seeds(window, x - left, y - top):
        if tried[sy, sx]:
            continue
        if attempts == BUTTON_MAX_SEEDS:
            break
        attempts += 1
        reached = _grow_face(window, sx, sy)
        if reached is None:
            return None  # the face color is the page background: not a button
        cols, rows = np.nonzero(reached.any(axis=0))[0], np.nonzero(reached.any(axis=1))[0]
        width, height = int(cols[-1] - cols[0] + 1), int(rows[-1] - rows[0] + 1)
        if BUTTON_MIN_W <= width <= BUTTON_MAX_W and BUTTON_MIN_H <= height <= BUTTON_MAX_H:
            return left + int(cols[0]), top + int(rows[0]), width, height
        tried |= reached  # trapped inside a letter: try a seed outside it
    return None


def _field_face(image: Image.Image, x: int, y: int) -> tuple[int, int, int, int] | None:
    """Rectangle of the input box under the point: its face grown up to its border; None if unclear."""
    left, top = max(0, x - FIELD_MAX_W), max(0, y - FIELD_MAX_H)
    right, bottom = min(image.width, x + FIELD_MAX_W + 1), min(image.height, y + FIELD_MAX_H + 1)
    window = np.asarray(image.convert("RGB").crop((left, top, right, bottom)), dtype=np.int16)
    tried = np.zeros(window.shape[:2], dtype=bool)
    attempts = 0
    for sx, sy in _face_seeds(window, x - left, y - top):
        if tried[sy, sx]:
            continue
        if attempts == BUTTON_MAX_SEEDS:
            break
        attempts += 1
        reached = _grow_face(window, sx, sy, FIELD_MAX_W, FIELD_MAX_H)
        if reached is None:
            return None  # no closed box around the point (page background)
        cols, rows = np.nonzero(reached.any(axis=0))[0], np.nonzero(reached.any(axis=1))[0]
        width, height = int(cols[-1] - cols[0] + 1), int(rows[-1] - rows[0] + 1)
        if BUTTON_MIN_W <= width <= FIELD_MAX_W and BUTTON_MIN_H <= height <= FIELD_MAX_H:
            return left + int(cols[0]), top + int(rows[0]), width, height
        tried |= reached  # trapped inside a letter: try a seed outside it
    return None


def _field_strip(image: Image.Image, x: int, y: int) -> tuple[int, int, int, int]:
    """Fallback reread area: a strip at most FIELD_STRIP_HALF_W to each side, cut at the nearest
    vertical border (a column that differs from the background over the whole band around y)."""
    top, bottom = max(0, y - FIELD_STRIP_HALF_H), min(image.height, y + FIELD_STRIP_HALF_H + 1)
    left, right = max(0, x - FIELD_STRIP_HALF_W), min(image.width, x + FIELD_STRIP_HALF_W + 1)
    band = np.asarray(image.convert("RGB").crop((left, top, right, bottom)), dtype=np.int16)
    background = np.median(band.reshape(-1, 3), axis=0)
    border = (np.max(np.abs(band - background), axis=2) >= DIFF_THRESHOLD).all(axis=0)
    px = x - left
    walls_left = np.nonzero(border[:px])[0]
    walls_right = np.nonzero(border[px + 1:])[0]
    start = left + int(walls_left[-1]) + 1 if walls_left.size else left
    end = left + px + 1 + int(walls_right[0]) if walls_right.size else right
    strip_top = max(0, y - FIELD_STRIP_H // 2)
    return start, strip_top, end - start, min(FIELD_STRIP_H, image.height - strip_top)


def field_region(image: Image.Image, x: int, y: int) -> tuple[int, int, int, int]:
    """Area to OCR when rereading the text field under the point: the input box itself when it can be
    delimited (so a neighbor input on the same row is never read), else a narrow strip."""
    rect = _field_face(image, x, y)
    if rect is not None and _contains(rect, x, y):
        return _pad(rect, BUTTON_PAD_PX)
    return _field_strip(image, x, y)


def _comparable(text: str) -> str:
    """Text reduced for comparing a reread with what was typed: no case, accents, spaces or punctuation."""
    return re.sub(r"[\W_]+", "", _fold(text).casefold())


def same_text(read: str, intended: str) -> bool:
    return bool(_comparable(intended)) and _comparable(read) == _comparable(intended)


def _contains(rect: tuple[int, int, int, int], x: int, y: int) -> bool:
    return rect[0] <= x < rect[0] + rect[2] and rect[1] <= y < rect[1] + rect[3]


def _pad(rect: tuple[int, int, int, int], pad: int) -> tuple[int, int, int, int]:
    x, y, w, h = rect
    left, top = max(0, x - pad), max(0, y - pad)
    return left, top, min(SCREEN_W, x + w + pad) - left, min(SCREEN_H, y + h + pad) - top


def _word_count(text: str) -> int:
    return len(re.findall(r"\w+", text))


def is_success_text(text: str) -> bool:
    """A short notice such as "Cambios aplicados" or "Cliente CL-0007 guardado"."""
    return _word_count(text) <= MAX_NOTICE_WORDS and bool(SUCCESS_NOTICE_RE.search(_fold(text)))


def new_heading_code(texts: Sequence[str], before: Sequence[TextBox]) -> str | None:
    """A record code that appeared in a short changed text (a title like "Cliente CL-0141")."""
    known = {code for box in before for code in assigned_codes(box.text)}
    for text in texts:
        if _word_count(text) > MAX_HEADING_WORDS:
            continue
        fresh = sorted(assigned_codes(text) - known)
        if fresh:
            return fresh[0]
    return None


def evidence_regions(regions: Sequence[tuple[int, int, int, int]]) -> list[tuple[int, int, int, int]]:
    """Changed regions worth reading after a commit: not huge, largest first, padded and clamped."""
    small = [r for r in regions if r[2] * r[3] <= MAX_EVIDENCE_AREA]
    small.sort(key=lambda r: r[2] * r[3], reverse=True)
    padded = []
    for x, y, w, h in small[:MAX_EVIDENCE_REGIONS]:
        left = max(0, x - EVIDENCE_PAD_PX)
        top = max(0, y - EVIDENCE_PAD_PX)
        right = min(SCREEN_W, x + w + EVIDENCE_PAD_PX)
        bottom = min(SCREEN_H, y + h + EVIDENCE_PAD_PX)
        padded.append((left, top, right - left, bottom - top))
    return padded


def _fold(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).lower()


def _inside(box: TextBox, region: tuple[int, int, int, int]) -> bool:
    cx, cy = box.center
    x, y, w, h = region
    return x - EVIDENCE_PAD_PX <= cx < x + w + EVIDENCE_PAD_PX and y - EVIDENCE_PAD_PX <= cy < y + h + EVIDENCE_PAD_PX


def new_success_notices(
    before: Sequence[TextBox], after: Sequence[TextBox], regions: Sequence[tuple[int, int, int, int]]
) -> list[str]:
    """Success-message texts that were not on screen before and lie inside a changed region."""
    seen = {_fold(box.text).strip() for box in before}
    notices: list[str] = []
    for box in sorted(after, key=lambda b: (b.y, b.x)):
        folded = _fold(box.text).strip()
        if folded in seen or not SUCCESS_NOTICE_RE.search(folded):
            continue
        if any(_inside(box, region) for region in regions) and box.text not in notices:
            notices.append(box.text[:MAX_EVIDENCE_CHARS])
    return notices


def image_block(image: Image.Image) -> dict[str, Any]:
    """PNG image as a Messages API content block (plain data)."""
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=False)
    data = base64.standard_b64encode(buffer.getvalue()).decode("ascii")
    return {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": data}}


def ocr_listing(boxes: Sequence[TextBox], cap: int = OCR_CAP) -> str:
    """Compact 'texto @x,y' lines (box centers) in reading order."""
    ordered = sorted(boxes, key=lambda box: (box.y, box.x))
    lines = [f"{box.text} @{box.center[0]},{box.center[1]}" for box in ordered[:cap]]
    if len(ordered) > cap:
        lines.append(f"(+{len(ordered) - cap} omitidos)")
    return "\n".join(lines) if lines else "(sin texto reconocido)"


def screen_regions(prev: Image.Image, curr: Image.Image) -> list[tuple[int, int, int, int]]:
    """Changed rectangles; large changes are grouped on an 8 px grid to stay fast."""
    if prev.size != curr.size:
        return [(0, 0, curr.width, curr.height)]
    before = np.asarray(prev.convert("RGB"), dtype=np.int16)
    after = np.asarray(curr.convert("RGB"), dtype=np.int16)
    mask = np.any(np.abs(after - before) >= DIFF_THRESHOLD, axis=2)
    count = int(mask.sum())
    if count == 0:
        return []
    if count <= EXACT_DIFF_MAX_PIXELS:
        return changed_regions(prev, curr, DIFF_THRESHOLD)
    height, width = mask.shape
    rows = -(-height // DIFF_BLOCK)
    cols = -(-width // DIFF_BLOCK)
    padded = np.zeros((rows * DIFF_BLOCK, cols * DIFF_BLOCK), dtype=bool)
    padded[:height, :width] = mask
    blocks = padded.reshape(rows, DIFF_BLOCK, cols, DIFF_BLOCK).any(axis=(1, 3))
    small_after = Image.fromarray((blocks * 255).astype(np.uint8)).convert("RGB")
    small_before = Image.new("RGB", small_after.size)
    regions = []
    for x, y, w, h in changed_regions(small_before, small_after, 1):
        rx, ry = x * DIFF_BLOCK, y * DIFF_BLOCK
        regions.append((rx, ry, min(w * DIFF_BLOCK, width - rx), min(h * DIFF_BLOCK, height - ry)))
    return regions


def field_label_box(
    boxes: Sequence[TextBox], region: tuple[int, int, int, int], ignore: Collection[str] = ()
) -> TextBox | None:
    """Nearest text left of the input on its row, or above it and aligned with it: the field's label.

    `ignore` holds normalized texts that are field contents, not labels (values already typed)."""
    rx, ry, rw, rh = region
    best: tuple[int, TextBox] | None = None
    for box in boxes:
        if not box.text.strip() or normalize(box.text) in ignore or _contains(region, *box.center):
            continue
        right, bottom = box.x + box.w, box.y + box.h
        distance: int | None = None
        if right <= rx + LABEL_EDGE_TOL_PX and box.y < ry + rh and bottom > ry:
            distance = rx - right
            if distance > LABEL_LEFT_MAX_PX:
                distance = None
        elif bottom <= ry + LABEL_EDGE_TOL_PX and rx - LABEL_ALIGN_PX <= box.x < rx + rw:
            distance = ry - bottom
            if distance > LABEL_ABOVE_MAX_PX:
                distance = None
        if distance is not None and (best is None or distance < best[0]):
            best = (distance, box)
    return best[1] if best else None


def _int(args: dict[str, Any], name: str, low: int, high: int) -> int:
    value = args.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ToolInputError(f"'{name}' debe ser un entero entre {low} y {high}")
    if not low <= value <= high:
        raise ToolInputError(f"'{name}'={value} fuera de rango ({low}-{high})")
    return value


def _point(args: dict[str, Any]) -> tuple[int, int]:
    return _int(args, "x", 0, SCREEN_W - 1), _int(args, "y", 0, SCREEN_H - 1)


def _text(args: dict[str, Any], name: str) -> str:
    value = args.get(name)
    if not isinstance(value, str):
        raise ToolInputError(f"'{name}' debe ser texto")
    if len(value) > MAX_TEXT_CHARS:
        raise ToolInputError(f"'{name}' es demasiado largo (máximo {MAX_TEXT_CHARS} caracteres)")
    return value


def _bool(args: dict[str, Any], name: str, default: bool) -> bool:
    value = args.get(name, default)
    if not isinstance(value, bool):
        raise ToolInputError(f"'{name}' debe ser true o false")
    return value


def _strings(args: dict[str, Any], name: str) -> list[str]:
    value = args.get(name, [])
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ToolInputError(f"'{name}' debe ser una lista de textos")
    return value


class ToolBox:
    """Executes tool calls: guard first, then the action, then a settled observation."""

    def __init__(
        self,
        screen: Screen,
        reader: Reader,
        settler: Settles,
        guard: Guard,
        clock: Callable[[], float],
        trace: TraceSink | None = None,
        deadline: float | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        plan_values: Sequence[tuple[str, str]] = (),
    ) -> None:
        self._screen = screen
        self._reader = reader
        self._settler = settler
        self._guard = guard
        self._clock = clock
        self._trace = trace
        self.deadline = deadline
        self._sleep = sleep
        self._image: Image.Image | None = None
        self._boxes: list[TextBox] = []
        self._commit_evidence: list[CommitEvidence] = []
        self._last_target: tuple[int, int, int, int] | None = None
        self._outcome_targets: list[tuple[int, int, int, int]] = []
        self.steps: list[str] = []
        self._step = 0
        self._plan_values: tuple[tuple[str, str], ...] = ()
        self.set_plan_values(plan_values)
        self._script: list[ScriptStep] = []
        self._script_ok = True
        self.last_reread: tuple[bool, ...] = ()  # one flag per field typed by the last type_into/fill_form
        self._handlers: dict[str, Callable[[dict[str, Any]], Awaitable[ToolOutcome]]] = {
            "click": self._click,
            "type_into": self._type_into,
            "key": self._key,
            "scroll": self._scroll,
            "wait": self._wait,
            "zoom": self._zoom,
            "fill_form": self._fill_form,
            "finish": self._finish,
        }

    @property
    def image(self) -> Image.Image | None:
        return self._image

    @property
    def boxes(self) -> list[TextBox]:
        return list(self._boxes)

    @property
    def commit_evidence(self) -> list[CommitEvidence]:
        return list(self._commit_evidence)

    @property
    def script(self) -> tuple[ScriptStep, ...]:
        """Replayable script of what was done; empty when something could not be recorded safely."""
        return tuple(self._script) if self._script_ok else ()

    def set_plan_values(self, values: Sequence[tuple[str, str]]) -> None:
        """Plan (field, value) pairs: typed text is recorded in the script by its field name."""
        self._plan_values = tuple((str(name), str(value)) for name, value in values)

    def keep_script_step(self, step: ScriptStep) -> None:
        """Keep a step that was not executed (a dismissal whose dialog was absent) in the script."""
        self._add_script(step)

    async def target_label(self, x: int, y: int) -> str:
        """Label the guard would read for a click at the point, without clicking."""
        return label_at(x, y, await self._target_boxes(x, y))

    def prime(self, image: Image.Image, boxes: list[TextBox]) -> None:
        self._image = image
        self._boxes = list(boxes)

    async def observe(self) -> tuple[Image.Image, list[TextBox]]:
        """Fresh screenshot and full OCR (no action)."""
        image = await self._screen.screenshot()
        boxes = await self._reader.read(image)
        self.prime(image, boxes)
        return image, list(boxes)

    async def settle_and_observe(self) -> tuple[Image.Image, list[TextBox]]:
        image, _ = await self._settler.settle(self._clock())
        boxes = await self._reader.read(image)
        self.prime(image, boxes)
        return image, list(boxes)

    def remaining(self) -> float | None:
        return None if self.deadline is None else self.deadline - self._clock()

    async def execute(self, call: ToolCall) -> ToolOutcome:
        handler = self._handlers.get(call.name)
        if handler is None:
            return ToolOutcome(text=f"Herramienta desconocida: {call.name}", is_error=True)
        args = call.input if isinstance(call.input, dict) else {}
        try:
            outcome = await handler(args)
        except ToolInputError as exc:
            outcome = ToolOutcome(text=f"Entrada inválida para {call.name}: {exc}", is_error=True)
        except ValueError as exc:
            outcome = ToolOutcome(text=f"No se pudo ejecutar {call.name}: {exc}", is_error=True)
        await self._save_trace(call, outcome)
        return outcome

    async def _save_trace(self, call: ToolCall, outcome: ToolOutcome) -> None:
        if self._trace is None:
            return
        self._step += 1
        image = outcome.image or self._image or Image.new("RGB", (SCREEN_W, SCREEN_H))
        action = {"tool": call.name, "input": call.input, "is_error": outcome.is_error}
        try:
            await asyncio.to_thread(self._trace.save_step, self._step, image, action, outcome.text[:2000])
        except OSError:
            log.exception("no se pudo guardar el paso %d de la traza", self._step)

    # --- observation -------------------------------------------------------------------------

    async def _observe_after(self, since: float, note: str) -> tuple[ToolOutcome, bool]:
        image, changed = await self._settler.settle(since)
        return await self._observation(image, note, changed), changed

    async def _observation(
        self,
        image: Image.Image,
        note: str,
        changed: bool | None = None,
        evidence_taken: bool = False,
        regions: list[tuple[int, int, int, int]] | None = None,
        boxes: list[TextBox] | None = None,
    ) -> ToolOutcome:
        """Observation text for the model; `regions`/`boxes` may be passed when already computed."""
        if regions is None:
            previous = self._image
            regions = await asyncio.to_thread(screen_regions, previous, image) if previous is not None else []
        if boxes is None:
            boxes = await self._reader.read(image)
        if not evidence_taken:
            notices = new_success_notices(self._boxes, boxes, regions)
            if notices:
                self._commit_evidence.append(CommitEvidence(label=NOTICE_LABEL, texts=tuple(notices), image=image))
        self.prime(image, boxes)
        if changed is None:
            changed = bool(regions)
        lines = [note]
        if regions:
            shown = " ".join(f"({x},{y},{w},{h})" for x, y, w, h in regions[:MAX_REGIONS_REPORTED])
            extra = f" y {len(regions) - MAX_REGIONS_REPORTED} más" if len(regions) > MAX_REGIONS_REPORTED else ""
            lines.append(f"Regiones cambiadas (x,y,w,h): {shown}{extra}")
        else:
            lines.append("La pantalla no cambió." if not changed else "La pantalla cambió.")
        lines.append(self._status_line())
        lines.append("Texto en pantalla (texto @x,y):")
        lines.append(ocr_listing(boxes))
        return ToolOutcome(text="\n".join(lines), image=image)

    def _status_line(self) -> str:
        remaining = self.remaining()
        time_part = f"Tiempo restante: {max(0, int(remaining))} s" if remaining is not None else "Tiempo restante: ?"
        commits = self._guard.commits
        done = f" ({', '.join(commits)})" if commits else ""
        return f"{time_part}. Confirmaciones hechas: {len(commits)} de {self._guard.planned_commits}{done}."

    async def _field_regions(self, points: Sequence[tuple[int, int]]) -> list[tuple[int, int, int, int]]:
        """Input box under each point, delimited on the screen as it was before typing."""
        image = self._image or await self._screen.screenshot()
        return [await asyncio.to_thread(field_region, image, x, y) for x, y in points]

    async def _read_field(self, image: Image.Image, region: tuple[int, int, int, int]) -> str:
        boxes = await self._reader.read(image, region)
        text = " ".join(b.text.strip() for b in sorted(boxes, key=lambda b: (b.y, b.x)) if b.text.strip())
        return text or EMPTY_FIELD

    async def _await_fields(
        self, image: Image.Image, regions: Sequence[tuple[int, int, int, int]], before: Image.Image | None
    ) -> Image.Image:
        """Latest screenshot once the typed fields stop changing (v4.2 renders typed text late).

        Fields still identical to the screen before typing have not rendered yet, so they keep the wait
        going; the wait ends after FIELD_QUIET_S without changes or after FIELD_MAX_WAIT_S in total.
        """

        def crops(img: Image.Image) -> list[bytes]:
            return [img.crop((x, y, x + w, y + h)).tobytes() for x, y, w, h in regions]

        original = crops(before) if before is not None and before.size == image.size else None
        current = crops(image)
        start = stable_since = self._clock()
        while self._clock() - start + FIELD_POLL_S <= FIELD_MAX_WAIT_S + 1e-9:
            rendered = original is None or all(c != o for c, o in zip(current, original))
            if rendered and self._clock() - stable_since >= FIELD_QUIET_S:
                break
            await self._sleep(FIELD_POLL_S)
            latest = await self._screen.screenshot()
            fresh = crops(latest) if latest.size == image.size else current
            if fresh != current:
                stable_since = self._clock()
                current = fresh
            image = latest
        return image

    @staticmethod
    def _reread_note(read_back: str, intended: str) -> str:
        if same_text(read_back, intended):
            return f"'{read_back}' ({FIELD_MATCH})"
        return f"'{read_back}' (no coincide con lo escrito '{intended}'; puede ser un error de OCR)"

    async def _target_boxes(self, x: int, y: int) -> Sequence[TextBox]:
        """Boxes the guard judges a click with: the clicked button only, else a narrow crop, else full OCR."""
        self._last_target = None
        if self._image is None:
            return self._boxes
        rect = await asyncio.to_thread(button_bounds, self._image, x, y)
        self._last_target = rect
        if rect is not None:
            region = _pad(rect, BUTTON_PAD_PX)
            boxes = await self._reader.read(self._image, region)
            text = " ".join(b.text.strip() for b in sorted(boxes, key=lambda b: (b.y, b.x)) if b.text.strip())
            if text:
                return [TextBox(text=text, x=region[0], y=region[1], w=region[2], h=region[3])]
        crop = await self._reader.read(self._image, guard_crop(x, y))
        if crop and label_at(x, y, crop):
            return crop
        return self._boxes

    def _visible_codes(self) -> set[str]:
        return {code for box in self._boxes for code in assigned_codes(box.text)}

    async def _read_regions(self, image: Image.Image, regions: Sequence[tuple[int, int, int, int]]) -> list[str]:
        texts: list[str] = []
        for region in regions:
            boxes = await self._reader.read(image, region)
            text = " ".join(box.text for box in sorted(boxes, key=lambda b: (b.y, b.x))).strip()
            if text and text not in texts:
                texts.append(text[:MAX_EVIDENCE_CHARS])
        return texts

    def _outcome_regions(
        self, regions: Sequence[tuple[int, int, int, int]], x: int, y: int
    ) -> list[tuple[int, int, int, int]]:
        """Small changed regions worth reading after a non-commit click; the clicked button itself is skipped."""
        picked = [
            r for r in regions
            if MIN_OUTCOME_W <= r[2] and MIN_OUTCOME_H <= r[3] and r[2] * r[3] <= MAX_OUTCOME_AREA
            and not (r[0] <= x < r[0] + r[2] and r[1] <= y < r[1] + r[3] and r[2] <= BUTTON_MAX_W)
        ]
        picked.sort(key=lambda r: r[2] * r[3], reverse=True)
        return evidence_regions(picked[:MAX_OUTCOME_REGIONS])

    def _keep_evidence(self, label: str, texts: Sequence[str], image: Image.Image) -> str:
        self._commit_evidence.append(CommitEvidence(label=label, texts=tuple(texts), image=image))
        return f"\nMensajes tras confirmar: {' | '.join(texts)}" if texts else ""

    # --- handlers ----------------------------------------------------------------------------

    def _record_step(self, step: str) -> None:
        """Navigation log for procedure memory: no typed values, consecutive repeats collapsed, capped."""
        if self.steps and self.steps[-1] == step:
            return
        if len(self.steps) < MAX_RECORDED_STEPS:
            self.steps.append(step)

    def _add_script(self, step: ScriptStep) -> None:
        if len(self._script) >= MAX_SCRIPT_STEPS:
            self._void_script("too many steps")
            return
        self._script.append(step)

    def _void_script(self, why: str) -> None:
        if self._script_ok:
            log.info("no se guardará un guion reproducible: %s", why)
        self._script_ok = False

    def _field_name(self, text: str) -> str | None:
        wanted = normalize(text)
        return next((name for name, value in self._plan_values if value and normalize(value) == wanted), None)

    def _record_fills(
        self, fields: Sequence[tuple[int, int, str]], regions: Sequence[tuple[int, int, int, int]]
    ) -> None:
        """Fill steps for typed fields, located by their label; voids the script if one cannot be."""
        typed = {normalize(value) for _, value in self._plan_values if value}
        for (x, y, text), region in zip(fields, regions):
            name = self._field_name(text)
            label = field_label_box(self._boxes, region, typed)
            if name is None or label is None:
                self._void_script("typed text outside the plan" if name is None else "field without a label")
                return
            lx, ly = label.center
            self._add_script(
                ScriptStep(kind="fill", field_label=label.text.strip(), field=name, x=x, y=y, dx=x - lx, dy=y - ly)
            )

    def _blocked(self, reason: str) -> ToolOutcome:
        return ToolOutcome(text=f"{reason}\n{self._status_line()}", is_error=True)

    async def _click(self, args: dict[str, Any]) -> ToolOutcome:
        x, y = _point(args)
        button = args.get("button", "left")
        if button not in ("left", "right", "middle"):
            raise ToolInputError("'button' debe ser left, right o middle")
        double = _bool(args, "double", False)
        verdict = self._guard.check_click(x, y, await self._target_boxes(x, y), visible_codes=self._visible_codes())
        half = OUTCOME_TARGET_HALF
        button_area = self._last_target or (x - half, y - half, 2 * half, 2 * half)
        if not verdict.is_commit and any(_contains(r, x, y) for r in self._outcome_targets):
            # Same button that saved before although its label is unreadable: apply the commit limits.
            verdict = self._guard.check_commit(NOTICE_LABEL, self._visible_codes())
        if not verdict.allowed:
            return self._blocked(verdict.reason)
        before_boxes = list(self._boxes)
        since = self._clock()
        await self._screen.click(x, y, button, double)
        if verdict.label:
            self._record_step(f"click '{verdict.label}'")
        if not verdict.label.strip() or button != "left" or double:
            self._void_script("click without a readable label")
        else:
            self._add_script(ScriptStep(kind="click", anchor=verdict.label.strip(), x=x, y=y,
                                        commit=verdict.is_commit, dismissal=verdict.dismissal))
        target = f" sobre '{verdict.label}'" if verdict.label else ""
        note = f"Clic en ({x},{y}){target}."
        if verdict.is_commit:
            image, changed = await self._settler.settle(since, **COMMIT_SETTLE)
        else:
            image, changed = await self._settler.settle(since)
        previous = self._image
        regions = await asyncio.to_thread(screen_regions, previous, image) if previous is not None else []
        navigation = sum(w * h for _, _, w, h in regions) >= NAVIGATION_AREA
        may_have_saved = not verdict.dismissal and not words(verdict.label) & NO_OUTCOME_WORDS
        if verdict.is_commit:
            texts = await self._read_regions(image, evidence_regions(regions))
        elif may_have_saved:
            texts = await self._read_regions(image, self._outcome_regions(regions, x, y))
        else:
            texts = []
        boxes = await self._reader.read(image)
        notices = [t for t in texts if is_success_text(t)]
        notices += [n for n in new_success_notices(before_boxes, boxes, regions) if n not in notices]
        code = new_heading_code(texts, before_boxes)
        evidence_taken = False
        if verdict.is_commit and verdict.weak and navigation and not notices and code is None:
            # "Crear"/"Nuevo" that just replaced the screen opened a form; it saved nothing.
            self._guard.note_navigation()
            note += " Se abrió otra pantalla; no cuenta como confirmación."
        elif verdict.is_commit:
            self._guard.record_commit(verdict.label)
            note += " Acción de confirmación registrada."
            if not regions:
                note += (
                    " La pantalla aún no cambió: no repitas la acción; usa wait o zoom y revisa el título "
                    "(código asignado) o el mensaje de éxito."
                )
            evidence_taken = bool(regions)
            if evidence_taken:
                note += self._keep_evidence(verdict.label, texts, image)
        elif (notices or code) and may_have_saved and not navigation:
            # The guard did not recognize the button (garbled OCR) but the app reported a save.
            self._guard.record_commit(NOTICE_LABEL)
            log.info("confirmación detectada por el resultado del clic en (%d,%d)", x, y)
            note += (
                " La aplicación mostró un aviso de guardado: se registró como acción de confirmación. "
                "No la repitas."
            )
            note += self._keep_evidence(NOTICE_LABEL, notices or texts, image)
            evidence_taken = True
            self._outcome_targets.append(button_area)
        else:
            self._guard.mark_verified()
            if navigation:
                self._guard.note_navigation()
        if code is not None and evidence_taken:
            self._guard.note_saved_code(code)
        return await self._observation(
            image, note, changed, evidence_taken=evidence_taken, regions=regions, boxes=boxes
        )

    async def trace_event(self, note: str) -> None:
        """Trace step for something the agent noticed without acting (e.g. an interface change)."""
        await self._save_trace(ToolCall(id="evento", name="evento", input={}), ToolOutcome(text=note))

    async def _check_field(self, x: int, y: int) -> None:
        verdict = self._guard.check_click(x, y, await self._target_boxes(x, y))
        if verdict.is_commit or words(verdict.label) & COMMIT_WORDS:
            raise ToolInputError(
                f"({x},{y}) está sobre '{verdict.label}', un botón de confirmación; no es un campo de texto"
            )

    async def _type_into(self, args: dict[str, Any]) -> ToolOutcome:
        x, y = _point(args)
        text = _text(args, "text")
        replace = _bool(args, "replace", True)
        await self._check_field(x, y)
        (region,) = await self._field_regions([(x, y)])
        before = self._image
        if not replace and before is not None and same_text(await self._read_field(before, region), text):
            # Appending would duplicate the value ("90123456712..." in a real run).
            return self._blocked(
                f"El campo en ({x},{y}) ya contiene '{text}'; no se escribió para no duplicarlo. "
                "Si dudas del valor usa zoom sobre el campo; para reescribirlo usa replace=true."
            )
        since = self._clock()
        await self._fill(x, y, text, replace)
        self._record_step("type into a field")
        if replace:
            self._record_fills([(x, y, text)], [region])
        else:
            self._void_script("text appended to a field")
        self._guard.note_data_entry()
        image, changed = await self._settler.settle(since)
        image = await self._await_fields(image, [region], before)
        read_back = await self._read_field(image, region)
        self.last_reread = (same_text(read_back, text),)
        note = f"Escribí en ({x},{y}). Campo releído: {self._reread_note(read_back, text)}."
        if not same_text(read_back, text):
            note += "\n" + REREAD_HINT
        return await self._observation(image, note, changed)

    async def _fill(self, x: int, y: int, text: str, replace: bool) -> None:
        await self._screen.click(x, y)
        if replace:
            await self._screen.key("ctrl+a")
        await self._screen.type_text(text)

    async def _fill_form(self, args: dict[str, Any]) -> ToolOutcome:
        fields = args.get("fields")
        if not isinstance(fields, list) or not 1 <= len(fields) <= MAX_FORM_FIELDS:
            raise ToolInputError(f"'fields' debe tener entre 1 y {MAX_FORM_FIELDS} campos")
        parsed: list[tuple[int, int, str]] = []
        for field in fields:
            if not isinstance(field, dict):
                raise ToolInputError("cada campo debe tener x, y y value")
            x, y = _point(field)
            parsed.append((x, y, _text(field, "value")))
        for x, y, _ in parsed:
            await self._check_field(x, y)
        regions = await self._field_regions([(x, y) for x, y, _ in parsed])
        before = self._image
        since = self._clock()
        for x, y, value in parsed:
            await self._fill(x, y, value, True)
        self._record_step(f"fill form ({len(parsed)} fields)")
        self._record_fills(parsed, regions)
        self._guard.note_data_entry()
        image, changed = await self._settler.settle(since)
        image = await self._await_fields(image, regions, before)
        lines = ["Formulario llenado. Campos releídos:"]
        mismatch = False
        reread: list[bool] = []
        for (x, y, value), region in zip(parsed, regions):
            read_back = await self._read_field(image, region)
            reread.append(same_text(read_back, value))
            mismatch |= not reread[-1]
            lines.append(f"- ({x},{y}): {self._reread_note(read_back, value)}")
        self.last_reread = tuple(reread)
        if mismatch:
            lines.append(REREAD_HINT)
        return await self._observation(image, "\n".join(lines), changed)

    async def _key(self, args: dict[str, Any]) -> ToolOutcome:
        combo = _text(args, "combo").strip()
        if not combo:
            raise ToolInputError("'combo' está vacío")
        verdict = self._guard.check_key(combo)
        if not verdict.allowed:
            return self._blocked(verdict.reason)
        since = self._clock()
        await self._screen.key(combo)
        self._record_step(f"key {combo}")
        normalized = normalize_combo(combo)
        if len(normalized) == 1:
            self._void_script("a key that types text")
        elif normalized not in SCRIPT_SKIPPED_COMBOS:
            self._add_script(ScriptStep(kind="key", anchor=combo))
        self._guard.note_data_entry()
        outcome, _ = await self._observe_after(since, f"Tecla '{combo}' enviada.")
        return outcome

    async def _scroll(self, args: dict[str, Any]) -> ToolOutcome:
        x, y = _point(args)
        direction = args.get("direction")
        if direction not in ("up", "down"):
            raise ToolInputError("'direction' debe ser up o down")
        steps = _int(args, "steps", 1, MAX_SCROLL_STEPS)
        since = self._clock()
        await self._screen.scroll(x, y, steps if direction == "up" else -steps)
        self._record_step(f"scroll {direction}")
        self._guard.mark_verified()
        outcome, _ = await self._observe_after(since, f"Desplazamiento {direction} de {steps} pasos en ({x},{y}).")
        return outcome

    async def _wait(self, args: dict[str, Any]) -> ToolOutcome:
        seconds = args.get("seconds")
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not 0 <= seconds <= MAX_WAIT_S:
            raise ToolInputError(f"'seconds' debe estar entre 0 y {MAX_WAIT_S:g}")
        await self._sleep(float(seconds))
        self._guard.mark_verified()
        image = await self._screen.screenshot()
        return await self._observation(image, f"Esperé {seconds:g} s.")

    async def _zoom(self, args: dict[str, Any]) -> ToolOutcome:
        x, y = _point(args)
        w = _int(args, "w", 8, SCREEN_W)
        h = _int(args, "h", 8, SCREEN_H)
        if x + w > SCREEN_W or y + h > SCREEN_H:
            raise ToolInputError("el rectángulo se sale de la pantalla (1280x800)")
        image = await self._screen.screenshot()
        crop = image.crop((x, y, x + w, y + h))
        scale = min(float(ZOOM_FACTOR), SCREEN_W / w)
        zoomed = crop.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.Resampling.LANCZOS)
        boxes = await self._reader.read(image, (x, y, w, h))
        self._guard.mark_verified()
        text = "\n".join(
            [f"Zoom de ({x},{y},{w},{h}). Texto leído:", ocr_listing(boxes), self._status_line()]
        )
        return ToolOutcome(text=text, image=zoomed)

    async def _finish(self, args: dict[str, Any]) -> ToolOutcome:
        estado = args.get("estado")
        if estado not in ("terminada", "fallida"):
            raise ToolInputError("'estado' debe ser terminada o fallida")
        resumen = _text(args, "resumen")
        payload: dict[str, Any] = {"estado": estado, "resumen": resumen}
        for name in ("respuesta", "datos_razon"):
            if name in args:
                payload[name] = _strings(args, name)
        if "aprendizaje" in args:
            payload["aprendizaje"] = _text(args, "aprendizaje")
        return ToolOutcome(text="Tarea finalizada.", finish=payload)
