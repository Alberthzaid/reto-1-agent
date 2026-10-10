"""Pure unit tests for the RFB codec (no sockets; socket tests live with the mock server)."""

import struct
from collections.abc import Iterator

import numpy as np
import pytest

from agent.infra.vnc.rfb import (
    BufferedReader,
    RFBAuthError,
    RFBClient,
    RFBConnectionError,
    RFBError,
    RFBProtocolError,
    RFBUnsupportedSecurityError,
    bgrx_to_rgb,
    pack_key,
    pack_pointer,
    pack_set_encodings,
    pack_set_pixel_format,
    pack_update_request,
    perform_handshake,
    read_framebuffer_update,
    vnc_auth_response,
)

CHALLENGE = bytes(range(16))


# ---------- DES auth ----------

def test_vnc_auth_response_known_vector() -> None:
    assert vnc_auth_response("secret", CHALLENGE).hex() == "ee22539f33a5983ec12f9c2edbc995dd"


def test_vnc_auth_response_truncates_to_eight_bytes() -> None:
    assert vnc_auth_response("longerthan8chars", CHALLENGE).hex() == "76543820b2c358fe359083c192fa77c9"
    assert vnc_auth_response("longerthan8chars", CHALLENGE) == vnc_auth_response("longerth", CHALLENGE)


def test_vnc_auth_response_differs_by_password_and_uses_latin1() -> None:
    assert vnc_auth_response("secret", CHALLENGE) != vnc_auth_response("secreT", CHALLENGE)
    # DES ignores each key byte's parity bit, so compare against a low-bit difference.
    assert vnc_auth_response("p\xe1ss", CHALLENGE) != vnc_auth_response("pbss", CHALLENGE)
    assert len(vnc_auth_response("p\xe1ss", CHALLENGE)) == 16


# ---------- message packing ----------

def test_pack_pointer() -> None:
    assert pack_pointer(640, 400, 1) == b"\x05\x01\x02\x80\x01\x90"
    assert pack_pointer(10, 20, 4) == struct.pack(">BBHH", 5, 4, 10, 20)


def test_pack_key() -> None:
    assert pack_key(0x61, True) == b"\x04\x01\x00\x00\x00\x00\x00\x61"
    assert pack_key(0xFF0D, False) == b"\x04\x00\x00\x00\x00\x00\xff\x0d"


def test_pack_update_request() -> None:
    assert pack_update_request(False, 0, 0, 1280, 800) == b"\x03\x00\x00\x00\x00\x00\x05\x00\x03\x20"
    assert pack_update_request(True, 1, 2, 3, 4) == struct.pack(">BBHHHH", 3, 1, 1, 2, 3, 4)


def test_pack_set_pixel_format_is_32bpp_shifts_16_8_0() -> None:
    msg = pack_set_pixel_format()
    assert len(msg) == 20
    assert msg[0] == 0
    bpp, depth, big_endian, true_color, rmax, gmax, bmax, rshift, gshift, bshift = struct.unpack(">BBBBHHHBBB", msg[4:17])
    assert (bpp, depth, big_endian, true_color) == (32, 24, 0, 1)
    assert (rmax, gmax, bmax) == (255, 255, 255)
    assert (rshift, gshift, bshift) == (16, 8, 0)


def test_pack_set_encodings_raw_and_copyrect() -> None:
    assert pack_set_encodings() == struct.pack(">BxHii", 2, 2, 0, 1)


def test_bgrx_to_rgb_swaps_channels_and_copies() -> None:
    fb = np.array([[[10, 20, 30, 0], [1, 2, 3, 255]]], dtype=np.uint8)  # B,G,R,X
    rgb = bgrx_to_rgb(fb)
    assert rgb.shape == (1, 2, 3)
    assert rgb.tolist() == [[[30, 20, 10], [3, 2, 1]]]
    rgb[0, 0, 0] = 99
    assert fb[0, 0, 2] == 30  # independent copy


# ---------- buffered reader ----------

