"""Run the local agent against the challenge `pruebas/` and check results with /__check.

Usage: uv run python -m dev_tools.run_tests [--only E1 E2]

Environment: DESKTOP_URL, DESKTOP_TOKEN, DESKTOP_PASSWORD, AGENT_URL (default http://127.0.0.1:8000),
TEAM_TOKEN. Secrets are registered for redaction and never logged.
"""

import argparse
import asyncio
import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from dotenv import load_dotenv

from agent.observability.log_setup import register_secret, setup_logging

log = logging.getLogger("run_tests")

ROOT = Path(__file__).resolve().parent.parent
PRUEBAS_DIR = ROOT / "pruebas"
POLL_S = 5.0
TASK_TIMEOUT_S = 300.0
FINAL_STATES = {"terminada", "fallida", "cancelada"}
_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")


def desktop_base(url: str) -> str:
    """https://<host> for a wss://<host>/websockify URL (http for ws)."""
    parsed = urlparse(url)
    schemes = {"wss": "https", "ws": "http"}
    if parsed.scheme not in schemes or not parsed.netloc:
        raise ValueError("DESKTOP_URL debe ser ws:// o wss://")
    return f"{schemes[parsed.scheme]}://{parsed.netloc}"


def load_pruebas(directory: Path, only: list[str] | None) -> list[dict[str, Any]]:
    pruebas = [json.loads(path.read_text(encoding="utf-8")) for path in sorted(directory.glob("E*.json"))]
    if only:
        wanted = {item.upper() for item in only}
        pruebas = [p for p in pruebas if str(p.get("id", "")).upper() in wanted]
    return pruebas


def _answer_line(resumen: str) -> str | None:
    for line in resumen.splitlines():
        if line.strip().lower().startswith("respuesta:"):
            return line.split(":", 1)[1].strip()
    return None


def _same(reported: str, expected: str) -> bool:
    reported_number = _NUMBER.search(reported)
    expected_number = _NUMBER.search(expected)
    if reported_number and expected_number:
        return float(reported_number.group()) == float(expected_number.group())
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


def evaluate(prueba: dict[str, Any], estado: str, resumen: str, check: dict[str, Any]) -> dict[str, Any]:
    report: dict[str, Any] = {
        "id": prueba.get("id"),
        "check_pass": bool(check.get("pass")),
        "checks": check.get("checks", []),
        "estado": estado,
        "estado_esperado": prueba.get("estado_esperado"),
        "estado_ok": estado == prueba.get("estado_esperado"),
    }
    ok = report["check_pass"] and report["estado_ok"]
    if "respuesta_esperada" in prueba:
        report["respuesta_ok"] = respuesta_matches(resumen, str(prueba["respuesta_esperada"]))
        ok = ok and report["respuesta_ok"]
    if prueba.get("claves_razon"):
        report["claves_faltantes"] = missing_keys(resumen, list(prueba["claves_razon"]))
        ok = ok and not report["claves_faltantes"]
    report["ok"] = ok
    return report


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
    report = evaluate(prueba, estado, resumen, check.json())
    report["segundos"] = round(elapsed, 1)
    report["resumen"] = resumen
    return report


def _env() -> dict[str, str]:
    env = {
        "DESKTOP_URL": os.environ.get("DESKTOP_URL", ""),
        "DESKTOP_TOKEN": os.environ.get("DESKTOP_TOKEN", ""),
        "DESKTOP_PASSWORD": os.environ.get("DESKTOP_PASSWORD", ""),
        "AGENT_URL": (os.environ.get("AGENT_URL") or "http://127.0.0.1:8000").rstrip("/"),
        "TEAM_TOKEN": os.environ.get("TEAM_TOKEN", ""),
    }
    for name in ("DESKTOP_TOKEN", "DESKTOP_PASSWORD", "TEAM_TOKEN", "DESKTOP_URL"):
        register_secret(env[name])
    register_secret(urlparse(env["DESKTOP_URL"]).hostname)
    return env


async def _run(only: list[str] | None) -> int:
    env = _env()
    missing = [name for name in ("DESKTOP_URL", "DESKTOP_TOKEN", "DESKTOP_PASSWORD") if not env[name]]
    if missing:
        log.error("faltan variables: %s", ", ".join(missing))
        return 2
    pruebas = load_pruebas(PRUEBAS_DIR, only)
    failures = 0
    async with httpx.AsyncClient(timeout=30.0) as client:
        for prueba in pruebas:
            try:
                report = await run_prueba(client, prueba, env)
            except httpx.HTTPError as exc:
                log.error("%s: error HTTP (%s)", prueba.get("id"), type(exc).__name__)
                failures += 1
                continue
            failures += 0 if report["ok"] else 1
            log.info("%s: %s", prueba["id"], json.dumps(report, ensure_ascii=False))
    log.info("pruebas: %d, fallidas: %d", len(pruebas), failures)
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Corre las pruebas E* contra el agente local.")
    parser.add_argument("--only", nargs="*", help="ids de prueba, p. ej. E1 E2")
    args = parser.parse_args(argv)
    load_dotenv()
    setup_logging("INFO")
    return asyncio.run(_run(args.only))


if __name__ == "__main__":
    sys.exit(main())
