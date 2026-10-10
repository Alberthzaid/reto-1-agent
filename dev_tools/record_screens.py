"""Capture one screenshot of the real desktop into data/screens/<timestamp>.png.

Usage: uv run python -m dev_tools.record_screens

Reads DESKTOP_URL and DESKTOP_PASSWORD from the environment (.env is loaded).
The password is never logged. After a mutating run, reset the target app with
`POST /__reset`.
"""

import asyncio
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

from agent.infra.vnc.rfb import RFBError
from agent.infra.vnc.vnc_screen import VncScreen
from agent.observability.log_setup import setup_logging

log = logging.getLogger("record_screens")

OUTPUT_DIR = Path("data") / "screens"
FIRST_FRAME_TIMEOUT_S = 90.0  # a full 1280x800 Raw frame is slow over the hosted websocket
POLL_INTERVAL_S = 0.25


async def wait_for_first_frame(screen: VncScreen, timeout: float = FIRST_FRAME_TIMEOUT_S) -> bool:
    """Wait until the first framebuffer update has been applied."""
    deadline = time.monotonic() + timeout
    while screen.last_change_ts() <= 0:
        if time.monotonic() > deadline:
            return False
        await asyncio.sleep(POLL_INTERVAL_S)
    return True


async def record(url: str, password: str, out_dir: Path = OUTPUT_DIR) -> Path:
    """Connect, wait for the first full frame and save a PNG. Returns its path."""
    screen = VncScreen()
    await screen.connect(url, password)
    try:
        if not await wait_for_first_frame(screen):
            log.warning("el primer frame no llegó en %.0f s; la captura puede salir negra", FIRST_FRAME_TIMEOUT_S)
        img = await screen.screenshot()
        if img.getbbox() is None:
            log.warning("la captura está completamente negra (el primer frame no se completó)")
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{datetime.now():%Y%m%d_%H%M%S}.png"
        img.save(path)
        log.info("captura guardada en %s (%dx%d)", path, *img.size)
        return path
    finally:
        await screen.close()


def main() -> int:
    load_dotenv()
    setup_logging()
    url = os.environ.get("DESKTOP_URL")
    password = os.environ.get("DESKTOP_PASSWORD")
    if not url or not password:
        missing = [n for n, v in (("DESKTOP_URL", url), ("DESKTOP_PASSWORD", password)) if not v]
        log.error("falta configurar: %s", ", ".join(missing))
        return 2
    try:
        asyncio.run(record(url, password))
    except RFBError as exc:
        log.error("no se pudo capturar: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
