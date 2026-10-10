"""Write guard: classifies clicks as commits and blocks unplanned or unverified ones."""

import math
import re
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from agent.domain.models import TextBox

COMMIT_WORDS: frozenset[str] = frozenset(
    {
        "guardar", "grabar", "aceptar", "confirmar", "si", "eliminar", "borrar", "anular",
        "crear", "registrar", "enviar", "exportar", "actualizar", "aplicar",
    }
)
# Words that, right after a commit, acknowledge or confirm it instead of starting a new one.
CONFIRM_WORDS: frozenset[str] = frozenset({"si", "aceptar", "confirmar"})
NEAR_RADIUS_PX = 40.0
HIT_PADDING_PX = 2
MODIFIER_ORDER: tuple[str, ...] = ("ctrl", "alt", "shift", "meta", "super")


@dataclass(frozen=True, slots=True)
class GuardVerdict:
    allowed: bool
    is_commit: bool = False
    reason: str = ""
    label: str = ""


def normalize_text(text: str) -> str:
    """Lowercase and strip accents ("Sí" -> "si")."""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).lower()


def words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", normalize_text(text)))


def normalize_combo(combo: str) -> str:
    parts = [part.strip().lower() for part in combo.split("+") if part.strip()]
    parts = ["ctrl" if part == "control" else part for part in parts]
    if not parts:
        return ""
    *mods, key = parts
    ordered = sorted(set(mods), key=lambda m: MODIFIER_ORDER.index(m) if m in MODIFIER_ORDER else len(MODIFIER_ORDER))
    return "+".join([*ordered, key])


def label_at(x: int, y: int, boxes: Sequence[TextBox], radius: float = NEAR_RADIUS_PX) -> str:
    """Text of the box containing the point, else the nearest one within `radius` pixels."""
    best: tuple[float, str] | None = None
    for box in boxes:
        dx = max(box.x - HIT_PADDING_PX - x, 0, x - (box.x + box.w + HIT_PADDING_PX))
        dy = max(box.y - HIT_PADDING_PX - y, 0, y - (box.y + box.h + HIT_PADDING_PX))
        distance = math.hypot(dx, dy)
        if distance <= radius and (best is None or distance < best[0]):
            best = (distance, box.text)
    return best[1] if best else ""


class Guard:
    """Tracks commits for one task. Error reasons are written for the LLM, in Spanish."""

    def __init__(self, blocked_combos: Iterable[str], planned_commits: int) -> None:
        self._blocked = frozenset(normalize_combo(combo) for combo in blocked_combos)
        self.planned_commits = planned_commits
        self._commits: list[str] = []
        self._pending_verification = False
        self._awaiting_confirmation = False

    @property
    def commits(self) -> tuple[str, ...]:
        return tuple(self._commits)

    def _is_confirmation(self, label: str) -> bool:
        found = words(label) & COMMIT_WORDS
        return self._awaiting_confirmation and bool(found) and found <= CONFIRM_WORDS

    def check_click(self, x: int, y: int, boxes: Sequence[TextBox]) -> GuardVerdict:
        label = label_at(x, y, boxes)
        if not words(label) & COMMIT_WORDS:
            return GuardVerdict(allowed=True, is_commit=False, label=label)
        if self._is_confirmation(label):
            return GuardVerdict(allowed=True, is_commit=True, label=label)
        if len(self._commits) >= self.planned_commits:
            done = ", ".join(self._commits) or "ninguno"
            return GuardVerdict(
                allowed=False,
                is_commit=True,
                label=label,
                reason=(
                    f"Clic bloqueado en '{label}': es una acción que confirma cambios y el plan solo "
                    f"permite {self.planned_commits} (ya hechos: {done}). Repetirla podría duplicar "
                    "registros. Verifica en pantalla el resultado y llama a finish."
                ),
            )
        if self._pending_verification:
            return GuardVerdict(
                allowed=False,
                is_commit=True,
                label=label,
                reason=(
                    f"Clic bloqueado en '{label}': la confirmación anterior cambió la pantalla y aún no "
                    "verificaste el resultado. Lee la pantalla (zoom o wait) antes de confirmar otra vez."
                ),
            )
        return GuardVerdict(allowed=True, is_commit=True, label=label)

    def record_commit(self, label: str, changed: bool) -> None:
        """Register a commit click once executed. A click that changed nothing is not a commit."""
        if not changed:
            return
        if self._is_confirmation(label):
            self._awaiting_confirmation = False
        else:
            self._commits.append(label)
            self._awaiting_confirmation = True
        self._pending_verification = True

    def mark_verified(self) -> None:
        """A read-type action happened after the last commit."""
        self._pending_verification = False

    def note_data_entry(self) -> None:
        """Typing or keys mean new work: later confirmations are new commits."""
        self._pending_verification = False
        self._awaiting_confirmation = False

    def check_key(self, combo: str) -> GuardVerdict:
        if normalize_combo(combo) in self._blocked:
            return GuardVerdict(
                allowed=False,
                reason=(
                    f"Combinación bloqueada: '{combo}'. Puede cerrar o recargar la aplicación; "
                    "usa los botones de la pantalla."
                ),
            )
        return GuardVerdict(allowed=True)
