import json
from pathlib import Path

import pytest

from dev_tools import run_tests
from dev_tools.run_tests import desktop_base, evaluate, load_pruebas, missing_keys, respuesta_matches

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize(
    "url,expected",
    [
        ("wss://desk.example.test/websockify", "https://desk.example.test"),
        ("ws://127.0.0.1:6080/websockify", "http://127.0.0.1:6080"),
    ],
)
def test_desktop_base(url: str, expected: str) -> None:
    assert desktop_base(url) == expected


def test_desktop_base_rejects_other_schemes() -> None:
    with pytest.raises(ValueError):
        desktop_base("https://desk.example.test")


def test_load_pruebas_filters_by_id() -> None:
    pruebas = load_pruebas(ROOT / "pruebas", ["E1", "E2"])
    assert [p["id"] for p in pruebas] == ["E1", "E2"]
    assert len(load_pruebas(ROOT / "pruebas", None)) >= 5


@pytest.mark.parametrize(
    "resumen,esperada,ok",
    [
        ("respuesta: 45\nConté las facturas.", "45", True),
        ("respuesta: 45.0", "45", True),
        ("respuesta: 44", "45", False),
        ("Conté 45 facturas", "45", False),
        ("respuesta: 1234.50; Pereira", "1234.5; pereira", True),
        ("respuesta: 12; Cali", "12; Pereira", False),
    ],
)
def test_respuesta_matches(resumen: str, esperada: str, ok: bool) -> None:
    assert respuesta_matches(resumen, esperada) is ok


def test_missing_keys() -> None:
    assert missing_keys("Datos: 52345678; 1019876543", ["52345678", "1019876543"]) == []
    assert missing_keys("Datos: 52345678", ["52345678", "1019876543"]) == ["1019876543"]


def test_evaluate_combines_check_and_state() -> None:
    prueba = json.loads((ROOT / "pruebas" / "E5-homonimos.json").read_text(encoding="utf-8"))
    report = evaluate(prueba, "fallida", "Datos: 52345678; 1019876543", {"pass": True, "checks": []})
    assert report["ok"] is True
    bad = evaluate(prueba, "terminada", "", {"pass": True, "checks": []})
    assert bad["ok"] is False


def test_module_exposes_main() -> None:
    assert callable(run_tests.main)
