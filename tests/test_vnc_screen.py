"""VncScreen behaviour against the mock RFB server (timings kept tolerant)."""

import asyncio
import struct

import pytest
from PIL import Image, ImageChops

from websockets.protocol import State

import agent.infra.vnc.vnc_screen as vnc_screen_module
from agent.infra.vnc.keys import BlockedKeyError, keysym
from agent.infra.vnc.rfb import RFBConnectionError, RFBError, RFBProtocolError
from agent.infra.vnc.vnc_screen import VncScreen
from agent.ports.interfaces import Screen
from dev_tools.mock_vnc_server import MockVncServer

AUTH_FIXTURE = "mock-auth-fixture"
CTRL, KEY_A = 0xFFE3, 0x61


async def open_screen(server: MockVncServer) -> VncScreen:
    screen = VncScreen()
    await screen.connect(server.url, AUTH_FIXTURE)
    for _ in range(250):  # wait for the first full frame
        if screen.last_change_ts() > 0:
            return screen
        await asyncio.sleep(0.02)
    raise AssertionError("first frame never arrived")


async def settle(server: MockVncServer, count: int, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while len(server.events) < count:
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"expected {count} events, got {len(server.events)}")
        await asyncio.sleep(0.02)


def test_vnc_screen_satisfies_screen_protocol() -> None:
    assert isinstance(VncScreen(), Screen)


@pytest.mark.asyncio
async def test_screenshot_is_rgb_1280x800_independent_copy() -> None:
    async with MockVncServer(AUTH_FIXTURE) as server:
        screen = await open_screen(server)
        try:
            img = await screen.screenshot()
            assert isinstance(img, Image.Image)
            assert img.mode == "RGB"
            assert img.size == (1280, 800)
            assert img.getpixel((0, 0)) == (50, 100, 200)  # server BGR (200,100,50) -> RGB
            img.putpixel((0, 0), (1, 1, 1))
            assert (await screen.screenshot()).getpixel((0, 0)) == (50, 100, 200)
        finally:
            await screen.close()


@pytest.mark.asyncio
async def test_left_click_sequence_and_hold_time() -> None:
    async with MockVncServer(AUTH_FIXTURE, repaint_delay=0.05) as server:
        screen = await open_screen(server)
        try:
            await screen.click(640, 400)
            await settle(server, 3)
            move, press, release = server.pointer_events()
            assert [(e.x, e.y, e.mask) for e in (move, press, release)] == [
                (640, 400, 0), (640, 400, 1), (640, 400, 0),
            ]
            assert release.ts - press.ts >= 0.10  # the desktop ignores shorter presses
            assert press.ts - move.ts >= 0.03
        finally:
            await screen.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("button,mask", [("left", 1), ("middle", 2), ("right", 4)])
async def test_click_button_masks(button: str, mask: int) -> None:
    async with MockVncServer(AUTH_FIXTURE, repaint_delay=0.05) as server:
        screen = await open_screen(server)
        try:
            await screen.click(10, 20, button=button)
            await settle(server, 3)
            assert [e.mask for e in server.pointer_events()] == [0, mask, 0]
        finally:
            await screen.close()


@pytest.mark.asyncio
async def test_unknown_button_raises_value_error() -> None:
    async with MockVncServer(AUTH_FIXTURE) as server:
        screen = await open_screen(server)
        try:
            with pytest.raises(ValueError):
                await screen.click(10, 20, button="side")
        finally:
            await screen.close()


@pytest.mark.asyncio
async def test_double_click_has_two_presses_about_80ms_apart() -> None:
    async with MockVncServer(AUTH_FIXTURE, repaint_delay=0.05) as server:
        screen = await open_screen(server)
        try:
            await screen.click(100, 100, double=True)
            await settle(server, 5)
            events = server.pointer_events()
            presses = [e for e in events if e.mask == 1]
            assert len(presses) == 2
            releases = [e for e in events if e.mask == 0 and e.ts > presses[0].ts]
            gap = presses[1].ts - releases[0].ts
            assert gap >= 0.05  # ~80 ms between clicks, tolerant lower bound
        finally:
            await screen.close()


