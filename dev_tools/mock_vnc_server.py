"""Fake RFB server over a websocket, for tests that must not touch the real desktop.

It speaks RFB 3.8 (or 3.3), performs the real VNC DES check, records pointer/key
events and repaints the framebuffer so clients can observe screen changes:

- a square is painted ``repaint_delay`` seconds AFTER each button press (delayed task);
- a small mark is painted immediately for every key press.

``chunk_size`` splits every server message across several websocket frames.
"""

import asyncio
import logging
import os
import struct
import time
from dataclasses import dataclass
from types import TracebackType

import numpy as np
import websockets
from websockets.asyncio.server import Server, ServerConnection

from agent.infra.vnc.rfb import SEC_NONE, SEC_VNC_AUTH, vnc_auth_response

log = logging.getLogger(__name__)

BACKGROUND_BGR = (200, 100, 50)  # RGB (50, 100, 200)
SQUARE_BGR = (255, 255, 255)
SQUARE_SIZE = 60
KEY_MARK_SIZE = 8
KEY_MARK_BGR = (0, 0, 255)  # RGB red
CLICK_BUTTONS = 0b111  # left, middle, right


@dataclass(frozen=True, slots=True)
class PointerEvent:
    ts: float
    x: int
    y: int
    mask: int


@dataclass(frozen=True, slots=True)
class KeyEvent:
    ts: float
    keysym: int
    down: bool


Event = PointerEvent | KeyEvent


