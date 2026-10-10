"""Memory mode: replay a learned script on the current screen without asking the model.

Every step is located again on the screen (buttons by their label, fields by their label plus the
recorded offset) and executed through the ToolBox, so the guard, the commit records, the evidence and the
trace work exactly as when the model acts. Any doubt stops the replay and hands control back to the model.
"""

import logging
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher

from agent.domain.guard import Guard
from agent.domain.models import Plan, Procedure, ScriptStep, TextBox, ToolCall
from agent.domain.procedures import normalize
from agent.domain.tools import SCREEN_H, SCREEN_W, ToolBox, ToolOutcome

log = logging.getLogger(__name__)

MAX_REPLAY_FAILURES = 2  # consecutive failed replays that retire a script (it stays as a text hint)
ANCHOR_SIMILARITY = 0.8  # OCR noise tolerated between a recorded label and the one on screen
MIN_STEP_S = 3.0  # time left below which no new step is started


@dataclass(frozen=True, slots=True)
class ReplayOutcome:
    completed: bool
    steps_done: int = 0  # steps finished (or skipped) before the replay stopped
    reason: str = ""  # why it stopped, in Spanish; empty when completed


def script_fields(script: Sequence[ScriptStep]) -> set[str]:
    return {step.field for step in script if step.kind == "fill"}


def replayable(procedures: Sequence[Procedure], plan: Plan, min_successes: int) -> Procedure | None:
    """First procedure whose script can run for this plan: enough successes, not retired, and filling
    exactly the plan's fields (a missing one would be left empty, an extra one has no value)."""
    plan_fields = {name for name, value in plan.values if value}
    for procedure in procedures:
        if (
            procedure.script
            and procedure.exitos >= min_successes
            and procedure.fallos_replay < MAX_REPLAY_FAILURES
            and script_fields(procedure.script) == plan_fields
        ):
            return procedure
    return None


def _plain(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", normalize(text)))


def similar(read: str, expected: str) -> bool:
    """Same label up to case, accents, punctuation and a little OCR noise."""
    a, b = _plain(read), _plain(expected)
    if not a or not b:
        return False
    return a == b or SequenceMatcher(None, a, b).ratio() >= ANCHOR_SIMILARITY


def find_label(boxes: Sequence[TextBox], label: str, near: tuple[int, int]) -> TextBox | None:
    """Box showing `label`; the one closest to where it was last seen when several match."""
    matches = [box for box in boxes if similar(box.text, label)]
    if not matches:
        return None
    return min(matches, key=lambda box: (box.center[0] - near[0]) ** 2 + (box.center[1] - near[1]) ** 2)


def _clamp(x: int, y: int) -> tuple[int, int]:
    return min(max(0, x), SCREEN_W - 1), min(max(0, y), SCREEN_H - 1)


def _first_line(outcome: ToolOutcome) -> str:
    return outcome.text.strip().splitlines()[0] if outcome.text.strip() else "error"


class ScriptRunner:
    """Runs a script step by step; stops at the first step it cannot do exactly as recorded."""

    def __init__(self, toolbox: ToolBox, guard: Guard, clock: Callable[[], float], deadline: float) -> None:
        self._toolbox = toolbox
        self._guard = guard
        self._clock = clock
        self._deadline = deadline
        self._calls = 0

    async def run(self, script: Sequence[ScriptStep], plan: Plan) -> ReplayOutcome:
        values = {name: value for name, value in plan.values if value}
        index = 0
        while index < len(script):
            if self._deadline - self._clock() < MIN_STEP_S:
                return ReplayOutcome(False, index, "no queda tiempo para seguir repitiendo")
            step = script[index]
            end = index + 1
            if step.kind == "fill":
                while end < len(script) and script[end].kind == "fill":
                    end += 1
                reason = await self._fill(script[index:end], values)
            elif step.kind == "click":
                reason = await self._click(step)
            elif step.kind == "key":
                reason = await self._key(step)
            else:
                reason = f"paso desconocido '{step.kind}'"
            if reason:
                log.info("repetición detenida en el paso %d: %s", index + 1, reason)
                return ReplayOutcome(False, index, reason)
            index = end
        if len(self._guard.commits) < plan.planned_commits:
            return ReplayOutcome(False, len(script), "no se registraron todas las confirmaciones del plan")
        return ReplayOutcome(True, len(script))

    async def _execute(self, name: str, args: dict[str, object]) -> ToolOutcome:
        self._calls += 1
        return await self._toolbox.execute(ToolCall(id=f"memoria_{self._calls}", name=name, input=dict(args)))

    async def _click(self, step: ScriptStep) -> str:
        box = find_label(self._toolbox.boxes, step.anchor, (step.x, step.y))
        if box is not None:
            x, y = box.center
        elif step.dismissal:
            # The dialog it closed is not shown this time; keep the step for the next replay.
            self._toolbox.keep_script_step(step)
            return ""
        elif similar(await self._toolbox.target_label(step.x, step.y), step.anchor):
            x, y = step.x, step.y
        else:
            return f"no se encontró '{step.anchor}' en la pantalla"
        outcome = await self._execute("click", {"x": x, "y": y})
        return _first_line(outcome) if outcome.is_error else ""

    async def _key(self, step: ScriptStep) -> str:
        outcome = await self._execute("key", {"combo": step.anchor})
        return _first_line(outcome) if outcome.is_error else ""

    async def _fill(self, steps: Sequence[ScriptStep], values: Mapping[str, str]) -> str:
        fields: list[dict[str, object]] = []
        for step in steps:
            value = values.get(step.field)
            if value is None:
                return f"el plan no tiene el campo '{step.field}'"
            label = find_label(self._toolbox.boxes, step.field_label, (step.x - step.dx, step.y - step.dy))
            if label is None:
                return f"no se encontró el campo '{step.field_label}' en la pantalla"
            x, y = _clamp(label.center[0] + step.dx, label.center[1] + step.dy)
            fields.append({"x": x, "y": y, "value": value})
        if len(fields) == 1:
            only = fields[0]
            outcome = await self._execute("type_into", {"x": only["x"], "y": only["y"], "text": only["value"],
                                                        "replace": True})
        else:
            outcome = await self._execute("fill_form", {"fields": fields})
        if outcome.is_error:
            return _first_line(outcome)
        reread = self._toolbox.last_reread
        if len(reread) != len(fields) or not all(reread):
            return "un campo releído no coincide con lo escrito"
        return ""
