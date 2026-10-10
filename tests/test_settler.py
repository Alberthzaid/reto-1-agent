import time

import pytest
from PIL import Image

from agent.domain.settler import Settler


class ScheduledScreen:
    def __init__(self, changes: list[float]) -> None:
        self._started = time.monotonic()
        self._changes = changes

    def last_change_ts(self) -> float:
        elapsed = time.monotonic() - self._started
        occurred = [self._started + offset for offset in self._changes if offset <= elapsed]
        return occurred[-1] if occurred else 0.0

    async def screenshot(self) -> Image.Image:
        return Image.new("RGB", (8, 8), "white")


@pytest.mark.asyncio
async def test_settler_waits_for_delayed_change_and_quiet_period() -> None:
    screen = ScheduledScreen([0.03])
    image, changed = await Settler(screen).settle(0.0, quiet=0.02, min_wait=0.01, max_wait=0.2)
    assert image.size == (8, 8)
    assert changed is True


@pytest.mark.asyncio
async def test_settler_returns_current_capture_when_no_change_arrives() -> None:
    started = time.monotonic()
    _, changed = await Settler(ScheduledScreen([])).settle(0.0, max_wait=0.04)
    assert changed is False
    assert time.monotonic() - started >= 0.04


@pytest.mark.asyncio
async def test_settler_returns_at_max_wait_for_continuous_changes() -> None:
    started = time.monotonic()
    _, changed = await Settler(ScheduledScreen([0.005, 0.02, 0.04, 0.06])).settle(
        0.0, quiet=0.05, max_wait=0.07
    )
    assert changed is True
    assert time.monotonic() - started >= 0.07
