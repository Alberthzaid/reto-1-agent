"""Reusable in-memory fakes for domain tests (no network, no real desktop)."""

import copy
from collections.abc import Callable
from pathlib import Path
from typing import Any

from PIL import Image

from agent.domain.models import Decision, TextBox, ToolCall

WRITE_ACTIONS = {"click", "type_text", "key", "scroll"}


class FakeScreen:
    """Records every input event; returns `image` on screenshot."""

    def __init__(self, image: Image.Image | None = None, change_ts: float = 1.0) -> None:
        self.image = image or Image.new("RGB", (1280, 800), "white")
        self.actions: list[tuple[Any, ...]] = []
        self.change_ts = change_ts
        self.connected: tuple[str, str] | None = None
        self.close_calls = 0

    async def connect(self, url: str, password: str) -> None:
        self.connected = (url, password)

    async def screenshot(self) -> Image.Image:
        return self.image.copy()

    async def click(self, x: int, y: int, button: str = "left", double: bool = False) -> None:
        self.actions.append(("click", x, y, button, double))

    async def type_text(self, text: str) -> None:
        self.actions.append(("type_text", text))

    async def key(self, combo: str) -> None:
        self.actions.append(("key", combo))

    async def scroll(self, x: int, y: int, steps: int) -> None:
        self.actions.append(("scroll", x, y, steps))

    def last_change_ts(self) -> float:
        return self.change_ts

    async def close(self) -> None:
        self.close_calls += 1

    @property
    def write_actions(self) -> list[tuple[Any, ...]]:
        return [action for action in self.actions if action[0] in WRITE_ACTIONS]


class FakeReader:
    """Full-screen reads return `boxes`; region reads return `region_fn(img, region)`, `region_boxes`
    or the boxes inside the region, in that order of preference."""

    def __init__(
        self,
        boxes: list[TextBox] | None = None,
        region_boxes: list[TextBox] | None = None,
        region_fn: Callable[[Image.Image, tuple[int, int, int, int]], list[TextBox]] | None = None,
    ) -> None:
        self.boxes = boxes or []
        self.region_boxes = region_boxes
        self.region_fn = region_fn
        self.calls: list[tuple[int, int] | tuple[int, int, int, int] | None] = []

    async def read(self, img: Image.Image, region: tuple[int, int, int, int] | None = None) -> list[TextBox]:
        self.calls.append(region)
        if region is None:
            return list(self.boxes)
        if self.region_fn is not None:
            return list(self.region_fn(img, region))
        if self.region_boxes is not None:
            return list(self.region_boxes)
        x, y, w, h = region
        return [b for b in self.boxes if x <= b.center[0] < x + w and y <= b.center[1] < y + h]


class FakeSettler:
    def __init__(self, screen: FakeScreen, changed: bool = True) -> None:
        self.screen = screen
        self.changed = changed
        self.calls: list[float] = []

    async def settle(self, since: float) -> tuple[Image.Image, bool]:
        self.calls.append(since)
        return await self.screen.screenshot(), self.changed


class FakeClock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class FakeModel:
    """Returns scripted decisions; snapshots every request so tests can check append-only history."""

    def __init__(self, script: list[Decision | Exception], clock: FakeClock | None = None, cost: float = 0.0) -> None:
        self.script = list(script)
        self.requests: list[dict[str, Any]] = []
        self.live_messages: list[list[Any]] = []
        self.clock = clock
        self.cost = cost

    async def decide(self, system: str, messages: list[Any], tools: list[Any]) -> Decision:
        self.requests.append({"system": system, "messages": copy.deepcopy(messages), "tools": tools})
        self.live_messages.append(messages)
        if self.clock is not None:
            self.clock.now += self.cost
        if not self.script:
            raise AssertionError("FakeModel script exhausted")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeTrace:
    def __init__(self) -> None:
        self.steps: list[tuple[int, dict[str, Any], str]] = []

    def save_step(self, n: int, image: Image.Image, action: dict[str, Any], note: str) -> Path:
        self.steps.append((n, action, note))
        return Path(f"{n:03d}.png")


def tool_decision(*calls: tuple[str, dict[str, Any]], text: str = "") -> Decision:
    """Decision whose raw blocks mirror what ClaudeModel would produce."""
    tool_calls = tuple(ToolCall(id=f"toolu_{i}_{name}", name=name, input=args) for i, (name, args) in enumerate(calls))
    raw: list[dict[str, Any]] = [{"type": "thinking", "thinking": "", "signature": "sig"}]
    if text:
        raw.append({"type": "text", "text": text})
    raw.extend({"type": "tool_use", "id": c.id, "name": c.name, "input": c.input} for c in tool_calls)
    return Decision(tool_calls=tool_calls, text=text, stop_reason="tool_use", raw=tuple(raw))


def overlaps(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> bool:
    """True when two (x, y, w, h) rectangles intersect."""
    return a[0] < b[0] + b[2] and b[0] < a[0] + a[2] and a[1] < b[1] + b[3] and b[1] < a[1] + a[3]


def text_decision(text: str) -> Decision:
    return Decision(text=text, stop_reason="end_turn", raw=({"type": "text", "text": text},))


class RoutedModel(FakeModel):
    """Computes each decision from the request, for scripts that must react to message content."""

    def __init__(self, route: Callable[[str, list[Any], list[Any]], Decision]) -> None:
        super().__init__([])
        self.route = route

    async def decide(self, system: str, messages: list[Any], tools: list[Any]) -> Decision:
        self.requests.append({"system": system, "messages": copy.deepcopy(messages), "tools": tools})
        self.live_messages.append(messages)
        return self.route(system, messages, tools)
