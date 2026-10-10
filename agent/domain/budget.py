"""Per-task time budget shared with adapters through the asyncio task context."""

from contextvars import ContextVar

# Absolute deadline (time.monotonic seconds) of the task running in the current asyncio task.
task_deadline: ContextVar[float | None] = ContextVar("task_deadline", default=None)


def current_deadline() -> float | None:
    """Return the deadline of the task running in this context, if any."""
    return task_deadline.get()
