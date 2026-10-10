"""LLM policy: ORIENT -> PLAN -> EXECUTE over an append-only message history."""

import asyncio
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from PIL import Image

from agent.domain.dates import NO_DATE_NOTE, parse_app_date, parse_date, relative_context
from agent.domain.guard import Guard
from agent.domain.models import (
    Classification, Decision, Plan, Procedure, TaskResult, TaskStatus, TextBox, ToolCall,
)
from agent.domain.procedures import describe_hints
from agent.domain.tools import TOOL_SPECS, ToolBox, image_block, ocr_listing
from agent.ports.interfaces import Model

log = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
APP_DATE_FORMAT = "%d/%m/%Y"
MAX_PLANNED_COMMITS = 3
MIN_TURN_S = 3.0
PLAN_KINDS = ["crear", "modificar", "eliminar", "consultar", "exportar", "otro"]

NUDGE = (
    "You answered without calling a tool. Continue by calling one of the tools; "
    "call finish when the task is done or cannot be done."
)
FINISH_NOW = (
    "Time is almost over ({seconds} s left). Do not start new changes: call finish now with the "
    "real state of the task."
)

REPORT_ORIENTATION: dict[str, Any] = {
    "name": "report_orientation",
    "description": "Report the application, the current module and the date the application shows as today.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "app": {"type": "string"},
            "module": {"type": "string"},
            "today": {"type": "string", "description": "dd/mm/yyyy as shown by the app, or empty."},
        },
        "required": ["app", "module", "today"],
        "additionalProperties": False,
    },
}

SUBMIT_PLAN: dict[str, Any] = {
    "name": "submit_plan",
    "description": "Submit the plan for the task.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": PLAN_KINDS},
            "entity": {"type": "string"},
            "target": {"type": "string"},
            "values": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"field": {"type": "string"}, "value": {"type": "string"}},
                    "required": ["field", "value"],
                    "additionalProperties": False,
                },
            },
            "preconditions": {"type": "array", "items": {"type": "string"}},
            "planned_commits": {"type": "integer"},
            "feasible": {"type": "boolean"},
            "reason": {"type": "string"},
            "expected_evidence": {"type": "string"},
        },
        "required": [
            "kind", "entity", "target", "values", "preconditions", "planned_commits", "feasible", "reason",
            "expected_evidence",
        ],
        "additionalProperties": False,
    },
}


@dataclass(frozen=True, slots=True)
class Orientation:
    today: str | None = None
    app: str = ""
    module: str = ""


def load_prompt(name: str, prompts_dir: Path = PROMPTS_DIR) -> str:
    return (prompts_dir / f"{name}.md").read_text(encoding="utf-8").strip()


def first_call(decision: Decision, name: str) -> ToolCall | None:
    return next((call for call in decision.tool_calls if call.name == name), None)


def describe_plan(plan: Plan) -> str:
    values = "; ".join(f"{field} = {value}" for field, value in plan.values) or "(none)"
    preconditions = "; ".join(plan.preconditions) or "(none)"
    return (
        f"kind: {plan.kind}\nentity: {plan.entity}\ntarget: {plan.target}\nvalues: {values}\n"
        f"preconditions: {preconditions}\nplanned commits: {plan.planned_commits}\n"
        f"expected evidence: {plan.expected_evidence}"
    )


def describe_orientation(orientation: Orientation) -> str:
    today = orientation.today or "unknown"
    text = f"Today (date shown by the app): {today}\nApp: {orientation.app or '?'}; module: {orientation.module or '?'}"
    parsed = parse_date(orientation.today) if orientation.today else None
    if parsed is None:
        return f"{text}\n{NO_DATE_NOTE}"
    return f"{text}\nRelative dates computed from the app's today:\n{relative_context(parsed)}"


async def screen_content(text: str, image: Image.Image, boxes: Sequence[TextBox]) -> list[dict[str, Any]]:
    block = await asyncio.to_thread(image_block, image)
    return [{"type": "text", "text": f"{text}\n\nScreen text (text @x,y):\n{ocr_listing(boxes)}"}, block]


