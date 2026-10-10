"""Wait for framebuffer activity to become visually stable."""

import asyncio
import time

from PIL import Image

from agent.ports.interfaces import Screen


class Settler:
    """Returns a settled screenshot and whether the framebuffer changed."""

    def __init__(self, screen: Screen) -> None:
        self._screen = screen

    async def settle(
        self,
        since: float,
        quiet: float = 0.3,
        min_wait: float = 0.25,
        max_wait: float = 2.0,
    ) -> tuple[Image.Image, bool]:
        """Wait for a post-action change followed by a quiet period, bounded by `max_wait`."""
        started = time.monotonic()
        deadline = started + max_wait
        min_deadline = started + min_wait
        saw_change = self._screen.last_change_ts() > since
        last_change = self._screen.last_change_ts() if saw_change else since

        while True:
            now = time.monotonic()
            observed_change = self._screen.last_change_ts()
            if observed_change > since:
                saw_change = True
                last_change = observed_change
            if now >= deadline:
                return await self._screen.screenshot(), saw_change
            if saw_change and now >= min_deadline and now - last_change >= quiet:
                return await self._screen.screenshot(), True
            next_deadline = deadline
            if saw_change:
                next_deadline = min(next_deadline, max(min_deadline, last_change + quiet))
            await asyncio.sleep(max(0.001, min(0.05, next_deadline - now)))