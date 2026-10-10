"""Scripted walk through the app's client screens, saving a screenshot per step.

Usage: uv run python -m dev_tools.explore_ui [--out data/screens/explore]

Used to document UI changes (e.g. incident FR-0153, theme v4.2): the modal on
start, the client list toolbar, the create form, what "Aplicar" does, whether a
second "Aplicar" duplicates, and whether an empty search opens a form by itself.
It mutates the app with throwaway data, so it resets the desktop with
`POST /__reset` before and after. Reads DESKTOP_URL, DESKTOP_PASSWORD and
DESKTOP_TOKEN from the environment; secrets are never logged.
"""

import argparse
import asyncio
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path

import httpx
from dotenv import load_dotenv

from agent.infra.vnc.vnc_screen import VncScreen
from agent.observability.log_setup import register_secret, setup_logging
from dev_tools.record_screens import wait_for_first_frame
from dev_tools.run_tests import desktop_base

log = logging.getLogger("explore_ui")

DEFAULT_OUT = Path("data") / "screens" / "explore"
SETTLE_S = 1.5


@dataclass(frozen=True)
class Step:
    name: str
    clicks: tuple[tuple[int, int], ...] = ()
    text: str | None = None
    wait: float = SETTLE_S


STEPS: tuple[Step, ...] = (
    Step("00_inicio"),
    Step("01_cerrar_modal", clicks=((840, 483),)),
    Step("02_menu_comercial", clicks=((122, 48),)),
    Step("03_menu_maestros", clicks=((122, 77),)),
    Step("04_lista_clientes", clicks=((327, 77),)),
    Step("05_crear_cliente", clicks=((1209, 185),)),
    Step("06_nombre", clicks=((463, 136),), text="Prueba Exploracion"),
    Step("07_nit", clicks=((239, 180),), text="999000111"),
    Step("08_telefono", clicks=((688, 180),), text="3000000000"),
    Step("09_ciudad_abierta", clicks=((239, 225),)),
    Step("10_ciudad_pereira", clicks=((60, 395),)),
    Step("11_aplicar_inmediato", clicks=((864, 376),), wait=0.3),
    Step("12_aplicar_asentado", wait=1.5),
    Step("13_aplicar_otra_vez", clicks=((864, 376),), wait=2.0),
    Step("14_cerrar_formulario", clicks=((793, 376),)),
    Step("15_filtrar_nit_creado", clicks=((260, 131),), text="999000111"),
    Step("16_filtrar_resultado", clicks=((540, 131),), wait=2.0),
    Step("17_filtrar_nit_inexistente", clicks=((260, 131),), text="888777666"),
    Step("18_filtrar_sin_resultados", clicks=((540, 131),), wait=2.0),
    Step("19_espera_tras_sin_resultados", wait=4.0),
)


async def reset(env: dict[str, str]) -> None:
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            f"{desktop_base(env['DESKTOP_URL'])}/__reset",
            headers={"Authorization": f"Bearer {env['DESKTOP_TOKEN']}"},
        )
        response.raise_for_status()
    log.info("escritorio reiniciado")


async def run_step(screen: VncScreen, step: Step, out_dir: Path) -> None:
    for x, y in step.clicks:
        await screen.click(x, y)
    if step.text is not None:
        await screen.key("ctrl+a")
        await screen.type_text(step.text)
    await asyncio.sleep(step.wait)
    image = await screen.screenshot()
    path = out_dir / f"{step.name}.png"
    image.save(path)
    log.info("paso %s guardado en %s", step.name, path)


async def explore(env: dict[str, str], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    await reset(env)
    screen = VncScreen()
    await screen.connect(env["DESKTOP_URL"], env["DESKTOP_PASSWORD"])
    try:
        if not await wait_for_first_frame(screen):
            log.warning("el primer frame no llegó; las capturas pueden salir negras")
        await asyncio.sleep(SETTLE_S)
        for step in STEPS:
            await run_step(screen, step, out_dir)
    finally:
        await screen.close()
        await reset(env)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Recorre la app y guarda una captura por paso.")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="carpeta de salida")
    args = parser.parse_args(argv)
    load_dotenv()
    setup_logging()
    env = {name: os.environ.get(name, "") for name in ("DESKTOP_URL", "DESKTOP_PASSWORD", "DESKTOP_TOKEN")}
    missing = [name for name, value in env.items() if not value]
    if missing:
        log.error("falta configurar: %s", ", ".join(missing))
        return 2
    for value in (*env.values(), desktop_base(env["DESKTOP_URL"]).split("://", 1)[-1]):
        register_secret(value)
    asyncio.run(explore(env, args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
