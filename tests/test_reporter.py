from collections.abc import Iterator

import pytest

from agent.domain.models import TaskResult, TaskStatus
from agent.domain.reporter import build_summary, normalize_number
from agent.observability import log_setup

AUTH_FIXTURE = "redaction-fixture-value"
DESKTOP_TOKEN_FIXTURE = "desktop-auth-fixture"
URL = "wss://desktop.example.test/websockify"


@pytest.fixture(autouse=True)
def _clear_registry() -> Iterator[None]:
    log_setup.clear_secrets()
    yield
    log_setup.clear_secrets()


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("1.234.567", "1234567"),
        ("$ 1.234,50", "1234.50"),
        ("1,5", "1.5"),
        ("45", "45"),
        ("1,234,567", "1234567"),
        ("1.234.567,89", "1234567.89"),
        ("COP 2.000", "2000"),
        ("3.5", "3.5"),
        ("  12  ", "12"),
        ("Pereira", "Pereira"),
        ("CL-0007", "CL-0007"),
        ("14/10/2026", "14/10/2026"),
    ],
)
def test_normalize_number(raw: str, expected: str) -> None:
    assert normalize_number(raw) == expected


def test_answer_line_comes_first_with_normalized_number() -> None:
    result = TaskResult(estado=TaskStatus.terminada, resumen="Conté las facturas vencidas.", respuesta=("1.234",))
    lines = build_summary(result, []).splitlines()
    assert lines[0] == "respuesta: 1234"
    assert 1 < len(lines) <= 4


def test_two_values_are_joined_in_order() -> None:
    result = TaskResult(estado=TaskStatus.terminada, respuesta=("$ 1.234,50", "Pereira"))
    assert build_summary(result, []).splitlines()[0] == "respuesta: 1234.50; Pereira"


def test_no_answer_line_when_question_unanswered() -> None:
    result = TaskResult(estado=TaskStatus.fallida, resumen="Respuesta: no encontré el dato", datos_razon=("Ferretería Ruiz",))
    summary = build_summary(result, [])
    assert not any(line.lower().startswith("respuesta:") for line in summary.splitlines())
    assert "respuesta:" not in summary.lower()
    assert "Ferretería Ruiz" in summary


def test_failure_names_the_reason_data() -> None:
    result = TaskResult(
        estado=TaskStatus.fallida,
        resumen="Hay dos clientes llamados Ana Torres.",
        datos_razon=("52345678", "1019876543"),
    )
    summary = build_summary(result, [])
    assert "52345678" in summary and "1019876543" in summary
    assert len(summary.splitlines()) <= 3


def test_done_lists_commits() -> None:
    result = TaskResult(estado=TaskStatus.terminada, resumen="Cliente creado.", commits=("Guardar",))
    summary = build_summary(result, [])
    assert "Guardar" in summary


def test_summary_lists_the_crop_commit_label() -> None:
    result = TaskResult(estado=TaskStatus.terminada, resumen="Cliente creado.", commits=("Guardar",))
    summary = build_summary(result, [])
    assert "Acciones confirmadas: Guardar" in summary
    assert "Exportar" not in summary


def test_empty_result_still_has_a_line() -> None:
    assert build_summary(TaskResult(estado=TaskStatus.fallida), []).strip()


def test_secrets_are_removed() -> None:
    log_setup.register_secret("registered-secret-fixture")
    result = TaskResult(
        estado=TaskStatus.fallida,
        resumen=f"Conecté a {URL} con {AUTH_FIXTURE} y token {DESKTOP_TOKEN_FIXTURE}; registered-secret-fixture",
        datos_razon=(AUTH_FIXTURE,),
    )
    summary = build_summary(result, [AUTH_FIXTURE, DESKTOP_TOKEN_FIXTURE, URL, "desktop.example.test", ""])
    for secret in (AUTH_FIXTURE, DESKTOP_TOKEN_FIXTURE, URL, "desktop.example.test", "registered-secret-fixture"):
        assert secret not in summary


def test_multiline_resumen_is_collapsed_to_three_lines() -> None:
    result = TaskResult(
        estado=TaskStatus.terminada,
        resumen="uno\ndos\ntres\ncuatro\ncinco",
        commits=("Guardar",),
        respuesta=("7",),
    )
    lines = build_summary(result, []).splitlines()
    assert lines[0] == "respuesta: 7"
    assert len(lines) <= 4
