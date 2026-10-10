"""Persistent, secret-redacted visual task traces."""

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image

from agent.observability.log_setup import redact

log = logging.getLogger(__name__)

ACTIONS_FILE = "actions.jsonl"
_SAFE_TASK_ID = re.compile(r"^[A-Za-z0-9_-]+$")


class TraceWriter:
    """Writes ordered screenshots and action metadata for a single task."""

    def __init__(self, task_id: str, data_dir: Path = Path("data")) -> None:
        self._directory = data_dir / "traces" / task_id
        self._directory.mkdir(parents=True, exist_ok=True)
        self._actions_path = self._directory / ACTIONS_FILE

    def save_step(self, n: int, image: Image.Image, action: dict[str, Any], note: str) -> Path:
        """Save the image and its matching, redacted JSONL action record."""
        image_path = self._directory / f"{n:03d}.png"
        image.save(image_path)
        payload = {"step": n, "image": image_path.name, "action": _redact_value(action), "note": redact(note)}
        with self._actions_path.open("a", encoding="utf-8") as actions_file:
            actions_file.write(json.dumps(payload, ensure_ascii=False) + "\n")
        return image_path


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {str(key): _redact_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    if isinstance(value, tuple):
        return [_redact_value(item) for item in value]
    return value

@dataclass(frozen=True, slots=True)
class TraceStep:
    """One persisted step: the screenshot path (None if missing), the redacted action and note."""

    step: int
    image: Path | None
    action: dict[str, Any]
    note: str


def read_trace(task_id: str, data_dir: Path = Path("data")) -> list[TraceStep]:
    """Steps written by `TraceWriter` for `task_id`; tolerant of a half-written last line."""
    if not _SAFE_TASK_ID.fullmatch(task_id):
        return []
    directory = data_dir / "traces" / task_id
    try:
        lines = (directory / ACTIONS_FILE).read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    except OSError:
        log.exception("no se pudo leer la traza de la tarea")
        return []
    steps: list[TraceStep] = []
    for line in lines:
        try:
            record = json.loads(line)
            image = directory / Path(str(record["image"])).name
            steps.append(
                TraceStep(
                    step=int(record["step"]),
                    image=image if image.is_file() else None,
                    action=dict(record.get("action") or {}),
                    note=str(record.get("note", "")),
                )
            )
        except (ValueError, KeyError, TypeError):
            continue
    return steps