class FakeSource:
    """Yields the given chunks (bytes or str) one per call, like ws.recv()."""

    def __init__(self, chunks: list[bytes | str]) -> None:
        self._chunks: Iterator[bytes | str] = iter(chunks)

    async def __call__(self) -> bytes | str:
        try:
            return next(self._chunks)
        except StopIteration:
            raise RFBConnectionError("fin de datos") from None


def split(data: bytes, size: int) -> list[bytes | str]:
    return [data[i:i + size] for i in range(0, len(data), size)]


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [1, 7, 1000])
async def test_reader_reassembles_split_chunks(size: int) -> None:
    payload = bytes(range(50))
    reader = BufferedReader(FakeSource(split(payload, size)))
    assert await reader.read(20) == payload[:20]
    assert await reader.read(30) == payload[20:]
    assert reader.bytes_in == 50


@pytest.mark.asyncio
async def test_reader_serves_multiple_reads_from_one_chunk_and_text_frames() -> None:
    reader = BufferedReader(FakeSource([b"abcdef", "gh"]))
    assert await reader.read(2) == b"ab"
    assert await reader.read(5) == b"cdefg"
    assert await reader.read(1) == b"h"


@pytest.mark.asyncio
async def test_reader_propagates_source_failure() -> None:
    reader = BufferedReader(FakeSource([b"ab"]))
    with pytest.raises(RFBConnectionError):
        await reader.read(3)


# ---------- framebuffer updates ----------

@pytest.mark.asyncio
@pytest.mark.parametrize("size", [1, 5, 4096])
async def test_read_framebuffer_update_raw_and_copyrect(size: int) -> None:
    fb = np.zeros((4, 4, 4), np.uint8)
    pixels = bytes(range(2 * 2 * 4))
    copy = struct.pack(">HHHHi", 3, 3, 1, 1, 1) + struct.pack(">HH", 1, 1)
    # Message body after the type byte: padding + nrects, then the rects.
    body = struct.pack(">xH", 2) + struct.pack(">HHHHi", 1, 1, 2, 2, 0) + pixels + copy
    reader = BufferedReader(FakeSource(split(body, size)))
    await read_framebuffer_update(reader, fb)
    assert fb[1:3, 1:3].tobytes() == pixels
    assert fb[3, 3].tolist() == fb[1, 1].tolist() == [0, 1, 2, 3]
    assert fb[0].sum() == 0


@pytest.mark.asyncio
async def test_read_framebuffer_update_rejects_unknown_encoding() -> None:
    fb = np.zeros((2, 2, 4), np.uint8)
    body = struct.pack(">xH", 1) + struct.pack(">HHHHi", 0, 0, 1, 1, 16)
    reader = BufferedReader(FakeSource([body]))
    with pytest.raises(RFBProtocolError):
        await read_framebuffer_update(reader, fb)


# ---------- handshake (pure, with a scripted reader) ----------

def scripted(*parts: bytes) -> tuple[BufferedReader, list[bytes], "SendLog"]:
    sent: list[bytes] = []
    return BufferedReader(FakeSource([b"".join(parts)])), sent, SendLog(sent)


class SendLog:
    def __init__(self, sent: list[bytes]) -> None:
        self._sent = sent

    async def __call__(self, data: bytes) -> None:
        self._sent.append(data)


SERVER_INIT = struct.pack(">HH", 1280, 800) + bytes(16) + struct.pack(">I", 4) + b"desk"


@pytest.mark.asyncio
async def test_handshake_38_vnc_auth_success() -> None:
    reader, sent, send = scripted(
        b"RFB 003.008\n", b"\x01\x02", CHALLENGE, struct.pack(">I", 0), SERVER_INIT
    )
    # The version line is consumed by the handshake itself.
    init = await perform_handshake(reader, send, "secret")
    assert (init.width, init.height, init.name) == (1280, 800, "desk")
    assert sent == [
        b"RFB 003.008\n",
        b"\x02",
        vnc_auth_response("secret", CHALLENGE),
        b"\x01",
    ]


@pytest.mark.asyncio
async def test_handshake_33_fallback_uses_server_imposed_type() -> None:
    reader, sent, send = scripted(
        b"RFB 003.003\n", struct.pack(">I", 2), CHALLENGE, struct.pack(">I", 0), SERVER_INIT
    )
    init = await perform_handshake(reader, send, "secret")
    assert init.width == 1280
    assert sent[0] == b"RFB 003.003\n"
    assert sent[1] == vnc_auth_response("secret", CHALLENGE)
    assert sent[-1] == b"\x01"


