"""Domain models: frozen, slotted dataclasses. No dependency on infra/api/application."""

from datetime import datetime, timezone
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class TaskStatus(str, Enum):
    # Assumption (open question in design): value set beyond terminada/fallida.
    pendiente = "pendiente"
    en_curso = "en_curso"
    terminada = "terminada"
    fallida = "fallida"
    cancelada = "cancelada"


@dataclass(frozen=True, slots=True)
class Task:
    id: str
    instruction: str
    desktop_url: str
    estado: TaskStatus = TaskStatus.pendiente
    resumen: str = ""
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(frozen=True, slots=True)
class TextBox:
    text: str
    x: int
    y: int
    w: int
    h: int
    conf: float = 1.0

    @property
    def center(self) -> tuple[int, int]:
        return self.x + self.w // 2, self.y + self.h // 2


@dataclass(frozen=True, slots=True)
class Observation:
    boxes: tuple[TextBox, ...] = ()
    fingerprint: str = ""
    timestamp: float = 0.0


@dataclass(frozen=True, slots=True)
class Action:
    kind: str
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Commit:
    """A committed (irreversible) step taken during a task."""

    action: Action
    description: str = ""


@dataclass(frozen=True, slots=True)
class ScriptStep:
    """One replayable step of a learned route. Holds labels and plan field NAMES, never typed values.

    kind: "click" (on the button labeled `anchor`, last seen at x,y), "fill" (the input at offset dx,dy
    from the label `field_label`, typed with the plan value of `field`) or "key" (combo in `anchor`).
    """

    kind: str
    anchor: str = ""
    x: int = 0
    y: int = 0
    field_label: str = ""
    field: str = ""
    commit: bool = False
    dismissal: bool = False
    dx: int = 0
    dy: int = 0


@dataclass(frozen=True, slots=True)
class Procedure:
    """A navigation route that completed a verified task: short step texts, never customer data."""

    app: str
    intent: str
    steps: tuple[str, ...] = ()
    aprendizaje: str = ""
    exitos: int = 0
    app_version: str | None = None  # version shown by the app when the route was learned ("4.2")
    script: tuple[ScriptStep, ...] = ()  # replayable form of the route; empty when it could not be recorded
    fallos_replay: int = 0  # consecutive replays of `script` that stopped or were not verified


@dataclass(frozen=True, slots=True)
class ToolCall:
    """One tool invocation requested by the model."""

    id: str
    name: str
    input: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Decision:
    """A model turn. `raw` holds the assistant content blocks to append back verbatim."""

    actions: tuple[Action, ...] = ()
    reasoning: str = ""
    done: bool = False
    tool_calls: tuple[ToolCall, ...] = ()
    text: str = ""
    stop_reason: str = ""
    raw: tuple[Any, ...] = ()


@dataclass(frozen=True, slots=True)
class Plan:
    """What the task asks for, decided before touching the screen."""

    kind: str
    entity: str = ""
    target: str = ""
    values: tuple[tuple[str, str], ...] = ()
    preconditions: tuple[str, ...] = ()
    planned_commits: int = 0
    feasible: bool = True
    reason: str = ""
    expected_evidence: str = ""


@dataclass(frozen=True, slots=True)
class TaskResult:
    """Outcome of the agent loop before it is turned into the public summary."""

    estado: TaskStatus
    resumen: str = ""
    respuesta: tuple[str, ...] = ()
    datos_razon: tuple[str, ...] = ()
    commits: tuple[str, ...] = ()
    aprendizaje: str = ""


@dataclass(frozen=True, slots=True)
class Classification:
    app: str
    intent: str
    confidence: float = 1.0
    task_type: str | None = None
    destructive: bool | None = None
    two_data: bool | None = None