class _ClientReader:
    """Exact-length reads over websocket frames (server side)."""

    def __init__(self, ws: ServerConnection) -> None:
        self._ws = ws
        self._buf = bytearray()

    async def read(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = await self._ws.recv()
            self._buf += chunk.encode() if isinstance(chunk, str) else chunk
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out


class _Session:
    def __init__(self, ws: ServerConnection) -> None:
        self.ws = ws
        self.send_lock = asyncio.Lock()
        self.wants_update = False
        self.dirty: list[tuple[int, int, int, int]] = []  # x, y, w, h


class MockVncServer:
    """Async context manager exposing ``url`` and ``events``."""

    def __init__(
        self,
        password: str = "secret",
        width: int = 1280,
        height: int = 800,
        chunk_size: int | None = None,
        version: str = "3.8",
        repaint_delay: float = 0.6,
        security_types: tuple[int, ...] = (SEC_VNC_AUTH,),
    ) -> None:
        if version not in ("3.3", "3.8"):
            raise ValueError("version must be '3.3' or '3.8'")
        self._password = password
        self.width = width
        self.height = height
        self._chunk_size = chunk_size
        self._version = version
        self._repaint_delay = repaint_delay
        self._security_types = security_types
        self.events: list[Event] = []
        self.framebuffer = np.zeros((height, width, 4), np.uint8)
        self.framebuffer[..., :3] = BACKGROUND_BGR
        self._server: Server | None = None
        self._sessions: list[_Session] = []
        self._tasks: set[asyncio.Task[None]] = set()
        self._buttons = 0
        self._key_marks = 0
        self.url = ""

    async def __aenter__(self) -> "MockVncServer":
        self._server = await websockets.serve(
            self._handle, "127.0.0.1", 0, subprotocols=["binary"]  # type: ignore[list-item]
        )
        port = self._server.sockets[0].getsockname()[1]
        self.url = f"ws://127.0.0.1:{port}/websockify"
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    # ---------- test helpers ----------

    def pointer_events(self) -> list[PointerEvent]:
        return [e for e in self.events if isinstance(e, PointerEvent)]

    def key_events(self) -> list[KeyEvent]:
        return [e for e in self.events if isinstance(e, KeyEvent)]

    async def push_copyrect(self, sx: int, sy: int, dx: int, dy: int, w: int, h: int) -> None:
        """Copy a region inside the server framebuffer and tell every client via CopyRect."""
        self.framebuffer[dy:dy + h, dx:dx + w] = self.framebuffer[sy:sy + h, sx:sx + w].copy()
        msg = struct.pack(">BxH", 0, 1) + struct.pack(">HHHHi", dx, dy, w, h, 1) + struct.pack(">HH", sx, sy)
        for session in self._sessions:
            await self._send(session, msg)

    async def inject_raw(self, data: bytes) -> None:
        """Send arbitrary bytes to every client (e.g. an unknown message type)."""
        for session in self._sessions:
            await self._send(session, data)

    async def paint(self, x: int, y: int, w: int, h: int, bgr: tuple[int, int, int]) -> None:
        """Paint a rectangle and push it to waiting clients."""
        x, y = max(0, min(x, self.width - 1)), max(0, min(y, self.height - 1))
        w, h = min(w, self.width - x), min(h, self.height - y)
        self.framebuffer[y:y + h, x:x + w, :3] = bgr
        for session in self._sessions:
            session.dirty.append((x, y, w, h))
            await self._flush(session)

    # ---------- sending ----------

    async def _send(self, session: _Session, data: bytes) -> None:
        async with session.send_lock:
            step = self._chunk_size or len(data) or 1
            for i in range(0, len(data), step):
                await session.ws.send(data[i:i + step])

    async def _flush(self, session: _Session) -> None:
        if not session.wants_update or not session.dirty:
            return
        rects, session.dirty = session.dirty, []
        session.wants_update = False
        parts = [struct.pack(">BxH", 0, len(rects))]
        for x, y, w, h in rects:
            parts.append(struct.pack(">HHHHi", x, y, w, h, 0))
            parts.append(self.framebuffer[y:y + h, x:x + w].tobytes())
        await self._send(session, b"".join(parts))

    # ---------- protocol ----------

    async def _handle(self, ws: ServerConnection) -> None:
        session = _Session(ws)
        reader = _ClientReader(ws)
        try:
            if not await self._handshake(session, reader):
                return
            self._sessions.append(session)
            await self._message_loop(session, reader)
        except websockets.ConnectionClosed:
            pass
        finally:
            if session in self._sessions:
                self._sessions.remove(session)

    async def _handshake(self, session: _Session, reader: _ClientReader) -> bool:
        minor = 8 if self._version == "3.8" else 3
        await self._send(session, f"RFB 003.00{minor}\n".encode())
        await reader.read(12)  # client version
        types = self._security_types
        if minor >= 7:
            await self._send(session, bytes([len(types), *types]))
            chosen = (await reader.read(1))[0]
            if chosen not in types:
                return False
        else:
            chosen = types[0]
            await self._send(session, struct.pack(">I", chosen))
        if chosen == SEC_VNC_AUTH:
            challenge = os.urandom(16)
            await self._send(session, challenge)
            response = await reader.read(16)
            if response != vnc_auth_response(self._password, challenge):
                await self._send(session, struct.pack(">I", 1))
                if minor >= 8:
                    reason = b"authentication failed"
                    await self._send(session, struct.pack(">I", len(reason)) + reason)
                await session.ws.close()
                return False
            await self._send(session, struct.pack(">I", 0))
        elif chosen == SEC_NONE:
            if minor >= 8:
                await self._send(session, struct.pack(">I", 0))
        else:
            return False
        await reader.read(1)  # ClientInit
        pixel_format = struct.pack(">BBBBHHHBBB3x", 32, 24, 0, 1, 255, 255, 255, 16, 8, 0)
        name = b"mock-desktop"
        await self._send(
            session,
            struct.pack(">HH", self.width, self.height) + pixel_format + struct.pack(">I", len(name)) + name,
        )
        return True

    async def _message_loop(self, session: _Session, reader: _ClientReader) -> None:
        while True:
            msg = (await reader.read(1))[0]
            if msg == 0:  # SetPixelFormat
                await reader.read(19)
            elif msg == 2:  # SetEncodings
                _, n = struct.unpack(">BH", await reader.read(3))
                await reader.read(4 * n)
            elif msg == 3:  # FramebufferUpdateRequest
                incremental, x, y, w, h = struct.unpack(">BHHHH", await reader.read(9))
                if not incremental:
                    session.dirty.append((x, y, w, h))
                session.wants_update = True
                await self._flush(session)
            elif msg == 4:  # KeyEvent
                down, keysym = struct.unpack(">BxxI", await reader.read(7))
                self.events.append(KeyEvent(time.monotonic(), keysym, bool(down)))
                if down:
                    await self._mark_key()
            elif msg == 5:  # PointerEvent
                mask, x, y = struct.unpack(">BHH", await reader.read(5))
                self.events.append(PointerEvent(time.monotonic(), x, y, mask))
                pressed = mask & ~self._buttons & CLICK_BUTTONS
                self._buttons = mask
                if pressed:
                    self._schedule_repaint(x, y)
            elif msg == 6:  # ClientCutText
                length = struct.unpack(">3xI", await reader.read(7))[0]
                await reader.read(length)
            else:
                log.warning("mock: mensaje de cliente desconocido: %d", msg)
                return

    async def _mark_key(self) -> None:
        x = 10 + (KEY_MARK_SIZE + 2) * (self._key_marks % 100)
        self._key_marks += 1
        await self.paint(x, 10, KEY_MARK_SIZE, KEY_MARK_SIZE, KEY_MARK_BGR)

    def _schedule_repaint(self, x: int, y: int) -> None:
        async def repaint() -> None:
            await asyncio.sleep(self._repaint_delay)
            await self.paint(
                x - SQUARE_SIZE // 2, y - SQUARE_SIZE // 2, SQUARE_SIZE, SQUARE_SIZE, SQUARE_BGR
            )

        task = asyncio.create_task(repaint())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

