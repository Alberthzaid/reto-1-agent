"""Persistent, secret-redacted visual task traces."""

import json
from pathlib import Path
from typing import Any

from PIL import Image

from agent.observability.log_setup import redact


class TraceWriter:
    """Writes ordered screenshots and action metadata for a single task."""

    def __init__(self, task_id: str, data_dir: Path = Path("data")) -> None:
        self._directory = data_dir / "traces" / task_id
        self._directory.mkdir(parents=True, exist_ok=True)
        self._actions_path = self._directory / "actions.jsonl"

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