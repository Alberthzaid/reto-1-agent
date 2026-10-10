"""Structured (JSON lines) logging that redacts registered secrets everywhere."""

import json
import logging
import sys
from typing import TextIO

_SECRETS: set[str] = set()


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


def setup_logging(level: str = "INFO", stream: TextIO | None = None) -> logging.Handler:
    """Install a redacting JSON handler on the root logger (UTF-8 safe stream)."""
    target: TextIO = stream if stream is not None else sys.stderr
    reconfigure = getattr(target, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(encoding="utf-8", errors="replace")
    handler = logging.StreamHandler(target)
    handler.setFormatter(RedactingFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())
    return handler
