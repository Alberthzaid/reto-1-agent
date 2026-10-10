"""Write guard: classifies clicks as commits and blocks unplanned or unverified ones."""

import math
import re
import unicodedata
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass

from agent.domain.models import TextBox

# Buttons that write by themselves, whatever was done before.
STRONG_COMMIT_WORDS: frozenset[str] = frozenset(
    {
        "guardar", "grabar", "aplicar", "confirmar", "eliminar", "borrar", "anular", "enviar", "exportar",
        "actualizar",
    }
)
# Buttons that usually only open a form ("Crear cliente" on a list toolbar); they write only when data was
# entered since the last navigation.
WEAK_COMMIT_WORDS: frozenset[str] = frozenset({"crear", "nuevo", "registrar"})
# Words that, right after a commit, acknowledge or confirm it instead of starting a new one. With no prior
# commit and no data entered they just close an informative dialog.
CONFIRM_WORDS: frozenset[str] = frozenset({"si", "aceptar", "confirmar"})
COMMIT_WORDS: frozenset[str] = STRONG_COMMIT_WORDS | WEAK_COMMIT_WORDS | CONFIRM_WORDS
# Buttons that close a dialog or a form without writing ("Entendido" in a release-notes modal).
DISMISS_WORDS: frozenset[str] = frozenset({"entendido", "cerrar"})
# Code the app assigns to a saved record, e.g. "CL-0141" in the title "Cliente CL-0141".
ASSIGNED_CODE_RE = re.compile(r"\b[A-Z]{2,4}-\d{3,6}\b")
NEAR_RADIUS_PX = 40.0
HIT_PADDING_PX = 2
MODIFIER_ORDER: tuple[str, ...] = ("ctrl", "alt", "shift", "meta", "super")


@dataclass(frozen=True, slots=True)
class GuardVerdict:
    allowed: bool
    is_commit: bool = False
    reason: str = ""
    label: str = ""
    dismissal: bool = False
    weak: bool = False


def normalize_text(text: str) -> str:
    """Lowercase and strip accents ("Sí" -> "si")."""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).lower()


def words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", normalize_text(text)))


def assigned_codes(text: str) -> set[str]:
    """Record codes such as "CL-0141" in an OCR text."""
    return set(ASSIGNED_CODE_RE.findall(text))


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
        self._data_entered = False
        self._saved_code: str | None = None

    @property
    def commits(self) -> tuple[str, ...]:
        return tuple(self._commits)

    @property
    def data_entered(self) -> bool:
        """Data was typed since the last commit or navigation."""
        return self._data_entered

    def _is_confirmation(self, label: str) -> bool:
        found = words(label) & COMMIT_WORDS
        return self._awaiting_confirmation and bool(found) and found <= CONFIRM_WORDS

    def check_click(
        self, x: int, y: int, boxes: Sequence[TextBox], visible_codes: Collection[str] = ()
    ) -> GuardVerdict:
        """`visible_codes` are the record codes on screen now; they tell whether a saved form is still open."""
        label = label_at(x, y, boxes)
        found = words(label)
        commit_words = found & COMMIT_WORDS
        if not commit_words:
            return GuardVerdict(allowed=True, label=label, dismissal=bool(found & DISMISS_WORDS))
        if self._is_confirmation(label):
            return GuardVerdict(allowed=True, is_commit=True, label=label)
        if commit_words <= CONFIRM_WORDS and not self._data_entered and not self._commits:
            return GuardVerdict(allowed=True, label=label, dismissal=True)
        weak = not commit_words & (STRONG_COMMIT_WORDS | CONFIRM_WORDS)
        if weak and not self._data_entered:
            return GuardVerdict(allowed=True, label=label)
        return self.check_commit(label, visible_codes, weak)

    def check_commit(self, label: str, visible_codes: Collection[str] = (), weak: bool = False) -> GuardVerdict:
        """Commit limits for a click already known to write (by its label or by what it did before)."""
        if len(self._commits) >= self.planned_commits:
            done = ", ".join(self._commits) or "ninguno"
            return GuardVerdict(
                allowed=False,
                is_commit=True,
                label=label,
                weak=weak,
                reason=(
                    f"Clic bloqueado en '{label}': es una acción que confirma cambios y el plan solo "
                    f"permite {self.planned_commits} (ya hechos: {done}). Repetirla podría duplicar "
                    "registros. No la pulses otra vez: usa wait o zoom y revisa el título (código asignado) "
                    "o el mensaje de éxito; luego llama a finish."
                ),
            )
        if self._saved_code is not None and self._saved_code in visible_codes and not self._data_entered:
            return GuardVerdict(
                allowed=False,
                is_commit=True,
                label=label,
                weak=weak,
                reason=(
                    f"Clic bloqueado en '{label}': el formulario ya muestra el código asignado "
                    f"{self._saved_code}, así que el registro ya se guardó. Pulsarlo otra vez sin cambiar "
                    "datos crearía un duplicado. Revisa el título o el mensaje de éxito (zoom o wait)."
                ),
            )
        if self._pending_verification:
            return GuardVerdict(
                allowed=False,
                is_commit=True,
                label=label,
                weak=weak,
                reason=(
                    f"Clic bloqueado en '{label}': aún no verificaste el resultado de la confirmación "
                    "anterior. Lee la pantalla (zoom o wait) y revisa el título o el mensaje de éxito antes "
                    "de confirmar otra vez."
                ),
            )
        return GuardVerdict(allowed=True, is_commit=True, label=label, weak=weak)

    def record_commit(self, label: str) -> None:
        """Register a commit click once executed; it counts even if the screen has not changed yet."""
        if self._is_confirmation(label):
            self._awaiting_confirmation = False
        else:
            self._commits.append(label)
            self._awaiting_confirmation = True
        self._data_entered = False
        self._pending_verification = True

    def note_saved_code(self, code: str) -> None:
        """The last commit made the app show this assigned record code (e.g. in the form title)."""
        self._saved_code = code

    def mark_verified(self) -> None:
        """A read-type action happened after the last commit."""
        self._pending_verification = False

    def note_data_entry(self) -> None:
        """Typing or keys mean new work: later confirmations are new commits."""
        self._pending_verification = False
        self._awaiting_confirmation = False
        self._data_entered = True

    def note_navigation(self) -> None:
        """The screen was replaced (menu, list, new form): data typed before no longer applies."""
        self._data_entered = False
        self._saved_code = None

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
