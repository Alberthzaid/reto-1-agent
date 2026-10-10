"""RFB (VNC) client over a websocket: pure codec functions plus an async client.

Secrets: the password and anything derived from it (DES key/response) are never
logged and never included in exception messages.
"""

import asyncio
import logging
import struct
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import numpy as np
import websockets
from Crypto.Cipher import DES

log = logging.getLogger(__name__)

SUBPROTOCOL = "binary"
SEC_NONE = 1
SEC_VNC_AUTH = 2
ENC_RAW = 0
ENC_COPYRECT = 1

MSG_FRAMEBUFFER_UPDATE = 0
MSG_SET_COLOUR_MAP = 1
MSG_BELL = 2
MSG_SERVER_CUT_TEXT = 3

ByteSource = Callable[[], Awaitable[bytes | str]]
Sender = Callable[[bytes], Awaitable[None]]


# ---------- errors ----------

class RFBError(Exception):
    """Base class for RFB client errors."""


class RFBConnectionError(RFBError):
    """The desktop could not be reached, or the connection was lost/refused."""


class RFBAuthError(RFBError):
    """VNC authentication failed."""


class RFBProtocolError(RFBError):
    """The server sent something this client does not support or understand."""


class RFBUnsupportedSecurityError(RFBProtocolError):
    """The server offers no security type this client supports."""


# ---------- pure codec ----------

def _reverse_bits(b: int) -> int:
    return int(f"{b:08b}"[::-1], 2)


def vnc_auth_response(password: str, challenge: bytes) -> bytes:
    """DES-ECB the 16-byte challenge with the bit-reversed, 8-byte Latin-1 password."""
    raw = password.encode("latin-1", errors="replace")[:8].ljust(8, b"\0")
    key = bytes(_reverse_bits(c) for c in raw)
    return DES.new(key, DES.MODE_ECB).encrypt(challenge)


def pack_pointer(x: int, y: int, mask: int) -> bytes:
    return struct.pack(">BBHH", 5, mask, x, y)


def pack_key(keysym: int, down: bool) -> bytes:
    return struct.pack(">BBxxI", 4, int(down), keysym)


def pack_update_request(incremental: bool, x: int, y: int, w: int, h: int) -> bytes:
    return struct.pack(">BBHHHH", 3, int(incremental), x, y, w, h)


def pack_set_pixel_format() -> bytes:
    """32 bpp, depth 24, little-endian true colour, shifts R16 G8 B0 (BGRX in memory)."""
    return struct.pack(">B3xBBBBHHHBBB3x", 0, 32, 24, 0, 1, 255, 255, 255, 16, 8, 0)


def pack_set_encodings() -> bytes:
    """Raw + CopyRect."""
    return struct.pack(">BxHii", 2, 2, ENC_RAW, ENC_COPYRECT)


def bgrx_to_rgb(framebuffer: np.ndarray) -> np.ndarray:
    """Convert an (h, w, 4) BGRX array to an independent (h, w, 3) RGB array."""
    return framebuffer[..., [2, 1, 0]].copy()


# ---------- transport ----------

