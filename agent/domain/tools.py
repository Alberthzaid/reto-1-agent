"""LLM tool definitions and their execution against the Screen/Reader ports."""

import asyncio
import base64
import io
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import numpy as np
from PIL import Image

from agent.domain.diff import changed_regions
from agent.domain.guard import Guard, label_at
from agent.domain.models import TextBox, ToolCall
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
# Full-screen OCR merges adjacent buttons into one line and misses light-on-dark text, so the guard
# reads a small crop around the click point instead.
GUARD_CROP_W = 180
GUARD_CROP_H = 34
# After a commit, the text of what changed (toasts, dialogs) is read before it disappears.
MAX_EVIDENCE_REGIONS = 6
MAX_EVIDENCE_AREA = SCREEN_W * SCREEN_H // 2
EVIDENCE_PAD_PX = 4
MAX_EVIDENCE_CHARS = 300

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
        "The field is read back afterwards.",
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
    async def settle(self, since: float) -> tuple[Image.Image, bool]: ...


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
        self.steps: list[str] = []
        self._step = 0
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

    async def _observation(self, image: Image.Image, note: str, changed: bool | None = None) -> ToolOutcome:
        previous = self._image
        regions = await asyncio.to_thread(screen_regions, previous, image) if previous is not None else []
        boxes = await self._reader.read(image)
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

    async def _read_field(self, image: Image.Image, x: int, y: int) -> str:
        left = max(0, x - 320)
        top = max(0, y - 16)
        width = min(640, SCREEN_W - left)
        height = min(32, SCREEN_H - top)
        boxes = await self._reader.read(image, (left, top, width, height))
        return " | ".join(box.text for box in sorted(boxes, key=lambda b: b.x)) or "(vacío o ilegible)"

    async def _target_boxes(self, x: int, y: int) -> Sequence[TextBox]:
        """Boxes the guard judges a click with: a crop around the point, else the full-screen OCR."""
        if self._image is None:
            return self._boxes
        crop = await self._reader.read(self._image, guard_crop(x, y))
        if crop and label_at(x, y, crop):
            return crop
        return self._boxes

    async def _collect_evidence(self, label: str, image: Image.Image) -> str:
        """Read the regions a commit changed (toasts vanish quickly) and keep the screenshot."""
        previous = self._image
        regions = await asyncio.to_thread(screen_regions, previous, image) if previous is not None else []
        texts: list[str] = []
        for region in evidence_regions(regions):
            boxes = await self._reader.read(image, region)
            text = " ".join(box.text for box in sorted(boxes, key=lambda b: (b.y, b.x))).strip()
            if text and text not in texts:
                texts.append(text[:MAX_EVIDENCE_CHARS])
        self._commit_evidence.append(CommitEvidence(label=label, texts=tuple(texts), image=image))
        return f"\nMensajes tras confirmar: {' | '.join(texts)}" if texts else ""

    # --- handlers ----------------------------------------------------------------------------

    def _record_step(self, step: str) -> None:
        """Navigation log for procedure memory: no typed values, consecutive repeats collapsed, capped."""
        if self.steps and self.steps[-1] == step:
            return
        if len(self.steps) < MAX_RECORDED_STEPS:
            self.steps.append(step)

    def _blocked(self, reason: str) -> ToolOutcome:
        return ToolOutcome(text=f"{reason}\n{self._status_line()}", is_error=True)

    async def _click(self, args: dict[str, Any]) -> ToolOutcome:
        x, y = _point(args)
        button = args.get("button", "left")
        if button not in ("left", "right", "middle"):
            raise ToolInputError("'button' debe ser left, right o middle")
        double = _bool(args, "double", False)
        verdict = self._guard.check_click(x, y, await self._target_boxes(x, y))
        if not verdict.allowed:
            return self._blocked(verdict.reason)
        since = self._clock()
        await self._screen.click(x, y, button, double)
        if verdict.label:
            self._record_step(f"click '{verdict.label}'")
        target = f" sobre '{verdict.label}'" if verdict.label else ""
        note = f"Clic en ({x},{y}){target}."
        image, changed = await self._settler.settle(since)
        if verdict.is_commit:
            self._guard.record_commit(verdict.label, changed)
            note += " Acción de confirmación registrada." if changed else " La confirmación no cambió la pantalla."
            if changed:
                note += await self._collect_evidence(verdict.label, image)
        else:
            self._guard.mark_verified()
        return await self._observation(image, note, changed)

    async def _check_field(self, x: int, y: int) -> None:
        verdict = self._guard.check_click(x, y, await self._target_boxes(x, y))
        if verdict.is_commit:
            raise ToolInputError(
                f"({x},{y}) está sobre '{verdict.label}', un botón de confirmación; no es un campo de texto"
            )

    async def _type_into(self, args: dict[str, Any]) -> ToolOutcome:
        x, y = _point(args)
        text = _text(args, "text")
        replace = _bool(args, "replace", True)
        await self._check_field(x, y)
        since = self._clock()
        await self._fill(x, y, text, replace)
        self._record_step("type into a field")
        self._guard.note_data_entry()
        image, changed = await self._settler.settle(since)
        read_back = await self._read_field(image, x, y)
        return await self._observation(image, f"Escribí en ({x},{y}). Campo releído: '{read_back}'.", changed)

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
        since = self._clock()
        for x, y, value in parsed:
            await self._fill(x, y, value, True)
        self._record_step(f"fill form ({len(parsed)} fields)")
        self._guard.note_data_entry()
        image, changed = await self._settler.settle(since)
        lines = ["Formulario llenado. Campos releídos:"]
        for x, y, _ in parsed:
            lines.append(f"- ({x},{y}): '{await self._read_field(image, x, y)}'")
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
