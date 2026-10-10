"""In-process task scheduling with one active worker per desktop."""

import asyncio
import logging
import uuid
from collections.abc import Callable
from dataclasses import replace

from agent.application.worker import Worker
from agent.domain.models import Task, TaskStatus
from agent.observability.log_setup import current_task_id, register_secret
from agent.ports.interfaces import Memory

log = logging.getLogger(__name__)

# (task_id, instruction, desktop_url, password) -> worker
WorkerFactory = Callable[[str, str, str, str], Worker]

REPLACED_SUMMARY = "reemplazada"
CANCELLED_SUMMARY = "cancelada por el usuario"
FINAL_STATES = frozenset({TaskStatus.terminada, TaskStatus.fallida, TaskStatus.cancelada})


class TaskManager:
    """Schedules task workers and persists every observable task transition."""

    def __init__(
        self,
        memory: Memory,
        worker_factory: WorkerFactory,
        time_limit_sec: float | Callable[[], float],
        max_concurrent_tasks: int,
    ) -> None:
        self._memory = memory
        self._worker_factory = worker_factory
        self._time_limit_sec = time_limit_sec
        self._semaphore = asyncio.Semaphore(max_concurrent_tasks)
        self._active_by_url: dict[str, asyncio.Task[None]] = {}
        self._running: dict[str, asyncio.Task[None]] = {}
        self._cancelled: set[str] = set()
        self._tasks: dict[str, Task] = {}
        self._ready = False

    @property
    def ready(self) -> bool:
        return self._ready

    @property
    def running_count(self) -> int:
        """Tasks scheduled in this process that have not finished yet."""
        return sum(1 for scheduled in self._running.values() if not scheduled.done())

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
        self._running[task_id] = scheduled
        return task_id

    async def cancel(self, task_id: str) -> bool:
        """Cancel a pending or running task; it ends as `cancelada`. False if it is unknown or finished."""
        scheduled = self._running.get(task_id)
        task = self._tasks.get(task_id)
        if scheduled is None or scheduled.done() or task is None or task.estado in FINAL_STATES:
            return False
        self._cancelled.add(task_id)
        scheduled.cancel()
        await asyncio.gather(scheduled, return_exceptions=True)
        log.info("tarea %s cancelada por el usuario", task_id)
        return True

    async def get(self, task_id: str) -> Task | None:
        return self._tasks.get(task_id) or await self._memory.load_task(task_id)

    async def list_tasks(self, limit: int = 50) -> list[Task]:
        """Most recent tasks first; in-process state wins over the stored projection."""
        merged = {task.id: task for task in await self._memory.list_tasks(limit)}
        merged.update(self._tasks)
        return sorted(merged.values(), key=lambda task: task.created_at, reverse=True)[:limit]

    async def _save(self, task: Task) -> None:
        self._tasks[task.id] = task
        await self._memory.save_task(task)

    def _interrupted(self, task: Task) -> Task:
        if task.id in self._cancelled:
            return replace(task, estado=TaskStatus.cancelada, resumen=CANCELLED_SUMMARY)
        return replace(task, estado=TaskStatus.fallida, resumen=REPLACED_SUMMARY)

    def _time_limit(self) -> float:
        limit = self._time_limit_sec
        return limit() if callable(limit) else limit

    async def _run_after_previous(
        self, task: Task, password: str, previous: asyncio.Task[None] | None
    ) -> None:
        current_task_id.set(task.id)
        try:
            await self._save(task)
            if previous is not None:
                await asyncio.gather(previous, return_exceptions=True)
            await self._run(task, password)
        except asyncio.CancelledError:
            await self._save(self._interrupted(task))
            raise
        finally:
            if self._active_by_url.get(task.desktop_url) is asyncio.current_task():
                self._active_by_url.pop(task.desktop_url, None)
            self._running.pop(task.id, None)
            self._cancelled.discard(task.id)

    async def _run(self, task: Task, password: str) -> None:
        worker: Worker | None = None
        running = replace(task, estado=TaskStatus.en_curso)
        try:
            async with self._semaphore:
                await self._save(running)
                worker = self._worker_factory(task.id, task.instruction, task.desktop_url, password)
                try:
                    async with asyncio.timeout(self._time_limit()):
                        status, summary = await worker.run()
                except TimeoutError:
                    summary = self._partial_summary(worker, "tiempo agotado")
                    await self._save(replace(running, estado=TaskStatus.fallida, resumen=summary))
                except asyncio.CancelledError:
                    await self._save(self._interrupted(running))
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
