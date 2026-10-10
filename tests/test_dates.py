from datetime import date

import pytest

from agent.domain.dates import NO_DATE_NOTE, parse_app_date, parse_date, relative_context
from agent.domain.models import TextBox

TODAY = date(2026, 10, 10)  # Saturday


@pytest.mark.parametrize(
    "text",
    [
        "10/10/2026",
        "10-10-2026",
        "2026-10-10",
        "10 de octubre de 2026",
        "sábado, 10 de octubre de 2026",
        "SABADO, 10 DE OCTUBRE DE 2026",
        "Sábado 10 de Octubre del 2026",
        "Hoy: 10/10/2026",
    ],
)
def test_parse_date_formats(text: str) -> None:
    assert parse_date(text) == TODAY


@pytest.mark.parametrize("text", ["31/02/2026", "2026-13-01", "32 de enero de 2026", "sin fecha", "", "10/10"])
def test_parse_date_rejects_invalid_or_missing(text: str) -> None:
    assert parse_date(text) is None


def test_parse_date_single_digit_day_and_month() -> None:
    assert parse_date("5/1/2026") == date(2026, 1, 5)


def box(text: str, x: int, y: int, w: int = 120, h: int = 20) -> TextBox:
    return TextBox(text=text, x=x, y=y, w=w, h=h)


def test_app_date_from_hoy_label_in_the_same_box() -> None:
    assert parse_app_date([box("Hoy: 14/10/2026", 1000, 10)]) == date(2026, 10, 14)


def test_app_date_from_label_in_a_nearby_box_on_the_same_line() -> None:
    boxes = [box("Fecha:", 900, 300, 50), box("10/10/2026", 960, 302, 90), box("01/01/2020", 960, 340, 90)]
    assert parse_app_date(boxes) == TODAY


def test_labeled_date_wins_over_table_and_header_dates() -> None:
    boxes = [
        box("05/10/2026", 100, 20),  # header, unlabeled
        box("Hoy: 10/10/2026", 1000, 400),
        box("03/10/2026", 100, 500),  # table row
    ]
    assert parse_app_date(boxes) == TODAY


def test_single_header_date_without_label() -> None:
    boxes = [box("SIGECOM", 10, 10), box("sábado, 10 de octubre de 2026", 900, 15, 300), box("09/10/2026", 100, 400)]
    assert parse_app_date(boxes) == TODAY


def test_two_different_header_dates_are_ambiguous() -> None:
    assert parse_app_date([box("10/10/2026", 900, 10), box("09/10/2026", 600, 10)]) is None


def test_date_only_in_the_table_body_is_not_today() -> None:
    boxes = [box("SIGECOM", 10, 10), box("Fecha", 100, 300), box("09/10/2026", 100, 330), box("08/10/2026", 100, 360)]
    assert parse_app_date(boxes) is None


def test_no_dates_at_all() -> None:
    assert parse_app_date([box("Clientes", 10, 10)]) is None
    assert parse_app_date([]) is None


@pytest.mark.parametrize(
    "expected",
    [
        "2026-10-10", "10/10/2026",  # today
        "2026-10-09", "09/10/2026",  # yesterday
        "2026-10-08", "08/10/2026",  # day before yesterday
        "2026-10-05", "05/10/2026",  # most recent Monday
        "2026-10-12", "12/10/2026",  # next Monday
        "2026-10-03", "03/10/2026",  # most recent Saturday
        "2026-10-17", "17/10/2026",  # next Saturday
        "2026-10-11", "11/10/2026",  # end of this week
        "2026-09-01", "01/09/2026",  # last month start
        "2026-09-30", "30/09/2026",  # last month end
    ],
)
def test_relative_context_contains_dates_in_both_formats(expected: str) -> None:
    assert expected in relative_context(TODAY)


def _line(text: str, prefix: str) -> str:
    return next(line for line in text.splitlines() if line.startswith(prefix))


def test_relative_context_lines() -> None:
    text = relative_context(TODAY)
    assert "2026-10-09" in _line(text, "Yesterday")
    assert "2026-10-08" in _line(text, "Day before yesterday")
    monday = _line(text, "Monday")
    assert "most recent 2026-10-05" in monday and "next 2026-10-12" in monday
    saturday = _line(text, "Saturday")
    assert "most recent 2026-10-03" in saturday and "next 2026-10-17" in saturday
    week = _line(text, "This week")
    assert "2026-10-05" in week and "2026-10-11" in week
    month = _line(text, "Last month")
    assert "2026-09-01" in month and "2026-09-30" in month


def test_last_month_wraps_the_year() -> None:
    month = _line(relative_context(date(2026, 1, 15)), "Last month")
    assert "2025-12-01" in month and "2025-12-31" in month


def test_no_date_note_warns_against_system_date() -> None:
    assert "system date" in NO_DATE_NOTE
