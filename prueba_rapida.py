"""Prueba rápida de conexión al escritorio remoto por VNC (RFB sobre websocket).

Muestra cada paso del protocolo, guarda capturas y mide el tiempo de repintado.

Uso (PowerShell):
    $env:DESKTOP_URL = "wss://<tu escritorio>/websockify"
    $env:DESKTOP_PASSWORD = "<contraseña>"
    python probe_vnc.py
    python probe_vnc.py --click 640 400
    python probe_vnc.py --click 640 400 --type "Papelería Los Andes"

Solo lee la pantalla salvo que pases --click o --type. Si haces cambios en la app,
deja todo limpio después con POST /__reset.
"""

import argparse
import asyncio
import os
import struct
import sys
import time
from pathlib import Path

import numpy as np
import websockets
from Crypto.Cipher import DES
from dotenv import load_dotenv
from PIL import Image

load_dotenv()

KEY_CTRL = 0xFFE3
KEY_RETURN = 0xFF0D
KEY_TAB = 0xFF09


def log(step: str, msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {step:<12} {msg}")


def reverse_bits(b: int) -> int:
    return int(f"{b:08b}"[::-1], 2)


def keysym(ch: str) -> int:
    if ch == "\n":
        return KEY_RETURN
    if ch == "\t":
        return KEY_TAB
    cp = ord(ch)
    if 0x20 <= cp <= 0x7E or 0xA0 <= cp <= 0xFF:   # ASCII y Latin-1 (tildes, ñ)
        return cp
    return 0x01000000 + cp                          # resto de Unicode


class Probe:
    def __init__(self) -> None:
        self.buf = bytearray()
        self.updates = 0
        self.last_change = 0.0
        self.bytes_in = 0

    # ---------- transporte ----------
    async def read(self, n: int) -> bytes:
        while len(self.buf) < n:
            chunk = await self.ws.recv()
            if isinstance(chunk, str):
                chunk = chunk.encode()
            self.bytes_in += len(chunk)
            self.buf += chunk
        out = bytes(self.buf[:n])
        del self.buf[:n]
        return out

    async def send(self, data: bytes) -> None:
        await self.ws.send(data)

    # ---------- handshake ----------
    async def connect(self, url: str, password: str) -> None:
        t0 = time.perf_counter()
        self.ws = await websockets.connect(url, subprotocols=["binary"], max_size=None, open_timeout=15)
        log("websocket", f"abierto en {time.perf_counter() - t0:.2f} s (subprotocolo: {self.ws.subprotocol})")

        version = await self.read(12)
        log("versión", f"servidor: {version!r}")
        minor = int(version[8:11])
        reply = b"RFB 003.008\n" if minor >= 8 else b"RFB 003.003\n"
        await self.send(reply)
        log("versión", f"respondemos: {reply!r}")

        if minor >= 7:
            n = (await self.read(1))[0]
            if n == 0:
                reason_len = struct.unpack(">I", await self.read(4))[0]
                raise ConnectionError(f"rechazado: {(await self.read(reason_len)).decode(errors='replace')}")
            types = list(await self.read(n))
            log("seguridad", f"tipos ofrecidos: {types} (1 = ninguna, 2 = VNC auth)")
            chosen = 2 if 2 in types else 1 if 1 in types else None
            if chosen is None:
                raise ConnectionError(f"ningún tipo de seguridad soportado en {types}")
            await self.send(bytes([chosen]))
        else:
            chosen = struct.unpack(">I", await self.read(4))[0]
            log("seguridad", f"tipo impuesto por el servidor: {chosen}")

        if chosen == 2:
            challenge = await self.read(16)
            key = bytes(reverse_bits(c) for c in password.encode("latin-1")[:8].ljust(8, b"\0"))
            await self.send(DES.new(key, DES.MODE_ECB).encrypt(challenge))
            log("auth", "reto de 16 bytes recibido y respondido con DES")

        if chosen == 2 or minor >= 8:
            result = struct.unpack(">I", await self.read(4))[0]
            if result != 0:
                raise ConnectionError("autenticación fallida: revisa la contraseña")
            log("auth", "OK")

        await self.send(b"\x01")  # ClientInit: sesión compartida
        self.w, self.h = struct.unpack(">HH", await self.read(4))
        pf = await self.read(16)
        name_len = struct.unpack(">I", await self.read(4))[0]
        name = (await self.read(name_len)).decode(errors="replace")
        log("servidor", f"pantalla {self.w}x{self.h}, {pf[0]} bpp, nombre: {name!r}")

        self.fb = np.zeros((self.h, self.w, 4), np.uint8)
        await self.send(struct.pack(">B3xBBBBHHHBBB3x", 0, 32, 24, 0, 1, 255, 255, 255, 16, 8, 0))
        await self.send(struct.pack(">BxHii", 2, 2, 0, 1))  # Raw + CopyRect
        log("formato", "pedimos 32 bpp color real y codificaciones Raw + CopyRect")

        self.reader = asyncio.create_task(self.read_loop())
        await self.request_update(incremental=False)

    # ---------- pantalla ----------
    async def request_update(self, incremental: bool) -> None:
        await self.send(struct.pack(">BBHHHH", 3, int(incremental), 0, 0, self.w, self.h))

    async def read_loop(self) -> None:
        while True:
            msg = (await self.read(1))[0]
            if msg == 0:
                _, nrects = struct.unpack(">BH", await self.read(3))
                for _ in range(nrects):
                    x, y, w, h, enc = struct.unpack(">HHHHi", await self.read(12))
                    if enc == 0:
                        data = await self.read(w * h * 4)
                        self.fb[y:y + h, x:x + w] = np.frombuffer(data, np.uint8).reshape(h, w, 4)
                    elif enc == 1:
                        sx, sy = struct.unpack(">HH", await self.read(4))
                        self.fb[y:y + h, x:x + w] = self.fb[sy:sy + h, sx:sx + w].copy()
                    else:
                        raise RuntimeError(f"codificación inesperada: {enc}")
                self.updates += 1
                self.last_change = time.perf_counter()
                await self.request_update(incremental=True)
            elif msg == 1:
                _, _, n = struct.unpack(">BHH", await self.read(5))
                await self.read(n * 6)
            elif msg == 2:
                pass
            elif msg == 3:
                await self.read(3)
                await self.read(struct.unpack(">I", await self.read(4))[0])
            else:
                raise RuntimeError(f"mensaje del servidor desconocido: {msg}")

    async def wait_stable(self, since: float, quiet: float = 0.4, timeout: float = 5.0) -> tuple[float | None, float]:
        """Espera a que la pantalla cambie después de `since` y luego quede quieta `quiet` segundos.
        Devuelve (segundos hasta el primer cambio o None, segundos hasta quedar estable)."""
        first_change = None
        while True:
            await asyncio.sleep(0.05)
            if self.reader.done():
                self.reader.result()  # re-raise the read loop's error instead of hanging silently
            now = time.perf_counter()
            if first_change is None and self.last_change > since:
                first_change = self.last_change - since
            if first_change is not None and now - self.last_change >= quiet:
                return first_change, now - since
            if now - since >= timeout:
                return first_change, now - since

    def save(self, path: Path) -> None:
        Image.fromarray(self.fb[..., [2, 1, 0]].copy()).save(path)
        log("captura", f"guardada en {path}")

    # ---------- acciones ----------
    async def pointer(self, x: int, y: int, mask: int) -> None:
        await self.send(struct.pack(">BBHH", 5, mask, x, y))

    async def click(self, x: int, y: int) -> None:
        await self.pointer(x, y, 0)
        await asyncio.sleep(0.05)
        await self.pointer(x, y, 1)
        await asyncio.sleep(0.15)  # menos de 100 ms se ignora
        await self.pointer(x, y, 0)

    async def key(self, sym: int, down: bool) -> None:
        await self.send(struct.pack(">BBxxI", 4, int(down), sym))

    async def tap(self, sym: int) -> None:
        await self.key(sym, True)
        await asyncio.sleep(0.02)
        await self.key(sym, False)
        await asyncio.sleep(0.02)

    async def select_all(self) -> None:
        await self.key(KEY_CTRL, True)
        await self.tap(ord("a"))
        await self.key(KEY_CTRL, False)

    async def type_text(self, text: str) -> None:
        for ch in text:
            await self.tap(keysym(ch))

    async def close(self) -> None:
        self.reader.cancel()
        await self.ws.close()


async def main() -> int:
    ap = argparse.ArgumentParser(description="Prueba de conexión VNC al escritorio remoto")
    ap.add_argument("--url", default=os.environ.get("DESKTOP_URL"), help="wss://<escritorio>/websockify")
    ap.add_argument("--password", default=os.environ.get("DESKTOP_PASSWORD"))
    ap.add_argument("--click", nargs=2, type=int, metavar=("X", "Y"), help="hace un clic en X,Y")
    ap.add_argument("--type", dest="text", help="tras el clic: Ctrl+A y escribe este texto")
    ap.add_argument("--out", default="capturas_prueba")
    args = ap.parse_args()

    if not args.url or not args.password:
        print("Falta la URL o la contraseña: usa --url/--password o DESKTOP_URL/DESKTOP_PASSWORD.")
        return 2
    if args.text and not args.click:
        print("--type necesita --click para enfocar primero un campo.")
        return 2

    out = Path(args.out)
    out.mkdir(exist_ok=True)
    p = Probe()
    try:
        await p.connect(args.url, args.password)

        t = time.perf_counter() - 0.001
        first, stable = await p.wait_stable(t, timeout=90)
        if first is None:
            log("pantalla", f"sin cuadro completo tras {stable:.1f} s ({p.bytes_in / 1e6:.1f} MB recibidos)")
        else:
            log("pantalla", f"primer cuadro en {first:.2f} s, estable en {stable:.2f} s")
        p.save(out / "01_inicial.png")

        if args.click:
            x, y = args.click
            t = time.perf_counter()
            await p.click(x, y)
            first, stable = await p.wait_stable(t)
            if first is None:
                log("clic", f"({x},{y}) sin cambios visibles en {stable:.1f} s")
            else:
                log("clic", f"({x},{y}) primer cambio a {first:.2f} s, estable a {stable:.2f} s")
            p.save(out / "02_despues_clic.png")

        if args.text:
            t = time.perf_counter()
            await p.select_all()
            await p.type_text(args.text)
            first, stable = await p.wait_stable(t)
            log("escribir", f"{len(args.text)} caracteres; estable a {stable:.2f} s")
            p.save(out / "03_despues_escribir.png")

        log("resumen", f"{p.updates} actualizaciones, {p.bytes_in / 1e6:.1f} MB recibidos")
        await p.close()
        return 0
    except Exception as e:
        log("ERROR", f"{type(e).__name__}: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))