@pytest.mark.asyncio
async def test_delayed_repaint_changes_screen_after_click() -> None:
    async with MockVncServer(AUTH_FIXTURE) as server:  # default 600 ms repaint delay
        screen = await open_screen(server)
        try:
            before = await screen.screenshot()
            stamp = screen.last_change_ts()
            await screen.click(640, 400)
            assert (await screen.screenshot()).tobytes() == before.tobytes()
            await asyncio.sleep(0.9)
            after = await screen.screenshot()
            assert after.tobytes() != before.tobytes()
            assert after.getpixel((640, 400)) == (255, 255, 255)
            assert screen.last_change_ts() > stamp
        finally:
            await screen.close()


@pytest.mark.asyncio
async def test_type_text_sends_keysyms_with_spacing() -> None:
    async with MockVncServer(AUTH_FIXTURE) as server:
        screen = await open_screen(server)
        try:
            await screen.type_text("hé\n")
            await settle(server, 6)
            keys = server.key_events()
            assert [(e.keysym, e.down) for e in keys] == [
                (keysym("h"), True), (keysym("h"), False),
                (keysym("é"), True), (keysym("é"), False),
                (keysym("\n"), True), (keysym("\n"), False),
            ]
            downs = [e.ts for e in keys if e.down]
            assert all(b - a >= 0.012 for a, b in zip(downs, downs[1:]))
        finally:
            await screen.close()


@pytest.mark.asyncio
async def test_key_combo_presses_in_order_and_releases_reversed() -> None:
    async with MockVncServer(AUTH_FIXTURE) as server:
        screen = await open_screen(server)
        try:
            await screen.key("ctrl+a")
            await settle(server, 4)
            assert [(e.keysym, e.down) for e in server.key_events()] == [
                (CTRL, True), (KEY_A, True), (KEY_A, False), (CTRL, False),
            ]
        finally:
            await screen.close()


@pytest.mark.asyncio
async def test_blocked_key_combo_sends_nothing() -> None:
    async with MockVncServer(AUTH_FIXTURE) as server:
        screen = await open_screen(server)
        try:
            with pytest.raises(BlockedKeyError):
                await screen.key("ctrl+w")
            await asyncio.sleep(0.1)
            assert server.key_events() == []
        finally:
            await screen.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("steps,button_mask,count", [(3, 8, 3), (-2, 16, 2)])
async def test_scroll_buttons_and_counts(steps: int, button_mask: int, count: int) -> None:
    async with MockVncServer(AUTH_FIXTURE) as server:
        screen = await open_screen(server)
        try:
            await screen.scroll(300, 200, steps)
            await settle(server, 1 + 2 * count)
            events = server.pointer_events()
            assert (events[0].x, events[0].y, events[0].mask) == (300, 200, 0)
            assert [e.mask for e in events[1:]] == [button_mask, 0] * count
        finally:
            await screen.close()


@pytest.mark.asyncio
async def test_scroll_zero_steps_sends_nothing() -> None:
    async with MockVncServer(AUTH_FIXTURE) as server:
        screen = await open_screen(server)
        try:
            await screen.scroll(300, 200, 0)
            await asyncio.sleep(0.1)
            assert server.pointer_events() == []
        finally:
            await screen.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("x,y", [(-1, 0), (0, -1), (1280, 0), (0, 800), (5000, 5000)])
async def test_out_of_bounds_coordinates_raise_value_error(x: int, y: int) -> None:
    async with MockVncServer(AUTH_FIXTURE) as server:
        screen = await open_screen(server)
        try:
            with pytest.raises(ValueError):
                await screen.click(x, y)
            with pytest.raises(ValueError):
                await screen.scroll(x, y, 1)
            await asyncio.sleep(0.1)
            assert server.pointer_events() == []
        finally:
            await screen.close()


@pytest.mark.asyncio
async def test_screenshot_before_connect_raises() -> None:
    with pytest.raises(RFBError):
        await VncScreen().screenshot()


@pytest.mark.asyncio
async def test_type_text_gap_is_15_to_20_ms(monkeypatch: pytest.MonkeyPatch) -> None:
    async with MockVncServer(AUTH_FIXTURE) as server:
        screen = await open_screen(server)
        try:
            real_sleep = asyncio.sleep
            delays: list[float] = []

            async def recording_sleep(delay: float) -> None:
                delays.append(delay)
                await real_sleep(0)

            class FakeAsyncio:
                sleep = staticmethod(recording_sleep)

            monkeypatch.setattr(vnc_screen_module, "asyncio", FakeAsyncio)
            await screen.type_text("abc")
            assert len(delays) == 6  # one gap after every key-down and every key-up
            assert all(0.015 <= d <= 0.020 for d in delays)
        finally:
            monkeypatch.undo()
            await screen.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("op", ["click", "type_text", "key", "scroll", "screenshot"])
