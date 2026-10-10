"""Run the local agent against the challenge `pruebas/` and check results with /__check.

Usage: uv run python -m dev_tools.run_tests [--only E1,E2] [--dir pruebas] [--agent-url URL] [--report PATH]

Environment: DESKTOP_URL, DESKTOP_TOKEN, DESKTOP_PASSWORD, AGENT_URL (default http://localhost:8000),
TEAM_TOKEN. Secrets are registered for redaction and never logged nor written to the report.
"""

import argparse
import asyncio
import json
import logging
import os
import re
import sys
import time
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from dotenv import load_dotenv

from agent.domain.reporter import normalize_number
from agent.observability.log_setup import redact, register_secret, setup_logging

log = logging.getLogger("run_tests")

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DIR = "pruebas"
DEFAULT_AGENT_URL = "http://localhost:8000"
REPORTS_DIR = Path("data") / "reports"
POLL_S = 5.0
TASK_TIMEOUT_S = 300.0
FINAL_STATES = {"terminada", "fallida", "cancelada"}
_NUMBER = re.compile(r"-?\d[\d.,]*")


def desktop_base(url: str) -> str:
    """https://<host> for a wss://<host>/websockify URL (http for ws)."""
    parsed = urlparse(url)
    schemes = {"wss": "https", "ws": "http"}
    if parsed.scheme not in schemes or not parsed.netloc:
        raise ValueError("DESKTOP_URL debe ser ws:// o wss://")
    return f"{schemes[parsed.scheme]}://{parsed.netloc}"


def load_pruebas(directory: Path, only: list[str] | None) -> list[dict[str, Any]]:
    pruebas = [json.loads(path.read_text(encoding="utf-8")) for path in sorted(directory.glob("*.json"))]
    if only:
        wanted = {item.upper() for item in only}
        pruebas = [p for p in pruebas if str(p.get("id", "")).upper() in wanted]
    return pruebas


def _answer_line(resumen: str) -> str | None:
    for line in resumen.splitlines():
        if line.strip().lower().startswith("respuesta:"):
            return line.split(":", 1)[1].strip()
    return None


def _number(text: str) -> float | None:
    """First number in the text; '$1.234' and '1.234' read as 1234 (thousands separator)."""
    match = _NUMBER.search(text)
    if match is None:
        return None
    try:
        return float(normalize_number(match.group().rstrip(".,")))
    except ValueError:
        return None


def _same(reported: str, expected: str) -> bool:
    reported_number = _number(reported)
    expected_number = _number(expected)
    if reported_number is not None and expected_number is not None:
        return reported_number == expected_number
    return reported.strip().lower() == expected.strip().lower()


def respuesta_matches(resumen: str, esperada: str) -> bool:
    """Compare the `respuesta:` line value by value (first number of each part)."""
    line = _answer_line(resumen)
    if line is None:
        return False
    reported = [part.strip() for part in line.split(";")]
    expected = [part.strip() for part in esperada.split(";")]
    return len(reported) >= len(expected) and all(_same(r, e) for r, e in zip(reported, expected))


def missing_keys(resumen: str, claves: list[str]) -> list[str]:
    return [clave for clave in claves if clave not in resumen]


def no_answer_ok(prueba: dict[str, Any], resumen: str) -> bool:
    """An impossible question (expected `fallida`, no expected answer) must not carry a `respuesta:` line."""
    if prueba.get("estado_esperado") != "fallida" or "respuesta_esperada" in prueba:
        return True
    return _answer_line(resumen) is None


def leaks(resumen: str, secrets: Sequence[str]) -> bool:
    """True when the summary contains any non-empty secret (case-insensitive)."""
    lowered = resumen.lower()
    return any(secret and secret.lower() in lowered for secret in secrets)