class Policy:
    """Drives the fast model with our tools; mechanical limits live here, strategy in the prompts."""

    def __init__(
        self,
        model: Model,
        toolbox: ToolBox,
        guard: Guard,
        clock: Callable[[], float],
        deadline: float,
        prompts_dir: Path = PROMPTS_DIR,
        max_turns: int = 40,
        finish_margin: float = 40.0,
        classification: Classification | None = None,
    ) -> None:
        self._model = model
        self._toolbox = toolbox
        self._guard = guard
        self._clock = clock
        self._deadline = deadline
        self._prompts_dir = prompts_dir
        self._max_turns = max_turns
        self._finish_margin = finish_margin
        self._classification = classification

    def _remaining(self) -> float:
        return self._deadline - self._clock()

    async def orient(self, image: Image.Image, boxes: Sequence[TextBox]) -> Orientation:
        app_date = parse_app_date(boxes)
        if app_date is not None:
            return Orientation(today=app_date.strftime(APP_DATE_FORMAT))
        content = await screen_content("First screenshot of the application.", image, boxes)
        try:
            decision = await self._model.decide(
                load_prompt("orient", self._prompts_dir), [{"role": "user", "content": content}], [REPORT_ORIENTATION]
            )
        except Exception:
            log.exception("falló la orientación con el modelo")
            return Orientation()
        call = first_call(decision, "report_orientation")
        if call is None:
            return Orientation()
        today = parse_date(str(call.input.get("today", "")))
        return Orientation(
            today=today.strftime(APP_DATE_FORMAT) if today is not None else None,
            app=str(call.input.get("app", "")),
            module=str(call.input.get("module", "")),
        )

    async def plan(
        self, instruction: str, orientation: Orientation, image: Image.Image, boxes: Sequence[TextBox]
    ) -> Plan:
        text = f"Task: {instruction}\n{describe_orientation(orientation)}{self._planning_hint()}"
        messages: list[Any] = [{"role": "user", "content": await screen_content(text, image, boxes)}]
        system = load_prompt("plan", self._prompts_dir) + self._planning_hint()
        for attempt in range(2):
            try:
                decision = await self._model.decide(system, messages, [SUBMIT_PLAN])
            except Exception:
                log.exception("falló la planificación con el modelo")
                return Plan(kind="otro", feasible=False, reason="No se pudo planear la tarea.")
            call = first_call(decision, "submit_plan")
            if call is not None:
                plan = self._to_plan(call.input)
                plan = self._apply_caution(plan)
                if plan.feasible:
                    self._guard.planned_commits = plan.planned_commits
                return plan
            if attempt == 0:
                if decision.raw:
                    messages.append({"role": "assistant", "content": list(decision.raw)})
                messages.append({"role": "user", "content": [{"type": "text", "text": "Call submit_plan now."}]})
        return Plan(kind="otro", feasible=False, reason="No se obtuvo un plan para la tarea.")

    @staticmethod
    def _to_plan(data: dict[str, Any]) -> Plan:
        values = tuple(
            (str(item.get("field", "")), str(item.get("value", "")))
            for item in data.get("values", [])
            if isinstance(item, dict)
        )
        raw_commits = data.get("planned_commits", 0)
        commits = raw_commits if isinstance(raw_commits, int) and not isinstance(raw_commits, bool) else 0
        commits = max(0, commits)
        feasible = bool(data.get("feasible", False))
        reason = str(data.get("reason", ""))
        if commits > MAX_PLANNED_COMMITS:
            feasible = False
            reason = (
                f"La tarea requiere {commits} confirmaciones; el máximo permitido es {MAX_PLANNED_COMMITS}. "
                f"{reason}"
            ).strip()
        return Plan(
            kind=str(data.get("kind", "otro")),
            entity=str(data.get("entity", "")),
            target=str(data.get("target", "")),
            values=values,
            preconditions=tuple(str(p) for p in data.get("preconditions", [])),
            planned_commits=commits,
            feasible=feasible,
            reason=reason,
            expected_evidence=str(data.get("expected_evidence", "")),
        )

    def _planning_hint(self) -> str:
        hint = self._classification
        if hint is None:
            return ""
        notes: list[str] = []
        if hint.task_type:
            notes.append(f"Falco advisory phase hint: {hint.task_type}. Validate it against the task.")
        if hint.destructive is True:
            notes.append(
                "Falco advisory: this may be destructive. Require explicit confirmation of the exact scope "
                "on screen before any commit; do not relax any precondition or verification."
            )
        if hint.two_data is True:
            notes.append("Falco advisory: the final respuesta must contain exactly two values formatted as A; B.")
        return "\n\n" + "\n".join(notes) if notes else ""

    def _apply_caution(self, plan: Plan) -> Plan:
        if self._classification is None or self._classification.destructive is not True:
            return plan
        scope_check = "confirmar en pantalla el alcance exacto antes de cualquier commit"
        if scope_check in plan.preconditions:
            return plan
        return replace(plan, preconditions=(*plan.preconditions, scope_check))

    async def run_plan(
        self,
        instruction: str,
        plan: Plan,
        orientation: Orientation,
        image: Image.Image,
        boxes: Sequence[TextBox],
        hints: Sequence[Procedure] = (),
    ) -> TaskResult:
        if not plan.feasible:
            return TaskResult(estado=TaskStatus.fallida, resumen=plan.reason or "La tarea no es realizable.")
        return await self.execute(instruction, plan, orientation, image, boxes, hints)

    def _failed(self, resumen: str) -> TaskResult:
        return TaskResult(estado=TaskStatus.fallida, resumen=resumen, commits=self._guard.commits)

    async def execute(
        self,
        instruction: str,
        plan: Plan,
        orientation: Orientation,
        image: Image.Image,
        boxes: Sequence[TextBox],
        hints: Sequence[Procedure] = (),
    ) -> TaskResult:
        system = load_prompt("system", self._prompts_dir)
        intro = (
            f"Task: {instruction}\n{describe_orientation(orientation)}\n\nPlan:\n{describe_plan(plan)}"
            f"{self._planning_hint()}"
        )
        hint_text = describe_hints(hints)
        if hint_text:
            intro = f"{intro}\n\n{hint_text}"
        messages: list[Any] = [{"role": "user", "content": await screen_content(intro, image, boxes)}]
        nudged = False
        for turn in range(1, self._max_turns + 1):
            if self._remaining() < MIN_TURN_S:
                return self._timed_out()
            try:
                decision = await self._model.decide(system, messages, TOOL_SPECS)
            except Exception:
                log.exception("falló la llamada al modelo durante la ejecución")
                return self._failed("Se interrumpió la tarea por un error al consultar el modelo.")
            if self._remaining() <= 0:
                return self._timed_out()
            if decision.raw:
                messages.append({"role": "assistant", "content": list(decision.raw)})
            if not decision.tool_calls:
                if nudged:
                    return self._failed("El agente dejó de operar sin reportar el resultado.")
                nudged = True
                messages.append({"role": "user", "content": [{"type": "text", "text": NUDGE}]})
                continue
            results, finish = await self._run_calls(decision.tool_calls)
            if finish is not None:
                log.info("tarea terminada en %d pasos", turn)
                return self._finished(finish)
            if self._remaining() <= 0:
                return self._timed_out()
            remaining = self._remaining()
            if remaining < self._finish_margin:
                results.append({"type": "text", "text": FINISH_NOW.format(seconds=max(0, int(remaining)))})
            messages.append({"role": "user", "content": results})
        return self._failed("Se alcanzó el límite de pasos sin terminar la tarea.")

    def _timed_out(self) -> TaskResult:
        return self._failed("Se agotó el tiempo antes de terminar la tarea.")

    async def _run_calls(self, calls: Sequence[ToolCall]) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
        results: list[dict[str, Any]] = []
        finish: dict[str, Any] | None = None
        for call in calls:
            if finish is not None or self._remaining() <= 0:
                reason = "No ejecutada: la tarea ya terminó." if finish is not None else "No ejecutada: sin tiempo."
                results.append(_tool_result(call.id, [{"type": "text", "text": reason}], True))
                continue
            outcome = await self._toolbox.execute(call)
            content: list[dict[str, Any]] = [{"type": "text", "text": outcome.text}]
            if outcome.image is not None:
                content.append(await asyncio.to_thread(image_block, outcome.image))
            results.append(_tool_result(call.id, content, outcome.is_error))
            if outcome.finish is not None:
                finish = outcome.finish
        return results, finish

    def _finished(self, payload: dict[str, Any]) -> TaskResult:
        estado = TaskStatus.terminada if payload.get("estado") == "terminada" else TaskStatus.fallida
        return TaskResult(
            estado=estado,
            resumen=str(payload.get("resumen", "")),
            respuesta=tuple(str(v) for v in payload.get("respuesta", []) if str(v).strip()),
            datos_razon=tuple(str(v) for v in payload.get("datos_razon", []) if str(v).strip()),
            commits=self._guard.commits,
            aprendizaje=str(payload.get("aprendizaje", "")),
        )


def _tool_result(tool_use_id: str, content: list[dict[str, Any]], is_error: bool) -> dict[str, Any]:
    block: dict[str, Any] = {"type": "tool_result", "tool_use_id": tool_use_id, "content": content}
    if is_error:
        block["is_error"] = True
    return block
