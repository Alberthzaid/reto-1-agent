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
class Procedure:
    app: str
    intent: str
    steps: tuple[Action, ...] = ()


@dataclass(frozen=True, slots=True)
class Decision:
    actions: tuple[Action, ...] = ()
    reasoning: str = ""
    done: bool = False


@dataclass(frozen=True, slots=True)
class Classification:
    app: str
    intent: str
    confidence: float = 1.0