def evaluate(
    prueba: dict[str, Any],
    estado: str,
    resumen: str,
    check: dict[str, Any],
    secrets: Sequence[str] = (),
) -> dict[str, Any]:
    """Named boolean checks plus `pass` (all of them)."""
    checks: dict[str, bool] = {
        "check": bool(check.get("pass")),
        "estado": estado == prueba.get("estado_esperado"),
        "sin_respuesta": no_answer_ok(prueba, resumen),
        "sin_fugas": not leaks(resumen, secrets),
    }
    report: dict[str, Any] = {
        "id": prueba.get("id"),
        "estado": estado,
        "estado_esperado": prueba.get("estado_esperado"),
        "checks": checks,
        "check_detalle": check.get("checks", []),
    }
    if "respuesta_esperada" in prueba:
        checks["respuesta"] = respuesta_matches(resumen, str(prueba["respuesta_esperada"]))
    if prueba.get("claves_razon"):
        report["claves_faltantes"] = missing_keys(resumen, list(prueba["claves_razon"]))
        checks["claves"] = not report["claves_faltantes"]
    report["pass"] = all(checks.values())
    return report


def is_false_success(result: dict[str, Any]) -> bool:
    """The agent said `terminada` but the task should fail or the desktop check rejected it."""
    if result.get("estado") != "terminada":
        return False
    return result.get("estado_esperado") == "fallida" or not result.get("checks", {}).get("check", False)


def summarize(results: Sequence[dict[str, Any]]) -> dict[str, Any]:
    times = [float(r.get("segundos", 0.0)) for r in results]
    return {
        "total": len(results),
        "aciertos": sum(1 for r in results if r.get("pass")),
        "falsos_exitos": sum(1 for r in results if is_false_success(r)),
        "tiempo_medio": round(sum(times) / len(times), 1) if times else 0.0,
    }


def format_table(results: Sequence[dict[str, Any]]) -> str:
    header = ("id", "pass", "checks", "estado", "segundos")
    rows = [header]
    for r in results:
        failed = ",".join(name for name, ok in r.get("checks", {}).items() if not ok) or "-"
        rows.append((str(r.get("id", "?")), "si" if r.get("pass") else "no", failed, str(r.get("estado", "?")),
                     f"{float(r.get('segundos', 0.0)):.1f}"))
    widths = [max(len(row[i]) for row in rows) for i in range(len(header))]
    return "\n".join(" | ".join(cell.ljust(width) for cell, width in zip(row, widths)).rstrip() for row in rows)


def write_report(path: Path, results: Sequence[dict[str, Any]], totals: dict[str, Any]) -> None:
    """JSON report; summaries are redacted with the registered secrets before writing."""
    clean = [{**r, "resumen": redact(str(r["resumen"]))} if "resumen" in r else dict(r) for r in results]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"resultados": clean, "totales": totals}, ensure_ascii=False, indent=2),
                    encoding="utf-8")


async def run_prueba(
    client: httpx.AsyncClient,
    prueba: dict[str, Any],
    env: dict[str, str],
) -> dict[str, Any]:
    base = desktop_base(env["DESKTOP_URL"])
    desktop_headers = {"Authorization": f"Bearer {env['DESKTOP_TOKEN']}"}
    agent_headers = {"Authorization": f"Bearer {env['TEAM_TOKEN']}"} if env.get("TEAM_TOKEN") else {}
    log.info("%s: reiniciando el escritorio", prueba["id"])
    (await client.post(f"{base}/__reset", headers=desktop_headers)).raise_for_status()
    created = await client.post(
        f"{env['AGENT_URL']}/tasks",
        headers=agent_headers,
        json={"tarea": prueba["instruccion"], "escritorio": {"url": env["DESKTOP_URL"], "password": env["DESKTOP_PASSWORD"]}},
    )
    created.raise_for_status()
    task_id = created.json()["task_id"]
    started = time.monotonic()
    estado, resumen = "pendiente", ""
    while time.monotonic() - started < TASK_TIMEOUT_S:
        await asyncio.sleep(POLL_S)
        status = await client.get(f"{env['AGENT_URL']}/tasks/{task_id}", headers=agent_headers)
        status.raise_for_status()
        body = status.json()
        estado, resumen = body["estado"], body.get("resumen", "")
        if estado in FINAL_STATES:
            break
    elapsed = time.monotonic() - started
    check = await client.post(f"{base}/__check", headers=desktop_headers, json=prueba)
    check.raise_for_status()
    report = evaluate(prueba, estado, resumen, check.json(), secrets(env))
    report["segundos"] = round(elapsed, 1)
    report["resumen"] = resumen
    return report