async def test_connection_error_mid_session_closes_client(op: str) -> None:
    async with MockVncServer(AUTH_FIXTURE) as server:
        screen = await open_screen(server)
        client = screen._client
        assert client is not None and client._ws is not None and client._task is not None
        ws, task = client._ws, client._task
        await server.disconnect_clients()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 5)  # reader died
        calls = {
            "click": lambda: screen.click(10, 10),
            "type_text": lambda: screen.type_text("a"),
            "key": lambda: screen.key("ctrl+a"),
            "scroll": lambda: screen.scroll(10, 10, 1),
            "screenshot": lambda: screen.screenshot(),
        }
        with pytest.raises(RFBConnectionError):
            await calls[op]()
        assert screen._client is None
        assert ws.state is State.CLOSED
        assert task.done()
        with pytest.raises(RFBError):  # later calls report "not connected"
            await screen.screenshot()


@pytest.mark.asyncio
async def test_error_halfway_through_typing_closes_client(monkeypatch: pytest.MonkeyPatch) -> None:
    async with MockVncServer(AUTH_FIXTURE) as server:
        screen = await open_screen(server)
        client = screen._client
        assert client is not None and client._ws is not None
        ws = client._ws
        real_key = client.key
        sent = 0

        async def flaky_key(sym: int, down: bool) -> None:
            nonlocal sent
            sent += 1
            if sent == 3:
                raise RFBConnectionError("lost")
            await real_key(sym, down)

        monkeypatch.setattr(client, "key", flaky_key)
        with pytest.raises(RFBConnectionError):
            await screen.type_text("abc")
        assert screen._client is None
        assert ws.state is State.CLOSED


@pytest.mark.asyncio
async def test_value_error_does_not_close_client() -> None:
    async with MockVncServer(AUTH_FIXTURE) as server:
        screen = await open_screen(server)
        try:
            with pytest.raises(ValueError):
                await screen.click(-1, -1)
            with pytest.raises(BlockedKeyError):
                await screen.key("ctrl+w")
            assert screen._client is not None
            assert (await screen.screenshot()).size == (1280, 800)
        finally:
            await screen.close()


@pytest.mark.asyncio
async def test_reader_dying_with_non_rfb_error_surfaces_protocol_error_and_closes() -> None:
    async with MockVncServer(AUTH_FIXTURE, width=64, height=48) as server:
        screen = await open_screen(server)
        client = screen._client
        assert client is not None and client._ws is not None and client._task is not None
        ws, task = client._ws, client._task
        # a Raw rect wider than the screen cannot be broadcast into the framebuffer: numpy raises ValueError
        bad = struct.pack(">BxH", 0, 1) + struct.pack(">HHHHi", 0, 0, 100, 1, 0) + bytes(400)
        await server.inject_raw(bad)
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 5)
        assert isinstance(task.exception(), ValueError)
        with pytest.raises(RFBProtocolError) as info:
            await screen.screenshot()
        assert isinstance(info.value.__cause__, ValueError)
        assert AUTH_FIXTURE not in str(info.value)
        assert screen._client is None
        assert ws.state is State.CLOSED


@pytest.mark.asyncio
async def test_key_through_screen_draws_on_mock_framebuffer() -> None:
    async with MockVncServer(AUTH_FIXTURE, width=64, height=48) as server:
        screen = await open_screen(server)
        try:
            before = await screen.screenshot()
            first = screen.last_change_ts()
            await screen.key("a")
            for _ in range(250):
                if screen.last_change_ts() > first:
                    break
                await asyncio.sleep(0.02)
            else:
                raise AssertionError("no repaint after key")
            after = await screen.screenshot()
            assert ImageChops.difference(before, after).getbbox() is not None
            assert ImageChops.difference(after, await screen.screenshot()).getbbox() is None
        finally:
            await screen.close()