@pytest.mark.asyncio
async def test_handshake_auth_failure_hides_password() -> None:
    reader, _, send = scripted(b"RFB 003.008\n", b"\x01\x02", CHALLENGE, struct.pack(">I", 1))
    with pytest.raises(RFBAuthError) as exc:
        await perform_handshake(reader, send, "hunter2-secret")
    text = str(exc.value)
    assert "hunter2" not in text
    assert vnc_auth_response("hunter2-secret", CHALLENGE).hex() not in text


@pytest.mark.asyncio
async def test_handshake_unsupported_security_type() -> None:
    reader, _, send = scripted(b"RFB 003.008\n", b"\x01\x10")
    with pytest.raises(RFBUnsupportedSecurityError):
        await perform_handshake(reader, send, "secret")


@pytest.mark.asyncio
async def test_handshake_server_refusal_reason() -> None:
    reader, _, send = scripted(b"RFB 003.008\n", b"\x00", struct.pack(">I", 4), b"nope")
    with pytest.raises(RFBConnectionError, match="nope"):
        await perform_handshake(reader, send, "secret")


def test_error_hierarchy() -> None:
    assert issubclass(RFBConnectionError, RFBError)
    assert issubclass(RFBAuthError, RFBError)
    assert issubclass(RFBProtocolError, RFBError)
    assert issubclass(RFBUnsupportedSecurityError, RFBProtocolError)


# ---------- client-level guards that need no socket ----------

@pytest.mark.asyncio
async def test_client_pointer_before_connect_raises() -> None:
    client = RFBClient("ws://127.0.0.1:1/x", "secret")
    with pytest.raises(RFBError):
        await client.pointer(1, 1, 0)


def test_client_repr_and_error_do_not_leak_password() -> None:
    client = RFBClient("ws://127.0.0.1:1/x", "hunter2-secret")
    assert "hunter2" not in repr(client)


# ---------- integration against the mock server (real websocket, loopback) ----------

import asyncio  # noqa: E402
import socket  # noqa: E402

from dev_tools.mock_vnc_server import MockVncServer  # noqa: E402

PASSWORD = "hunter2-secret"


async def wait_until(predicate, timeout: float = 5.0) -> None:  # type: ignore[no-untyped-def]
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.02)


