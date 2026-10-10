"""Worker contract and a deterministic implementation for integration tests."""

import asyncio
from typing import Protocol, runtime_checkable

from agent.domain.models import TaskStatus


@runtime_checkable
class Worker(Protocol):
    """Executes one task without exposing connection credentials in its result."""

    async def run(self) -> tuple[TaskStatus, str]: ...

    def partial_summary(self, reason: str) -> str: ...

    async def close(self) -> None: ...


class DummyWorker:
    """Injected placeholder until the screen-driven worker is implemented."""

    def __init__(self, delay_seconds: float = 0.05, summary: str = "tarea de prueba completada") -> None:
        self._delay_seconds = delay_seconds
        self._summary = summary
        self.closed = False

    async def run(self) -> tuple[TaskStatus, str]:
        await asyncio.sleep(self._delay_seconds)
        return TaskStatus.terminada, self._summary

    def partial_summary(self, reason: str) -> str:
        return reason

    async def close(self) -> None:
        self.closed = True