"""App identity read in code from the header OCR, used to notice interface changes between runs."""

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass

from agent.domain.models import Procedure, TextBox

HEADER_BAND_PX = 90  # the version is searched in this top band only (never in dialogs or the body)
TOP_MENU_BAND_PX = 64  # application name row plus the top menu row
TITLE_ROW_PX = 30  # boxes whose center is above this belong to the title bar, the rest to the menu
VERSION_RE = re.compile(r"\bv(?:ersion)?\s?(\d+(?:\.\d+)+)\b")
DIGIT_RE = re.compile(r"\d")
UNKNOWN_VERSION = "versión desconocida"


@dataclass(frozen=True, slots=True)
class AppProfile:
    name: str = ""
    version: str | None = None
    labels: tuple[str, ...] = ()


def _fold(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).lower()


def _version(boxes: Sequence[TextBox]) -> str | None:
    for box in sorted(boxes, key=lambda b: (b.y, b.x)):
        if box.center[1] >= HEADER_BAND_PX:
            continue
        match = VERSION_RE.search(_fold(box.text))
        if match:
            return match.group(1)
    return None


def read_profile(boxes: Sequence[TextBox]) -> AppProfile:
    """Name (left of the title bar), version ("v4.2") and top menu labels, without calling a model."""
    top = sorted((b for b in boxes if b.center[1] < TOP_MENU_BAND_PX), key=lambda b: (b.y, b.x))
    plain = [b for b in top if not DIGIT_RE.search(b.text) and b.text.strip()]
    title = [b for b in plain if b.center[1] < TITLE_ROW_PX]
    menu = sorted((b for b in plain if b.center[1] >= TITLE_ROW_PX), key=lambda b: b.x)
    name = min(title, key=lambda b: b.x).text.strip() if title else ""
    return AppProfile(name=name, version=_version(boxes), labels=tuple(b.text.strip() for b in menu))


APP_NAME_RE = re.compile(r"[a-z]{4,}")


def app_key(boxes: Sequence[TextBox]) -> str | None:
    """Memory key of the application: the first word of its title bar ("sigecom").

    OCR often returns the whole title bar as one line with the version and date, and its other words vary
    with OCR noise, so only the leftmost name is stable enough to key procedures by.
    """
    title = [b for b in boxes if b.center[1] < TITLE_ROW_PX and b.text.strip()]
    if not title:
        return None
    first = min(title, key=lambda b: b.x).text
    plain = "".join(
        ch for ch in unicodedata.normalize("NFKD", first) if not unicodedata.combining(ch)
    ).lower()
    match = APP_NAME_RE.search(plain)
    return match.group(0) if match else None


def interface_change(old: str | None, new: str) -> str:
    before = f"v{old}" if old else UNKNOWN_VERSION
    return f"cambio de interfaz detectado: {before} → v{new}"


def select_current(
    procedures: Sequence[Procedure], version: str | None
) -> tuple[list[Procedure], str | None]:
    """Procedures learned on this app version, plus a change message when older ones were dropped.

    Procedures without a version predate versioning; once the version is known they count as stale. When
    the current version cannot be read, nothing is dropped.
    """
    if version is None:
        return list(procedures), None
    current = [p for p in procedures if p.app_version == version]
    stale = [p for p in procedures if p.app_version != version]
    if not stale:
        return current, None
    old = next((p.app_version for p in stale if p.app_version), None)
    return current, interface_change(old, version)
