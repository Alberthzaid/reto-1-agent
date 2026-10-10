from collections.abc import Iterator
from pathlib import Path

import pytest
from PIL import Image

from agent.observability import log_setup
from agent.observability.traces import TraceStep, TraceWriter, read_trace

REDACTION_FIXTURE = "redaction-fixture-value"


@pytest.fixture(autouse=True)
def _clear_registry() -> Iterator[None]:
    log_setup.clear_secrets()
    yield
    log_setup.clear_secrets()


def test_read_trace_round_trips_with_writer(tmp_path: Path) -> None:
    log_setup.register_secret(REDACTION_FIXTURE)
    writer = TraceWriter("task-1", tmp_path)
    image = Image.new("RGB", (10, 10), "white")
    writer.save_step(1, image, {"tool": "click", "x": 3, "y": 4}, "primer clic")
    writer.save_step(2, image, {"tool": "type_text", "text": REDACTION_FIXTURE}, f"escribe {REDACTION_FIXTURE}")
    steps = read_trace("task-1", tmp_path)
    assert steps == [
        TraceStep(step=1, image=tmp_path / "traces" / "task-1" / "001.png",
                  action={"tool": "click", "x": 3, "y": 4}, note="primer clic"),
        TraceStep(step=2, image=tmp_path / "traces" / "task-1" / "002.png",
                  action={"tool": "type_text", "text": "***"}, note="escribe ***"),
    ]


def test_missing_trace_is_empty(tmp_path: Path) -> None:
    assert read_trace("no-existe", tmp_path) == []


def test_malformed_lines_and_missing_images_are_tolerated(tmp_path: Path) -> None:
    directory = tmp_path / "traces" / "task-2"
    directory.mkdir(parents=True)
    (directory / "actions.jsonl").write_text(
        '{"step": 1, "image": "001.png", "action": {"tool": "key"}, "note": "n"}\n{"step": 2, "ima',
        encoding="utf-8",
    )
    assert read_trace("task-2", tmp_path) == [TraceStep(step=1, image=None, action={"tool": "key"}, note="n")]


@pytest.mark.parametrize("task_id", ["../secret", "a/b", "", "..", "a\\b"])
def test_rejects_ids_that_escape_the_trace_dir(tmp_path: Path, task_id: str) -> None:
    assert read_trace(task_id, tmp_path) == []
