"""Async port Protocols. The domain depends only on these."""

from collections.abc import Sequence
from typing import Any, Protocol, runtime_checkable

from PIL import Image

from agent.domain.models import (
    Classification,
    Decision,
    Procedure,
    Task,
    TextBox,
)


@runtime_checkable
class Screen(Protocol):
    async def connect(self, url: str, password: str) -> None: ...

    async def screenshot(self) -> Image.Image: ...

    async def click(self, x: int, y: int, button: str = "left", double: bool = False) -> None: ...

    async def type_text(self, text: str) -> None: ...

    async def key(self, combo: str) -> None: ...

    async def scroll(self, x: int, y: int, steps: int) -> None: ...

    def last_change_ts(self) -> float: ...

    async def close(self) -> None: ...


@runtime_checkable
class Reader(Protocol):
    async def read(
        self, img: Image.Image, region: tuple[int, int, int, int] | None = None
    ) -> list[TextBox]: ...


@runtime_checkable
class Model(Protocol):
    async def decide(self, system: str, messages: list[Any], tools: list[Any]) -> Decision: ...


@runtime_checkable
class Memory(Protocol):
    def fingerprint(self, boxes: list[TextBox]) -> str: ...

    async def procedures(self, app: str, intent: str) -> list[Procedure]: ...

    async def save_procedure(self, app: str, procedure: Procedure) -> None: ...

    async def penalize(self, app: str, intent: str, procedures: Sequence[Procedure]) -> None: ...

    async def save_task(self, task: Task) -> None: ...

    async def load_task(self, task_id: str) -> Task | None: ...

    async def list_tasks(self, limit: int = 50) -> list[Task]: ...

    async def mark_stale_running_as_failed(self) -> int: ...


@runtime_checkable
class Classifier(Protocol):
    async def classify(self, instruction: str) -> Classification | None: ...
