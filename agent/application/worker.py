"""Worker contract, the screen-driven agent worker and a deterministic test worker."""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlparse

from agent.config import Settings
from agent.domain.app_profile import app_key, select_current
from agent.domain.budget import task_deadline
from agent.domain.guard import Guard
from agent.domain.models import Plan, Procedure, ScriptStep, TaskResult, TaskStatus, TextBox
from agent.domain.policy import DEFAULT_MAX_TURNS, PROMPTS_DIR, Orientation, Policy
from agent.domain.procedures import intent_key, normalize, sanitize_script, sanitize_steps, sanitize_text
from agent.domain.replay import ScriptRunner, replayable
from agent.domain.reporter import build_summary
from agent.domain.settler import Settler
from agent.domain.tools import Settles, ToolBox, TraceSink
from agent.domain.verifier import Verifier
from agent.infra.vnc.keys import BLOCKED_COMBOS
from agent.ports.interfaces import Classifier, Memory, Model, Reader, Screen

log = logging.getLogger(__name__)

FIRST_FRAME_TIMEOUT_S = 30.0
SCREEN_WIDTH = 1280
HEADER_BAND_PX = 64  # title bar plus top menu, read as a region so dark overlays are inverted
FIRST_FRAME_POLL_S = 0.2
HARD_MARGIN_S = 5.0  # stop before the task manager's own timeout
VERIFY_RESERVE_S = 30.0  # time kept for the final verification call
QUESTION_KINDS: frozenset[str] = frozenset({"consultar", "pregunta", "question"})
REPLAY_STOPPED_NOTE = (
    "Repetición desde memoria detenida en el paso {step}: {reason}. Continúa desde la pantalla actual; "
    "no repitas confirmaciones ya registradas."
)
CHANGE_VERBS: dict[str, str] = {
    "crear": "Se creó", "modificar": "Se modificó", "eliminar": "Se eliminó", "exportar": "Se exportó",
}


def is_question(plan: Plan) -> bool:
    """Read-only task that asks for a value instead of changing data."""
    return plan.kind.strip().casefold() in QUESTION_KINDS


def describe_change(plan: Plan) -> str:
    """Spanish summary of a change done exactly as planned, e.g. "Se creó cliente 'X' con nit: 9."."""
    verb = CHANGE_VERBS.get(normalize(plan.kind), "Se completó la tarea sobre")
    subject = " ".join(part for part in (plan.entity.strip(), f"'{plan.target}'" if plan.target else "") if part)
    values = "; ".join(f"{name}: {value}" for name, value in plan.values if value)
    text = f"{verb} {subject}".strip()
    return f"{text} con {values}." if values else f"{text}."


@runtime_checkable
class Worker(Protocol):
    """Executes one task without exposing connection credentials in its result."""

    async def run(self) -> tuple[TaskStatus, str]: ...

    def partial_summary(self, reason: str) -> str: ...

    async def close(self) -> None: ...


