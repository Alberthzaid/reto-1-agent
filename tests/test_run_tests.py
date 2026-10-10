import json
import re
from pathlib import Path
from typing import Any

import pytest

from dev_tools import run_tests
from dev_tools.run_tests import (
    desktop_base,
    evaluate,
    format_table,
    leaks,
    load_pruebas,
    missing_keys,
    no_answer_ok,
    parse_args,
    respuesta_matches,
    summarize,
    write_report,
)

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
        ("respuesta: 45 facturas", "45", True),
        ("respuesta: $1.234", "1234", True),
        ("respuesta: $1.234", "1235", False),
        ("respuesta: 3; 1.500.000", "3; 1500000", True),
        ("respuesta: Pereira", "pereira", True),
        ("respuesta: Cali", "Pereira", False),
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
    assert report["pass"] is True
    bad = evaluate(prueba, "terminada", "", {"pass": True, "checks": []})
    assert bad["pass"] is False
    assert bad["checks"]["estado"] is False


IMPOSSIBLE = {"id": "C8", "instruccion": "¿Cuántas naves espaciales hay?", "estado_esperado": "fallida",
              "diff_esperado": [], "lista_blanca": []}


def test_no_answer_ok_for_impossible_question() -> None:
    assert no_answer_ok(IMPOSSIBLE, "No existe ese dato en la aplicación.") is True
    assert no_answer_ok(IMPOSSIBLE, "respuesta: 0\nNo encontré naves.") is False
    expected_answer = {**IMPOSSIBLE, "respuesta_esperada": "3"}
    assert no_answer_ok(expected_answer, "respuesta: 3") is True
    finished = {**IMPOSSIBLE, "estado_esperado": "terminada"}
    assert no_answer_ok(finished, "respuesta: 3") is True


def test_evaluate_flags_answer_on_impossible_question() -> None:
    ok = evaluate(IMPOSSIBLE, "fallida", "No existe el dato.", {"pass": True})
    assert ok["pass"] is True and ok["checks"]["sin_respuesta"] is True
    bad = evaluate(IMPOSSIBLE, "fallida", "respuesta: 0", {"pass": True})
    assert bad["pass"] is False and bad["checks"]["sin_respuesta"] is False


def test_leaks_detects_secrets_only_when_present() -> None:
    secrets = ["leak-fixture-value", "team-token-x", "", "desk.example.test"]
    assert leaks("Cliente creado.", secrets) is False
    assert leaks("Usé la clave leak-fixture-value", secrets) is True
    assert leaks("Conecté a DESK.example.test", secrets) is True


def test_evaluate_fails_when_summary_leaks_a_secret() -> None:
    prueba = json.loads((ROOT / "pruebas" / "E3-facturas-vencidas.json").read_text(encoding="utf-8"))
    report = evaluate(prueba, "terminada", "respuesta: 45\nclave leak-fixture-value", {"pass": True}, ["leak-fixture-value"])
    assert report["checks"]["sin_fugas"] is False
    assert report["pass"] is False
    clean = evaluate(prueba, "terminada", "respuesta: 45", {"pass": True}, ["leak-fixture-value"])
    assert clean["pass"] is True


def _result(id_: str, ok: bool, estado: str, esperado: str, check_pass: bool, segundos: float) -> dict[str, Any]:
    return {"id": id_, "pass": ok, "estado": estado, "estado_esperado": esperado, "segundos": segundos,
            "checks": {"check": check_pass, "estado": estado == esperado}}


def test_summarize_counts_hits_false_successes_and_mean_time() -> None:
    results = [
        _result("E1", True, "terminada", "terminada", True, 10.0),
        _result("E2", False, "terminada", "fallida", True, 20.0),
        _result("E3", False, "terminada", "terminada", False, 30.0),
        _result("E4", False, "fallida", "terminada", False, 40.0),
    ]
    totals = summarize(results)
    assert totals == {"total": 4, "aciertos": 1, "falsos_exitos": 2, "tiempo_medio": 25.0}
    assert summarize([]) == {"total": 0, "aciertos": 0, "falsos_exitos": 0, "tiempo_medio": 0.0}


def test_format_table_lists_failed_check_names() -> None:
    table = format_table([_result("E1", True, "terminada", "terminada", True, 10.0),
                          _result("E2", False, "terminada", "fallida", True, 20.5)])
    lines = table.splitlines()
    assert "id" in lines[0] and "segundos" in lines[0]
    e2 = next(line for line in lines if line.startswith("E2"))
    assert "estado" in e2 and "20.5" in e2 and "no" in e2


def test_write_report_has_results_and_totals(tmp_path: Path) -> None:
    path = tmp_path / "reports" / "r.json"
    results = [_result("E1", True, "terminada", "terminada", True, 10.0)]
    write_report(path, results, summarize(results))
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["totales"]["aciertos"] == 1
    assert data["resultados"][0]["id"] == "E1"


def test_parse_args_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENT_URL", raising=False)
    args = parse_args([])
    assert args.agent_url == "http://localhost:8000"
    assert args.only is None
    assert Path(args.dir) == Path("pruebas")
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}_\d{6}\.json", Path(args.report).name)
    assert Path(args.report).parent == Path("data") / "reports"


def test_parse_args_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_URL", "http://agent.test:9000/")
    args = parse_args(["--only", "E1,e2", "E3", "--dir", "custom_tests", "--report", "out.json"])
    assert args.agent_url == "http://agent.test:9000"
    assert args.only == ["E1", "E2", "E3"]
    assert args.dir == "custom_tests" and args.report == "out.json"
    assert parse_args(["--agent-url", "http://x:1"]).agent_url == "http://x:1"


def test_module_exposes_main() -> None:
    assert callable(run_tests.main)


def test_load_pruebas_reads_any_json_in_dir(tmp_path: Path) -> None:
    (tmp_path / "C1-x.json").write_text(json.dumps({"id": "C1"}), encoding="utf-8")
    assert [p["id"] for p in load_pruebas(tmp_path, None)] == ["C1"]
