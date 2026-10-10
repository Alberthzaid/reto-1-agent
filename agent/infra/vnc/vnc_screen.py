"""`Screen` adapter on top of `RFBClient`: screenshots, mouse and keyboard input."""

import asyncio
import logging

from PIL import Image

from agent.infra.vnc.keys import keysym, parse_combo
from agent.infra.vnc.rfb import RFBClient, RFBError, bgrx_to_rgb
from agent.observability.log_setup import register_secret

log = logging.getLogger(__name__)

BUTTON_MASKS: dict[str, int] = {"left": 1, "middle": 2, "right": 4}
SCROLL_UP_MASK = 8  # button 4
SCROLL_DOWN_MASK = 16  # button 5

MOVE_SETTLE_S = 0.05
CLICK_HOLD_S = 0.15  # the desktop ignores presses shorter than ~100 ms
DOUBLE_CLICK_GAP_S = 0.08
KEY_INTERVAL_S = 0.0175  # 15-20 ms between typed events
SCROLL_STEP_S = 0.03


class VncScreen:
    """Implements the `Screen` port over an RFB websocket connection."""

    def __init__(self) -> None:
        self._client: RFBClient | None = None

    async def connect(self, url: str, password: str) -> None:
        register_secret(password)
        client = RFBClient(url, password)
        await client.connect()
        self._client = client

    def _require(self) -> RFBClient:
        if self._client is None:
            raise RFBError("la pantalla VNC no está conectada")
        return self._client

    def _check_bounds(self, client: RFBClient, x: int, y: int) -> None:
        if not (0 <= x < client.width and 0 <= y < client.height):
            raise ValueError(f"coordenadas fuera de pantalla: ({x}, {y})")

    async def screenshot(self) -> Image.Image:
        fb = await self._require().snapshot()
        return Image.fromarray(bgrx_to_rgb(fb), "RGB")

    async def click(self, x: int, y: int, button: str = "left", double: bool = False) -> None:
        client = self._require()
        self._check_bounds(client, x, y)
        mask = BUTTON_MASKS.get(button)
        if mask is None:
            raise ValueError(f"botón desconocido: {button!r}")
        await client.pointer(x, y, 0)
        await asyncio.sleep(MOVE_SETTLE_S)
        for i in range(2 if double else 1):
            if i:
                await asyncio.sleep(DOUBLE_CLICK_GAP_S)
            await client.pointer(x, y, mask)
            await asyncio.sleep(CLICK_HOLD_S)
            await client.pointer(x, y, 0)
        log.debug("clic %s en (%d, %d)%s", button, x, y, " doble" if double else "")

    async def type_text(self, text: str) -> None:
        client = self._require()
        for char in text:
            sym = keysym(char)
            await client.key(sym, True)
            await asyncio.sleep(KEY_INTERVAL_S)
            await client.key(sym, False)
            await asyncio.sleep(KEY_INTERVAL_S)

    async def key(self, combo: str) -> None:
        client = self._require()
        syms = parse_combo(combo)  # raises BlockedKeyError before anything is sent
        for sym in syms:
            await client.key(sym, True)
            await asyncio.sleep(KEY_INTERVAL_S)
        for sym in reversed(syms):
            await client.key(sym, False)
            await asyncio.sleep(KEY_INTERVAL_S)

    async def scroll(self, x: int, y: int, steps: int) -> None:
        client = self._require()
        self._check_bounds(client, x, y)
        if steps == 0:
            return
        mask = SCROLL_UP_MASK if steps > 0 else SCROLL_DOWN_MASK
        await client.pointer(x, y, 0)
        await asyncio.sleep(MOVE_SETTLE_S)
        for _ in range(abs(steps)):
            await client.pointer(x, y, mask)
            await asyncio.sleep(SCROLL_STEP_S)
            await client.pointer(x, y, 0)
            await asyncio.sleep(SCROLL_STEP_S)

    def last_change_ts(self) -> float:
        return self._client.last_change if self._client is not None else 0.0

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None