class BufferedReader:
    """Exact-length reads on top of a source of arbitrary-sized websocket frames."""

    def __init__(self, source: ByteSource) -> None:
        self._source = source
        self._buf = bytearray()
        self.bytes_in = 0

    async def read(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = await self._source()
            if isinstance(chunk, str):
                chunk = chunk.encode()
            self.bytes_in += len(chunk)
            self._buf += chunk
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out


async def read_framebuffer_update(reader: BufferedReader, fb: np.ndarray) -> None:
    """Apply one FramebufferUpdate (after its type byte) to `fb` in place."""
    _, nrects = struct.unpack(">BH", await reader.read(3))
    for _ in range(nrects):
        x, y, w, h, enc = struct.unpack(">HHHHi", await reader.read(12))
        if enc == ENC_RAW:
            data = await reader.read(w * h * 4)
            fb[y:y + h, x:x + w] = np.frombuffer(data, np.uint8).reshape(h, w, 4)
        elif enc == ENC_COPYRECT:
            sx, sy = struct.unpack(">HH", await reader.read(4))
            fb[y:y + h, x:x + w] = fb[sy:sy + h, sx:sx + w].copy()
        else:
            raise RFBProtocolError(f"codificación inesperada: {enc}")


@dataclass(frozen=True, slots=True)
class ServerInit:
    width: int
    height: int
    name: str


async def perform_handshake(reader: BufferedReader, send: Sender, password: str) -> ServerInit:
    """RFB 3.8 handshake with 3.3 fallback, VNC auth and shared ClientInit."""
    version = await reader.read(12)
    try:
        minor = int(version[8:11])
    except ValueError:
        raise RFBProtocolError(f"versión RFB inválida: {version!r}") from None
    await send(b"RFB 003.008\n" if minor >= 8 else b"RFB 003.003\n")
    log.debug("RFB versión del servidor: %r", version)

    if minor >= 7:
        count = (await reader.read(1))[0]
        if count == 0:
            raise RFBConnectionError(f"rechazado por el servidor: {await _read_reason(reader)}")
        types = list(await reader.read(count))
        chosen = SEC_VNC_AUTH if SEC_VNC_AUTH in types else SEC_NONE if SEC_NONE in types else None
        if chosen is None:
            raise RFBUnsupportedSecurityError(f"ningún tipo de seguridad soportado en {types}")
        await send(bytes([chosen]))
    else:
        chosen = struct.unpack(">I", await reader.read(4))[0]
        if chosen == 0:
            raise RFBConnectionError(f"rechazado por el servidor: {await _read_reason(reader)}")
        if chosen not in (SEC_NONE, SEC_VNC_AUTH):
            raise RFBUnsupportedSecurityError(f"tipo de seguridad no soportado: {chosen}")

    if chosen == SEC_VNC_AUTH:
        await send(vnc_auth_response(password, await reader.read(16)))
    if chosen == SEC_VNC_AUTH or minor >= 8:
        if struct.unpack(">I", await reader.read(4))[0] != 0:
            raise RFBAuthError("autenticación fallida: revisa la contraseña")
        log.debug("RFB autenticación OK")

    await send(b"\x01")  # ClientInit: shared session
    width, height = struct.unpack(">HH", await reader.read(4))
    await reader.read(16)  # server pixel format, replaced by SetPixelFormat
    name_len = struct.unpack(">I", await reader.read(4))[0]
    name = (await reader.read(name_len)).decode(errors="replace")
    return ServerInit(width, height, name)


async def _read_reason(reader: BufferedReader) -> str:
    length = struct.unpack(">I", await reader.read(4))[0]
    return (await reader.read(length)).decode(errors="replace")


# ---------- client ----------

class RFBClient:
    """Async RFB client over a websocket (subprotocol "binary")."""

    def __init__(self, url: str, password: str, open_timeout: float = 15.0) -> None:
        self._url = url
        self.__password = password
        self._open_timeout = open_timeout
        self._ws: websockets.ClientConnection | None = None
        self._reader: BufferedReader | None = None
        self._task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._fb: np.ndarray | None = None
        self.width = 0
        self.height = 0
        self.name = ""
        self.updates = 0
        self.last_change = 0.0

    def __repr__(self) -> str:
        return f"RFBClient(size={self.width}x{self.height}, updates={self.updates})"

    @property
    def bytes_in(self) -> int:
        return self._reader.bytes_in if self._reader else 0

    async def connect(self) -> None:
        try:
            self._ws = await websockets.connect(
                self._url, subprotocols=[SUBPROTOCOL], max_size=None, open_timeout=self._open_timeout
            )
        except (OSError, TimeoutError, websockets.WebSocketException) as exc:
            raise RFBConnectionError(f"no se pudo conectar al escritorio ({type(exc).__name__})") from None
        ws = self._ws
        self._reader = BufferedReader(self._recv)
        try:
            init = await perform_handshake(self._reader, self._send, self.__password)
            self.width, self.height, self.name = init.width, init.height, init.name
            self._fb = np.zeros((self.height, self.width, 4), np.uint8)
            await self._send(pack_set_pixel_format())
            await self._send(pack_set_encodings())
        except BaseException:
            await ws.close()
            self._ws = None
            raise
        log.info("RFB conectado: %dx%d (%s)", self.width, self.height, self.name)
        self._task = asyncio.create_task(self._read_loop())
        await self.request_update(incremental=False)

    async def _recv(self) -> bytes | str:
        assert self._ws is not None
        try:
            return await self._ws.recv()
        except websockets.ConnectionClosed:
            raise RFBConnectionError("conexión cerrada por el servidor") from None

    async def _send(self, data: bytes) -> None:
        assert self._ws is not None
        try:
            await self._ws.send(data)
        except websockets.ConnectionClosed:
            raise RFBConnectionError("conexión cerrada por el servidor") from None

    def _ensure_alive(self) -> None:
        """Raise the read loop's error (if it died) or if never connected."""
        if self._ws is None or self._task is None:
            raise RFBError("el cliente RFB no está conectado")
        if self._task.done() and not self._task.cancelled():
            exc = self._task.exception()
            if exc is None:
                raise RFBConnectionError("el lector RFB terminó")
            if isinstance(exc, RFBError):
                raise exc
            raise RFBProtocolError(f"el lector RFB falló ({type(exc).__name__})") from exc

    async def request_update(self, incremental: bool) -> None:
        await self._send(pack_update_request(incremental, 0, 0, self.width, self.height))

    async def _read_loop(self) -> None:
        assert self._reader is not None and self._fb is not None
        reader = self._reader
        while True:
            msg = (await reader.read(1))[0]
            if msg == MSG_FRAMEBUFFER_UPDATE:
                async with self._lock:
                    await read_framebuffer_update(reader, self._fb)
                    self.updates += 1
                    self.last_change = time.monotonic()
                await self.request_update(incremental=True)
            elif msg == MSG_SET_COLOUR_MAP:
                _, _, n = struct.unpack(">BHH", await reader.read(5))
                await reader.read(n * 6)
            elif msg == MSG_BELL:
                pass
            elif msg == MSG_SERVER_CUT_TEXT:
                await reader.read(3)
                await reader.read(struct.unpack(">I", await reader.read(4))[0])
            else:
                raise RFBProtocolError(f"mensaje del servidor desconocido: {msg}")

    async def snapshot(self) -> np.ndarray:
        """Return a copy of the BGRX framebuffer, never a half-applied update."""
        self._ensure_alive()
        assert self._fb is not None
        async with self._lock:
            return self._fb.copy()

    async def pointer(self, x: int, y: int, mask: int) -> None:
        self._ensure_alive()
        if not (0 <= x < self.width and 0 <= y < self.height):
            raise ValueError(f"coordenadas fuera de pantalla: ({x}, {y})")
        await self._send(pack_pointer(x, y, mask))

    async def key(self, keysym: int, down: bool) -> None:
        self._ensure_alive()
        await self._send(pack_key(keysym, down))

    async def close(self) -> None:
        """Cancel the reader and close the socket; idempotent, never raises the reader's error."""
        try:
            if self._task is not None:
                self._task.cancel()
                try:
                    await self._task
                except asyncio.CancelledError:
                    pass
                except Exception as exc:  # the reader may have died with any error
                    log.debug("lector RFB terminó con %s", type(exc).__name__)
        finally:
            if self._ws is not None:
                await self._ws.close()
