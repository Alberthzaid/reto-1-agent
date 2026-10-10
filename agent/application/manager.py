"""In-process task scheduling with one active worker per desktop."""

import asyncio
import logging
import uuid
from collections.abc import Callable
from dataclasses import replace

from agent.application.worker import Worker
from agent.domain.models import Task, TaskStatus
from agent.observability.log_setup import register_secret
from agent.ports.interfaces import Memory

log = logging.getLogger(__name__)

WorkerFactory = Callable[[str, str, str], Worker]


class TaskManager:
    """Schedules task workers and persists every observable task transition."""

    def __init__(
        self,
        memory: Memory,
        worker_factory: WorkerFactory,
        time_limit_sec: float,
        max_concurrent_tasks: int,
    ) -> None:
        self._memory = memory
        self._worker_factory = worker_factory
        self._time_limit_sec = time_limit_sec
        self._semaphore = asyncio.Semaphore(max_concurrent_tasks)
        self._active_by_url: dict[str, asyncio.Task[None]] = {}
        self._tasks: dict[str, Task] = {}
        self._ready = False

    @property
    def ready(self) -> bool:
        return self._ready

    async def start(self) -> None:
        """Recover interrupted tasks before accepting work."""
        await self._memory.mark_stale_running_as_failed()
        self._ready = True

    async def stop(self) -> None:
        """Cancel active workers during application shutdown."""
        active = tuple(self._active_by_url.values())
        for task in active:
            task.cancel()
        if active:
            await asyncio.gather(*active, return_exceptions=True)
        self._active_by_url.clear()
        self._ready = False

    def create(self, instruction: str, url: str, password: str) -> str:
        """Create a task immediately; all worker and persistence I/O stays backgrounded."""
        task_id = str(uuid.uuid4())
        task = Task(id=task_id, instruction=instruction, desktop_url=url)
        self._tasks[task_id] = task
        register_secret(password)
        previous = self._active_by_url.get(url)
        if previous is not None and not previous.done():
            previous.cancel()
        scheduled = asyncio.create_task(self._run_after_previous(task, password, previous))
        self._active_by_url[url] = scheduled
        return task_id

    async def get(self, task_id: str) -> Task | None:
        return self._tasks.get(task_id) or await self._memory.load_task(task_id)

    async def _save(self, task: Task) -> None:
        self._tasks[task.id] = task
        await self._memory.save_task(task)

    async def _run_after_previous(
        self, task: Task, password: str, previous: asyncio.Task[None] | None
    ) -> None:
        try:
            await self._save(task)
            if previous is not None:
                await asyncio.gather(previous, return_exceptions=True)
            await self._run(task, password)
        except asyncio.CancelledError:
            await self._save(replace(task, estado=TaskStatus.fallida, resumen="reemplazada"))
            raise
        finally:
            if self._active_by_url.get(task.desktop_url) is asyncio.current_task():
                self._active_by_url.pop(task.desktop_url, None)

    async def _run(self, task: Task, password: str) -> None:
        worker: Worker | None = None
        running = replace(task, estado=TaskStatus.en_curso)
        try:
            async with self._semaphore:
                await self._save(running)
                worker = self._worker_factory(task.instruction, task.desktop_url, password)
                try:
                    async with asyncio.timeout(self._time_limit_sec):
                        status, summary = await worker.run()
                except TimeoutError:
                    summary = self._partial_summary(worker, "tiempo agotado")
                    await self._save(replace(running, estado=TaskStatus.fallida, resumen=summary))
                except asyncio.CancelledError:
                    await self._save(replace(running, estado=TaskStatus.fallida, resumen="reemplazada"))
                    raise
                except Exception:
                    log.exception("el worker de la tarea %s terminó con un error", task.id)
                    await self._save(replace(running, estado=TaskStatus.fallida, resumen="error interno"))
                else:
                    await self._save(replace(running, estado=status, resumen=summary))
        finally:
            if worker is not None:
                await worker.close()

    @staticmethod
    def _partial_summary(worker: Worker, reason: str) -> str:
        try:
            return worker.partial_summary(reason)
        except Exception:
            log.exception("el worker no pudo producir un resumen parcial")
            return reason