async def first_frame(client: RFBClient) -> None:
    await wait_until(lambda: client.updates >= 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("version", ["3.8", "3.3"])
async def test_connect_handshake_and_capture(version: str) -> None:
    async with MockVncServer(PASSWORD, version=version) as server:
        client = RFBClient(server.url, PASSWORD)
        await client.connect()
        try:
            await first_frame(client)
            assert (client.width, client.height, client.name) == (1280, 800, "mock-desktop")
            fb = await client.snapshot()
            assert fb.shape == (800, 1280, 4)
            assert fb[0, 0, :3].tolist() == [200, 100, 50]
            assert fb is not client._fb  # a copy
        finally:
            await client.close()


@pytest.mark.asyncio
async def test_wrong_password_raises_auth_error_without_leaking() -> None:
    async with MockVncServer(PASSWORD) as server:
        client = RFBClient(server.url, "wrong-password")
        with pytest.raises(RFBAuthError) as exc:
            await client.connect()
    assert "wrong-password" not in str(exc.value)
    assert PASSWORD not in str(exc.value)


@pytest.mark.asyncio
async def test_connection_refused_raises_connection_error() -> None:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    client = RFBClient(f"ws://127.0.0.1:{port}/websockify", PASSWORD, open_timeout=3)
    with pytest.raises(RFBConnectionError):
        await client.connect()


@pytest.mark.asyncio
async def test_unsupported_security_type_over_socket() -> None:
    async with MockVncServer(PASSWORD, security_types=(16,)) as server:
        client = RFBClient(server.url, PASSWORD)
        with pytest.raises(RFBUnsupportedSecurityError):
            await client.connect()


@pytest.mark.asyncio
@pytest.mark.parametrize("chunk_size", [1, 7])
async def test_messages_split_across_websocket_frames(chunk_size: int) -> None:
    async with MockVncServer(PASSWORD, width=16, height=8, chunk_size=chunk_size) as server:
        client = RFBClient(server.url, PASSWORD)
        await client.connect()
        try:
            await first_frame(client)
            fb = await client.snapshot()
            assert fb.shape == (8, 16, 4)
            assert (fb[..., :3] == [200, 100, 50]).all()
            await server.paint(2, 2, 4, 4, (1, 2, 3))
            await wait_until(lambda: client.updates >= 2)
            fb = await client.snapshot()
            assert fb[2:6, 2:6, :3].tolist() == [[[1, 2, 3]] * 4] * 4
        finally:
            await client.close()


@pytest.mark.asyncio
async def test_delayed_repaint_advances_last_change() -> None:
    async with MockVncServer(PASSWORD, repaint_delay=0.3) as server:
        client = RFBClient(server.url, PASSWORD)
        await client.connect()
        try:
            await first_frame(client)
            before = await client.snapshot()
            stamp = client.last_change
            await client.pointer(400, 300, 1)
            await client.pointer(400, 300, 0)
            assert (await client.snapshot()).tobytes() == before.tobytes()
            await wait_until(lambda: client.last_change > stamp)
            after = await client.snapshot()
            assert after.tobytes() != before.tobytes()
            assert after[300, 400, :3].tolist() == [255, 255, 255]
        finally:
            await client.close()


@pytest.mark.asyncio
async def test_copyrect_over_socket() -> None:
    async with MockVncServer(PASSWORD, width=32, height=32) as server:
        client = RFBClient(server.url, PASSWORD)
        await client.connect()
        try:
            await first_frame(client)
            await server.paint(0, 0, 4, 4, (9, 8, 7))
            await wait_until(lambda: client.updates >= 2)
            await server.push_copyrect(0, 0, 10, 10, 4, 4)
            await wait_until(lambda: client.updates >= 3)
            fb = await client.snapshot()
            assert fb[10:14, 10:14, :3].tolist() == [[[9, 8, 7]] * 4] * 4
        finally:
            await client.close()


@pytest.mark.asyncio
async def test_read_loop_error_propagates_to_callers() -> None:
    async with MockVncServer(PASSWORD, width=16, height=8) as server:
        client = RFBClient(server.url, PASSWORD)
        await client.connect()
        try:
            await first_frame(client)
            await server.inject_raw(b"\xee")
            await wait_until(lambda: client._task is not None and client._task.done())
            with pytest.raises(RFBProtocolError):
                await client.snapshot()
        finally:
            await client.close()


@pytest.mark.asyncio
async def test_server_records_pointer_and_key_events() -> None:
    async with MockVncServer(PASSWORD, width=64, height=64) as server:
        client = RFBClient(server.url, PASSWORD)
        await client.connect()
        try:
            await client.pointer(5, 6, 1)
            await client.key(0x61, True)
            await wait_until(lambda: len(server.events) == 2)
            assert [(e.x, e.y, e.mask) for e in server.pointer_events()] == [(5, 6, 1)]
            assert [(e.keysym, e.down) for e in server.key_events()] == [(0x61, True)]
        finally:
            await client.close()


# ---------- remediation: close, subprotocol, auth-failure cleanup, bounds, key draw, chunking ----------

from websockets.protocol import State  # noqa: E402


@pytest.mark.asyncio
async def test_close_cancels_reader_task_and_closes_socket() -> None:
    async with MockVncServer(PASSWORD) as server:
        client = RFBClient(server.url, PASSWORD)
        await client.connect()
        await first_frame(client)
        task, ws = client._task, client._ws
        assert task is not None and not task.done()
        assert ws is not None and ws.state is State.OPEN
        await client.close()
        assert task.cancelled()
        assert ws.state is State.CLOSED


@pytest.mark.asyncio
async def test_close_closes_socket_even_if_reader_died_with_any_exception() -> None:
    async with MockVncServer(PASSWORD) as server:
        client = RFBClient(server.url, PASSWORD)
        await client.connect()
        await first_frame(client)
        ws = client._ws
        assert client._task is not None and ws is not None
        client._task.cancel()
        await asyncio.gather(client._task, return_exceptions=True)

        async def boom() -> None:
            raise ValueError("bad rect")

        client._task = asyncio.ensure_future(boom())
        await asyncio.gather(client._task, return_exceptions=True)
        await client.close()  # must not re-raise the reader's ValueError
        assert ws.state is State.CLOSED


@pytest.mark.asyncio
async def test_close_is_idempotent_and_safe_before_connect() -> None:
    await RFBClient("ws://127.0.0.1:1/x", "secret").close()
    async with MockVncServer(PASSWORD) as server:
        client = RFBClient(server.url, PASSWORD)
        await client.connect()
        await first_frame(client)
        await client.close()
        await client.close()


@pytest.mark.asyncio
async def test_subprotocol_binary_is_negotiated() -> None:
    async with MockVncServer(PASSWORD) as server:
        client = RFBClient(server.url, PASSWORD)
        await client.connect()
        try:
            await wait_until(lambda: len(server.subprotocols) == 1)
            assert server.subprotocols == ["binary"]
            assert client._ws is not None and client._ws.subprotocol == "binary"
        finally:
            await client.close()


@pytest.mark.asyncio
async def test_socket_closed_after_failed_login(monkeypatch: pytest.MonkeyPatch) -> None:
    import agent.infra.vnc.rfb as rfb_module

    opened = []
    real_connect = rfb_module.websockets.connect

    async def spy(*args, **kwargs):  # type: ignore[no-untyped-def]
        ws = await real_connect(*args, **kwargs)
        opened.append(ws)
        return ws

    monkeypatch.setattr(rfb_module.websockets, "connect", spy)
    async with MockVncServer(PASSWORD) as server:
        client = RFBClient(server.url, "wrong-password")
        with pytest.raises(RFBAuthError):
            await client.connect()
    assert len(opened) == 1
    assert opened[0].state is State.CLOSED
    assert client._ws is None


@pytest.mark.asyncio
@pytest.mark.parametrize("x,y", [(-1, 0), (0, -1), (64, 0), (0, 32), (999, 999)])
async def test_client_pointer_out_of_bounds_raises_value_error(x: int, y: int) -> None:
    async with MockVncServer(PASSWORD, width=64, height=32) as server:
        client = RFBClient(server.url, PASSWORD)
        await client.connect()
        try:
            await first_frame(client)
            with pytest.raises(ValueError):
                await client.pointer(x, y, 1)
            await client.pointer(63, 31, 0)  # last valid pixel is accepted
            await wait_until(lambda: len(server.events) == 1)
            assert [(e.x, e.y) for e in server.pointer_events()] == [(63, 31)]
        finally:
            await client.close()


@pytest.mark.asyncio
async def test_key_event_makes_mock_draw() -> None:
    async with MockVncServer(PASSWORD, width=64, height=32) as server:
        client = RFBClient(server.url, PASSWORD)
        await client.connect()
        try:
            await first_frame(client)
            before = await client.snapshot()
            updates = client.updates
            await client.key(0x61, True)
            await wait_until(lambda: client.updates > updates)
            after = await client.snapshot()
            assert after.tobytes() != before.tobytes()
            assert after[10, 10, :3].tolist() == [0, 0, 255]  # key mark, BGR red
            assert before[10, 10, :3].tolist() == [200, 100, 50]
        finally:
            await client.close()


@pytest.mark.asyncio
async def test_mock_frames_never_exceed_chunk_size() -> None:
    async with MockVncServer(PASSWORD, width=16, height=8, chunk_size=64) as server:
        client = RFBClient(server.url, PASSWORD)
        await client.connect()
        try:
            await first_frame(client)
            assert len(server.frame_sizes) > 8  # the 512-byte frame alone needs 8 chunks
            assert max(server.frame_sizes) <= 64
            assert 64 in server.frame_sizes  # chunks are actually full-sized, not 1-byte frames
        finally:
            await client.close()
