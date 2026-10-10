"""Worker contract, the screen-driven agent worker and a deterministic test worker."""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import replace
from typing import Protocol, runtime_checkable
from urllib.parse import urlparse

from agent.config import Settings
from agent.domain.app_profile import select_current
from agent.domain.budget import task_deadline
from agent.domain.guard import Guard
from agent.domain.models import Plan, Procedure, TaskResult, TaskStatus, TextBox
from agent.domain.policy import Policy
from agent.domain.procedures import intent_key, sanitize_steps, sanitize_text
from agent.domain.reporter import build_summary
from agent.domain.settler import Settler
from agent.domain.tools import Settles, ToolBox, TraceSink
from agent.domain.verifier import Verifier
from agent.infra.vnc.keys import BLOCKED_COMBOS
from agent.ports.interfaces import Classifier, Memory, Model, Reader, Screen

log = logging.getLogger(__name__)

FIRST_FRAME_TIMEOUT_S = 30.0
FIRST_FRAME_POLL_S = 0.2
HARD_MARGIN_S = 5.0  # stop before the task manager's own timeout
VERIFY_RESERVE_S = 30.0  # time kept for the final verification call
QUESTION_KINDS: frozenset[str] = frozenset({"consultar", "pregunta", "question"})


def is_question(plan: Plan) -> bool:
    """Read-only task that asks for a value instead of changing data."""
    return plan.kind.strip().casefold() in QUESTION_KINDS


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
            classification=classification,
        )
        app = self._fingerprint(boxes)
        orientation = await policy.orient(image, boxes)
        plan = await policy.plan(self._instruction, orientation, image, boxes)
        intent = intent_key(plan)
        recalled = await self._recall(app, intent) if plan.feasible else []
        hints, change = select_current(recalled, orientation.version)
        if change:
            # Routes learned on another interface version would point the model at buttons that moved.
            log.info("%s; se descartan %d procedimientos guardados", change, len(recalled) - len(hints))
            await toolbox.trace_event(change)
        result = await policy.run_plan(self._instruction, plan, orientation, image, boxes, hints=hints)
        if result.estado is not TaskStatus.terminada:
            await self._penalize(app, intent, hints)
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
        confirmed, evidence = await Verifier(self._model_strong).confirm(
            plan, self._instruction, final_image, final_boxes, claim=claim,
            evidence=toolbox.commit_evidence,
        )
        if confirmed:
            await self._remember(app, intent, plan, toolbox.steps, result, orientation.version)
            return result
        log.info("el verificador no confirmó el resultado")
        await self._penalize(app, intent, hints)
        return replace(
            result,
            estado=TaskStatus.fallida,
            resumen=f"No se pudo confirmar el resultado en pantalla: {evidence}",
        )

    # --- procedure memory: failures are logged and never change the task result -----------------

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
