"""Dates as the application shows them, and relative dates computed from that 'today'."""

import re
import unicodedata
from collections.abc import Sequence
from datetime import date, timedelta

from agent.domain.models import TextBox

HEADER_BAND_PX = 90
LABEL_WORDS: frozenset[str] = frozenset({"hoy", "fecha"})
SAME_LINE_PX = 12  # max vertical distance between box centers on one OCR line
NEAR_LABEL_PX = 300  # max horizontal gap from a label to its value

NO_DATE_NOTE = (
    "The app does not show today's date; do not assume the system date. "
    "Read it from the screen if the task needs it."
)

MONTHS: dict[str, int] = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6, "julio": 7, "agosto": 8,
    "septiembre": 9, "setiembre": 9, "octubre": 10, "noviembre": 11, "diciembre": 12,
}
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")

_ISO = re.compile(r"(?<!\d)(\d{4})-(\d{1,2})-(\d{1,2})(?!\d)")
_DMY = re.compile(r"(?<!\d)(\d{1,2})[/-](\d{1,2})[/-](\d{4})(?!\d)")
_LONG = re.compile(r"(?<!\d)(\d{1,2})\s+de\s+([a-z]+)\s+(?:de|del)\s+(\d{4})(?!\d)")


def _normalize(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).lower()


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def find_dates(text: str) -> list[date]:
    """Every valid date in the text, in order of appearance; invalid ones (31/02) are skipped."""
    normalized = _normalize(text)
    found: list[tuple[int, date | None]] = []
    for match in _ISO.finditer(normalized):
        found.append((match.start(), _safe_date(int(match[1]), int(match[2]), int(match[3]))))
    for match in _DMY.finditer(normalized):
        found.append((match.start(), _safe_date(int(match[3]), int(match[2]), int(match[1]))))
    for match in _LONG.finditer(normalized):
        month = MONTHS.get(match[2])
        if month is not None:
            found.append((match.start(), _safe_date(int(match[3]), month, int(match[1]))))
    return [value for _, value in sorted(found, key=lambda item: item[0]) if value is not None]


def parse_date(text: str) -> date | None:
    """First valid date in the text (dd/mm/yyyy, dd-mm-yyyy, yyyy-mm-dd or '10 de octubre de 2026')."""
    dates = find_dates(text)
    return dates[0] if dates else None


def _has_label(box: TextBox) -> bool:
    return bool(set(re.findall(r"[a-z]+", _normalize(box.text))) & LABEL_WORDS)


def _same_line(a: TextBox, b: TextBox) -> bool:
    return abs(a.center[1] - b.center[1]) <= max(SAME_LINE_PX, min(a.h, b.h) // 2)


def _labeled_date(boxes: Sequence[TextBox]) -> date | None:
    for label in (box for box in boxes if _has_label(box)):
        own = parse_date(label.text)
        if own is not None:
            return own
        neighbors = sorted(
            (box for box in boxes if box is not label and _same_line(label, box)
             and 0 <= box.x - (label.x + label.w) <= NEAR_LABEL_PX),
            key=lambda box: box.x,
        )
        for box in neighbors:
            value = parse_date(box.text)
            if value is not None:
                return value
    return None


def parse_app_date(boxes: Sequence[TextBox]) -> date | None:
    """Today's date as shown by the app: a 'Hoy'/'Fecha' label first, else the only date in the header."""
    labeled = _labeled_date(boxes)
    if labeled is not None:
        return labeled
    header = {value for box in boxes if box.y < HEADER_BAND_PX for value in find_dates(box.text)}
    return header.pop() if len(header) == 1 else None


def _fmt(value: date) -> str:
    return f"{value.isoformat()} ({value.strftime('%d/%m/%Y')})"


def relative_context(today: date) -> str:
    """Relative dates for the prompt, each in ISO and dd/mm/yyyy, computed from the app's today."""
    lines = [
        f"Today: {WEEKDAYS[today.weekday()]} {_fmt(today)}",
        f"Yesterday: {_fmt(today - timedelta(days=1))}",
        f"Day before yesterday: {_fmt(today - timedelta(days=2))}",
    ]
    for index, name in enumerate(WEEKDAYS):
        back = (today.weekday() - index) % 7 or 7
        ahead = (index - today.weekday()) % 7 or 7
        lines.append(
            f"{name}: most recent {_fmt(today - timedelta(days=back))}; next {_fmt(today + timedelta(days=ahead))}"
        )
    week_start = today - timedelta(days=today.weekday())
    lines.append(f"This week (Monday-Sunday): {_fmt(week_start)} to {_fmt(week_start + timedelta(days=6))}")
    last_month_end = today.replace(day=1) - timedelta(days=1)
    lines.append(f"Last month: {_fmt(last_month_end.replace(day=1))} to {_fmt(last_month_end)}")
    return "\n".join(lines)