def secrets(env: dict[str, str]) -> list[str]:
    """Values that must never appear in an agent summary."""
    host = urlparse(env.get("DESKTOP_URL", "")).hostname or ""
    names = ("DESKTOP_PASSWORD", "TEAM_TOKEN", "DESKTOP_TOKEN", "DESKTOP_URL")
    return [value for value in (*(env.get(name, "") for name in names), host) if value]


def _env(agent_url: str) -> dict[str, str]:
    env = {
        "DESKTOP_URL": os.environ.get("DESKTOP_URL", ""),
        "DESKTOP_TOKEN": os.environ.get("DESKTOP_TOKEN", ""),
        "DESKTOP_PASSWORD": os.environ.get("DESKTOP_PASSWORD", ""),
        "AGENT_URL": agent_url.rstrip("/"),
        "TEAM_TOKEN": os.environ.get("TEAM_TOKEN", ""),
    }
    for name in ("DESKTOP_TOKEN", "DESKTOP_PASSWORD", "TEAM_TOKEN", "DESKTOP_URL"):
        register_secret(env[name])
    register_secret(urlparse(env["DESKTOP_URL"]).hostname)
    return env


def _error_result(prueba: dict[str, Any], reason: str) -> dict[str, Any]:
    return {"id": prueba.get("id"), "estado": "error", "estado_esperado": prueba.get("estado_esperado"),
            "checks": {reason: False}, "pass": False, "segundos": 0.0}


async def _run(args: argparse.Namespace) -> int:
    env = _env(args.agent_url)
    missing = [name for name in ("DESKTOP_URL", "DESKTOP_TOKEN", "DESKTOP_PASSWORD") if not env[name]]
    if missing:
        log.error("faltan variables: %s", ", ".join(missing))
        return 2
    pruebas = load_pruebas(Path(args.dir), args.only)
    if not pruebas:
        log.error("no hay pruebas en %s", args.dir)
        return 2
    results: list[dict[str, Any]] = []
    async with httpx.AsyncClient(timeout=30.0) as client:
        for prueba in pruebas:
            try:
                report = await run_prueba(client, prueba, env)
            except httpx.HTTPError as exc:
                log.error("%s: error HTTP (%s)", prueba.get("id"), type(exc).__name__)
                report = _error_result(prueba, "http")
            results.append(report)
            log.info("%s: %s", prueba.get("id"), "pasa" if report["pass"] else "falla")
    totals = summarize(results)
    log.info("resultados:\n%s", format_table(results))
    log.info(
        "aciertos: %d de %d, falsos éxitos: %d, tiempo medio: %.1f s",
        totals["aciertos"], totals["total"], totals["falsos_exitos"], totals["tiempo_medio"],
    )
    report_path = Path(args.report)
    write_report(report_path, results, totals)
    log.info("reporte escrito en %s", report_path)
    return 0 if totals["aciertos"] == totals["total"] else 1


def _split_ids(values: list[str] | None) -> list[str] | None:
    if values is None:
        return None
    ids = [item.strip().upper() for value in values for item in value.split(",") if item.strip()]
    return ids or None


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    default_report = REPORTS_DIR / f"{datetime.now():%Y-%m-%d_%H%M%S}.json"
    parser = argparse.ArgumentParser(description="Corre las pruebas contra el agente local.")
    parser.add_argument("--only", nargs="*", help="ids de prueba, p. ej. E1,E2 o E1 E2")
    parser.add_argument("--dir", default=DEFAULT_DIR, help="carpeta con los JSON de prueba (por defecto: pruebas)")
    parser.add_argument("--agent-url", default=os.environ.get("AGENT_URL") or DEFAULT_AGENT_URL,
                        help="URL del agente (por defecto: AGENT_URL o http://localhost:8000)")
    parser.add_argument("--report", default=str(default_report),
                        help="ruta del reporte JSON (por defecto: data/reports/<fecha_hora>.json)")
    args = parser.parse_args(argv)
    args.only = _split_ids(args.only)
    args.agent_url = args.agent_url.rstrip("/")
    return args


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    args = parse_args(argv)
    setup_logging("INFO")
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
