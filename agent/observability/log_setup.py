"""Structured (JSON lines) logging that redacts registered secrets everywhere."""

import json
import logging
import sys
import threading
from collections import deque
from contextvars import ContextVar
from typing import Any, TextIO

_SECRETS: set[str] = set()
LOG_BUFFER_CAPACITY = 2000

# Task id of the work running in the current context; asyncio tasks and to_thread calls inherit it.
current_task_id: ContextVar[str | None] = ContextVar("current_task_id", default=None)


def register_secret(value: str | None) -> None:
    """Mask `value` as "***" in every later log record (message, args, traceback)."""
    if value:
        _SECRETS.add(value)


def clear_secrets() -> None:
    _SECRETS.clear()


def _redact(text: str) -> str:
    for secret in sorted(_SECRETS, key=len, reverse=True):
        text = text.replace(secret, "***")
    return text


def redact(text: str) -> str:
    """Redact registered values before persisting observability artifacts."""
    return _redact(text)


class RedactingFormatter(logging.Formatter):
    """Formats a record as one JSON line, redacting secrets in every text field."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, str] = {
            "ts": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "message": _redact(record.getMessage()),
        }
        if record.exc_info:
            payload["exc"] = _redact(self.formatException(record.exc_info))
        elif record.exc_text:
            payload["exc"] = _redact(record.exc_text)
        line = json.dumps(payload, ensure_ascii=False)
        # Safety net: a secret that survived (e.g. split by JSON escaping) is still masked.
        return _redact(line)


class MemoryLogHandler(logging.Handler):
    """Bounded in-memory ring buffer of redacted log records, for the control panel."""

    def __init__(self, capacity: int = LOG_BUFFER_CAPACITY) -> None:
        super().__init__()
        self.capacity = capacity
        self._records: deque[dict[str, Any]] = deque(maxlen=capacity)
        self._buffer_lock = threading.Lock()
        self.setFormatter(RedactingFormatter())

    def emit(self, record: logging.LogRecord) -> None:
        try:
            payload: dict[str, Any] = json.loads(self.format(record))
        except Exception:
            self.handleError(record)
            return
        payload["levelno"] = record.levelno
        payload["task_id"] = current_task_id.get()
        with self._buffer_lock:
            self._records.append(payload)

    def records(
        self, level: str | None = None, contains: str | None = None, task_id: str | None = None
    ) -> list[dict[str, Any]]:
        """Records at or above `level`, whose message holds `contains` (case-insensitive), for `task_id`."""
        minimum = logging.getLevelName(level.upper()) if level else logging.NOTSET
        if not isinstance(minimum, int):
            minimum = logging.NOTSET
        needle = contains.lower() if contains else ""
        with self._buffer_lock:
            snapshot = list(self._records)
        return [
            dict(item)
            for item in snapshot
            if item["levelno"] >= minimum
            and (not needle or needle in item["message"].lower())
            and (not task_id or item["task_id"] == task_id)
        ]


log_buffer = MemoryLogHandler()


def setup_logging(
    level: str = "INFO", stream: TextIO | None = None, buffer: MemoryLogHandler | None = None
) -> logging.Handler:
    """Install a redacting JSON handler (UTF-8 safe stream) plus the in-memory buffer on the root logger."""
    target: TextIO = stream if stream is not None else sys.stderr
    reconfigure = getattr(target, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(encoding="utf-8", errors="replace")
    handler = logging.StreamHandler(target)
    handler.setFormatter(RedactingFormatter())
    root = logging.getLogger()
    root.handlers = [handler, buffer if buffer is not None else log_buffer]
    root.setLevel(level.upper())
    return handler
