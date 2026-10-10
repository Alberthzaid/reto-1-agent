"""Deterministic, secret-free task summaries for the evaluator."""

import re
from collections.abc import Iterable

from agent.domain.models import TaskResult, TaskStatus
from agent.observability.log_setup import redact

_CURRENCY = re.compile(r"^(?:\$|€|COP|USD)\s*", re.IGNORECASE)
_NUMERIC = re.compile(r"^-?\d[\d.,]*$")
_DOT_THOUSANDS = re.compile(r"^-?\d{1,3}(?:\.\d{3})+$")
_COMMA_THOUSANDS = re.compile(r"^-?\d{1,3}(?:,\d{3})+$")
_ANSWER_PREFIX = re.compile(r"respuesta\s*:", re.IGNORECASE)
MAX_BODY_LINES = 3
MAX_LINE_CHARS = 300


def normalize_number(value: str) -> str:
    """Drop currency and thousands separators; use '.' as decimal mark. Text is left untouched."""
    text = value.strip()
    compact = _CURRENCY.sub("", text).replace(" ", "")
    if not _NUMERIC.match(compact):
        return text
    if "." in compact and "," in compact:
        decimal = "." if compact.rfind(".") > compact.rfind(",") else ","
        thousands = "," if decimal == "." else "."
        return compact.replace(thousands, "").replace(decimal, ".")
    if "." in compact:
        return compact.replace(".", "") if _DOT_THOUSANDS.match(compact) else compact
    if "," in compact:
        return compact.replace(",", "") if _COMMA_THOUSANDS.match(compact) else compact.replace(",", ".")
    return compact


def _clean(text: str) -> str:
    """One line, no stray 'respuesta:' marker (only the first line may carry it)."""
    flat = " ".join(text.split())
    flat = _ANSWER_PREFIX.sub("respuesta", flat)
    return flat[:MAX_LINE_CHARS]


def build_summary(result: TaskResult, secrets: Iterable[str]) -> str:
    """`respuesta: <v>` first (when answered), then up to three lines of what happened."""
    lines: list[str] = []
    if result.respuesta:
        values = "; ".join(_clean(normalize_number(value)) for value in result.respuesta)
        lines.append(f"respuesta: {values}")
    body: list[str] = []
    if result.resumen.strip():
        body.append(_clean(result.resumen))
    if result.datos_razon:
        body.append(_clean("Datos: " + "; ".join(result.datos_razon)))
    if result.commits:
        body.append(_clean("Acciones confirmadas: " + "; ".join(result.commits)))
    if not body:
        body.append("Tarea completada." if result.estado is TaskStatus.terminada else "No se completó la tarea.")
    lines.extend(body[:MAX_BODY_LINES])
    return _scrub("\n".join(lines), secrets)


def _scrub(text: str, secrets: Iterable[str]) -> str:
    cleaned = redact(text)
    for secret in sorted((s for s in secrets if s), key=len, reverse=True):
        cleaned = cleaned.replace(secret, "***")
    return cleaned