class AgentWorker:
    """ORIENT -> PLAN -> EXECUTE -> VERIFY on one desktop; `terminada` only when verified."""

    def __init__(
        self,
        instruction: str,
        url: str,
        password: str,
        settings: Settings,
        model_fast: Model,
        model_strong: Model,
        reader: Reader,
        screen_factory: Callable[[], Screen],
        clock: Callable[[], float] = time.monotonic,
        trace_factory: Callable[[], TraceSink | None] | None = None,
        blocked_combos: Iterable[str] = BLOCKED_COMBOS,
        settler_factory: Callable[[Screen], Settles] = Settler,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        first_frame_timeout: float = FIRST_FRAME_TIMEOUT_S,
        classifier: Classifier | None = None,
        memory: Memory | None = None,
        max_turns: int = DEFAULT_MAX_TURNS,
        prompts_dir: Path = PROMPTS_DIR,
        replay_min_successes: int = 1,
    ) -> None:
        self._instruction = instruction
        self._url = url
        self._password = password
        self._settings = settings
        self._model_fast = model_fast
        self._model_strong = model_strong
        self._reader = reader
        self._screen_factory = screen_factory
        self._clock = clock
        self._trace_factory = trace_factory
        self._settler_factory = settler_factory
        self._sleep = sleep
        self._first_frame_timeout = first_frame_timeout
        self._classifier = classifier
        self._memory = memory
        self._max_turns = max_turns
        self._prompts_dir = prompts_dir
        self._replay_min_successes = replay_min_successes
        self._guard = Guard(blocked_combos, planned_commits=0)
        self._screen: Screen | None = None
        self._closed = False

    @property
    def _secrets(self) -> list[str]:
        host = urlparse(self._url).hostname or ""
        return [
            value
            for value in (self._password, self._url, host, self._settings.anthropic_api_key,
                          self._settings.team_token, self._settings.desktop_password)
            if value
        ]

    async def run(self) -> tuple[TaskStatus, str]:
        started = self._clock()
        hard_deadline = started + self._settings.time_limit_sec - HARD_MARGIN_S
        task_deadline.set(hard_deadline)
        try:
            result = await self._run(hard_deadline)
        except Exception:
            log.exception("el agente falló operando el escritorio")
            result = TaskResult(
                estado=TaskStatus.fallida,
                resumen="Error interno al operar el escritorio.",
                commits=self._guard.commits,
            )
        return result.estado, build_summary(result, self._secrets)

    async def _run(self, hard_deadline: float) -> TaskResult:
        screen = self._screen_factory()
        self._screen = screen
        classification_task = (
            asyncio.create_task(self._classifier.classify(self._instruction))
            if self._classifier is not None
            else None
        )
        try:
            await screen.connect(self._url, self._password)
            classification = await classification_task if classification_task is not None else None
        except BaseException:
            if classification_task is not None:
                classification_task.cancel()
                await asyncio.gather(classification_task, return_exceptions=True)
            raise
        if not await self._wait_first_frame(screen):
            return TaskResult(estado=TaskStatus.fallida, resumen="No se recibió la imagen del escritorio.")
        trace = self._make_trace()
        exec_deadline = hard_deadline - VERIFY_RESERVE_S
        toolbox = ToolBox(
            screen, self._reader, self._settler_factory(screen), self._guard, self._clock,
            trace=trace, deadline=exec_deadline, sleep=self._sleep,
        )
        image, boxes = await toolbox.observe()
        if not boxes:
            # The first Raw frame (~4 MB) may still be arriving: wait for a stable screen and look again.
            image, boxes = await toolbox.settle_and_observe()
        policy = Policy(
            self._model_fast,
            toolbox,
            self._guard,
            self._clock,
            exec_deadline,
            prompts_dir=self._prompts_dir,
            max_turns=self._max_turns,
            classification=classification,
        )
        app = await self._app_key(image, boxes)
        orientation = await policy.orient(image, boxes)
        plan = await policy.plan(self._instruction, orientation, image, boxes)
        intent = intent_key(plan)
        recalled = await self._recall(app, intent) if plan.feasible else []
        hints, change = select_current(recalled, orientation.version)
        if change:
            # Routes learned on another interface version would point the model at buttons that moved.
            log.info("%s; se descartan %d procedimientos guardados", change, len(recalled) - len(hints))
            await toolbox.trace_event(change)
        toolbox.set_plan_values(plan.values)
        used = (
            replayable(hints, plan, self._replay_min_successes)
            if plan.feasible and not is_question(plan)
            else None
        )
        replayed = False
        if used is None:
            result = await policy.run_plan(self._instruction, plan, orientation, image, boxes, hints=hints)
        else:
            result, replayed = await self._replay(used, plan, policy, toolbox, orientation, hints, exec_deadline)
        if result.estado is not TaskStatus.terminada:
            await self._penalize(app, intent, hints)
            await self._replay_failed(app, intent, used)
            return result
        if is_question(plan) and not self._guard.commits:
            # A read-only answer leaves nothing to verify on screen; a fresh screenshot of a paginated
            # list cannot confirm a total, so the verifier would only reject a correct answer.
            if result.respuesta:
                await self._remember(app, intent, plan, toolbox.steps, result, orientation.version)
                return result
            await self._penalize(app, intent, hints)
            return replace(
                result, estado=TaskStatus.fallida, resumen=f"{result.resumen} No se obtuvo la respuesta.".strip()
            )
        final_image, final_boxes = await toolbox.observe()
        claim = "; ".join(filter(None, [result.resumen, "respuesta: " + "; ".join(result.respuesta)
                                        if result.respuesta else ""]))
        confirmed, evidence = await Verifier(self._model_strong, self._prompts_dir).confirm(
            plan, self._instruction, final_image, final_boxes, claim=claim,
            evidence=toolbox.commit_evidence,
        )
        if confirmed:
            if used is not None and replayed:
                await self._remember_replay(app, intent, plan, used, toolbox.script, orientation.version)
            else:
                await self._replay_failed(app, intent, used, penalize=True)
                await self._remember(
                    app, intent, plan, toolbox.steps, result, orientation.version, script=toolbox.script
                )
            return result
        log.info("el verificador no confirmó el resultado")
        await self._penalize(app, intent, hints)
        await self._replay_failed(app, intent, used)
        return replace(
            result,
            estado=TaskStatus.fallida,
            resumen=f"No se pudo confirmar el resultado en pantalla: {evidence}",
        )

    async def _replay(
        self,
        used: Procedure,
        plan: Plan,
        policy: Policy,
        toolbox: ToolBox,
        orientation: Orientation,
        hints: Sequence[Procedure],
        deadline: float,
    ) -> tuple[TaskResult, bool]:
        """Memory mode: replay the learned script; if it stops, the model continues from the current screen
        with the guard state kept (commits already made stay counted). Returns (result, replay completed)."""
        outcome = await ScriptRunner(toolbox, self._guard, self._clock, deadline).run(used.script, plan)
        if outcome.completed:
            log.info("modo memoria: %d pasos reproducidos", outcome.steps_done)
            await toolbox.trace_event(f"modo memoria: {outcome.steps_done} pasos reproducidos")
            result = TaskResult(
                estado=TaskStatus.terminada,
                resumen=describe_change(plan),
                commits=self._guard.commits,
                aprendizaje=used.aprendizaje,
            )
            return result, True
        step = outcome.steps_done + 1
        await toolbox.trace_event(f"repetición detenida en {step}: {outcome.reason}")
        image, boxes = await toolbox.observe()
        note = REPLAY_STOPPED_NOTE.format(step=step, reason=outcome.reason)
        result = await policy.execute(self._instruction, plan, orientation, image, boxes, hints, note=note)
        return result, False

    # --- procedure memory: failures are logged and never change the task result -----------------

    async def _remember_replay(
        self,
        app: str | None,
        intent: str,
        plan: Plan,
        used: Procedure,
        script: Sequence[ScriptStep],
        app_version: str | None,
    ) -> None:
        """A verified replay counts as one more success of the procedure it replayed; the script just
        recorded (current positions) replaces the stored one."""
        if self._memory is None or app is None:
            return
        try:
            fresh = sanitize_script(script, plan, self._secrets)
            procedure = replace(used, app_version=app_version, script=fresh or used.script)
            await self._memory.save_procedure(app, procedure)
            await self._memory.record_replay(app, intent, procedure, success=True)
        except Exception:
            log.exception("no se pudo registrar la repetición desde memoria")

    async def _replay_failed(
        self, app: str | None, intent: str, used: Procedure | None, penalize: bool = False
    ) -> None:
        if self._memory is None or app is None or used is None:
            return
        try:
            await self._memory.record_replay(app, intent, used, success=False)
        except Exception:
            log.exception("no se pudo registrar el fallo de la repetición desde memoria")
        if penalize:
            await self._penalize(app, intent, [used])

    async def _app_key(self, image: Any, boxes: list[TextBox]) -> str | None:
        """Memory key: the app name read from the title bar region (readable even under a modal that
        darkens it for full-screen OCR), else the name in the full-screen OCR, else the legacy fingerprint."""
        if self._memory is None:
            return None
        try:
            header = await self._reader.read(image, (0, 0, SCREEN_WIDTH, HEADER_BAND_PX))
        except Exception:
            log.exception("no se pudo leer el encabezado de la aplicación")
            header = []
        return app_key(header) or app_key(boxes) or self._fingerprint(boxes)

    def _fingerprint(self, boxes: list[TextBox]) -> str | None:
        if self._memory is None:
            return None
        try:
            # An empty fingerprint means the app could not be identified: no recall, no save.
            return self._memory.fingerprint(boxes) or None
        except Exception:
            log.exception("no se pudo identificar la aplicación para la memoria de procedimientos")
            return None

    async def _recall(self, app: str | None, intent: str) -> list[Procedure]:
        if self._memory is None or app is None:
            return []
        try:
            return list(await self._memory.procedures(app, intent))
        except Exception:
            log.exception("no se pudieron leer los procedimientos guardados")
            return []

    async def _remember(
        self,
        app: str | None,
        intent: str,
        plan: Plan,
        steps: Sequence[str],
        result: TaskResult,
        app_version: str | None = None,
        script: Sequence[ScriptStep] = (),
    ) -> None:
        if self._memory is None or app is None:
            return
        try:
            secrets = self._secrets
            procedure = Procedure(
                app=app,
                intent=intent,
                steps=sanitize_steps(steps, plan, secrets),
                aprendizaje=sanitize_text(result.aprendizaje, plan, secrets),
                app_version=app_version,
                script=sanitize_script(script, plan, secrets),
            )
            await self._memory.save_procedure(app, procedure)
        except Exception:
            log.exception("no se pudo guardar el procedimiento")

    async def _penalize(self, app: str | None, intent: str, hints: Sequence[Procedure]) -> None:
        if self._memory is None or app is None or not hints:
            return
        try:
            await self._memory.penalize(app, intent, hints)
        except Exception:
            log.exception("no se pudieron penalizar los procedimientos usados")

    async def _wait_first_frame(self, screen: Screen) -> bool:
        limit = self._clock() + self._first_frame_timeout
        while screen.last_change_ts() <= 0:
            if self._clock() >= limit:
                return False
            await self._sleep(FIRST_FRAME_POLL_S)
        return True

    def _make_trace(self) -> TraceSink | None:
        if self._trace_factory is None:
            return None
        try:
            return self._trace_factory()
        except OSError:
            log.exception("no se pudo crear la traza de la tarea")
            return None

    def partial_summary(self, reason: str) -> str:
        result = TaskResult(estado=TaskStatus.fallida, resumen=reason, commits=self._guard.commits)
        return build_summary(result, self._secrets)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._screen is not None:
            await self._screen.close()


class DummyWorker:
    """Deterministic worker for API tests and for running without an LLM key."""

    def __init__(self, delay_seconds: float = 0.05, summary: str = "tarea de prueba completada") -> None:
        self._delay_seconds = delay_seconds
        self._summary = summary
        self.closed = False

    async def run(self) -> tuple[TaskStatus, str]:
        await asyncio.sleep(self._delay_seconds)
        return TaskStatus.terminada, self._summary

    def partial_summary(self, reason: str) -> str:
        return reason

    async def close(self) -> None:
        self.closed = True
