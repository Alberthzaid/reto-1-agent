import io
import json
import logging
from collections.abc import Iterator

import pytest

from agent.observability import log_setup
from agent.observability.log_setup import RedactingFormatter, register_secret, setup_logging

REDACTION_FIXTURE = "redaction-fixture-value"
JSON_REDACTION_FIXTURE = 'redaction"\\fixture'


@pytest.fixture(autouse=True)
def _clear_registry() -> Iterator[None]:
    log_setup.clear_secrets()
    yield
    log_setup.clear_secrets()


@pytest.fixture
def stream_logger() -> Iterator[tuple[logging.Logger, io.StringIO]]:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(RedactingFormatter())
    logger = logging.getLogger("test.redaction")
    logger.handlers = [handler]
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    yield logger, stream
    logger.handlers = []


def test_message_masked(stream_logger: tuple[logging.Logger, io.StringIO]) -> None:
    logger, stream = stream_logger
    register_secret(REDACTION_FIXTURE)
    logger.info("pw=%s", REDACTION_FIXTURE)
    out = stream.getvalue()
    assert "pw=***" in out and REDACTION_FIXTURE not in out


def test_args_masked(stream_logger: tuple[logging.Logger, io.StringIO]) -> None:
    logger, stream = stream_logger
    register_secret(REDACTION_FIXTURE)
    logger.info("pw=%s", REDACTION_FIXTURE)
    assert REDACTION_FIXTURE not in stream.getvalue()
    assert "pw=***" in stream.getvalue()


def test_traceback_masked(stream_logger: tuple[logging.Logger, io.StringIO]) -> None:
    logger, stream = stream_logger
    register_secret(REDACTION_FIXTURE)
    try:
        raise ValueError(f"bad {REDACTION_FIXTURE}")
    except ValueError:
        logger.exception("failed")
    out = stream.getvalue()
    assert REDACTION_FIXTURE not in out and "***" in out


def test_unregistered_text_intact(stream_logger: tuple[logging.Logger, io.StringIO]) -> None:
    logger, stream = stream_logger
    register_secret(REDACTION_FIXTURE)
    logger.info("hola mundo")
    assert json.loads(stream.getvalue())["message"] == "hola mundo"


def test_output_is_json_lines_with_level_and_logger(stream_logger: tuple[logging.Logger, io.StringIO]) -> None:
    logger, stream = stream_logger
    logger.warning("a")
    logger.error("b")
    lines = stream.getvalue().strip().splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["level"] == "WARNING" and first["logger"] == "test.redaction" and first["message"] == "a"
    assert json.loads(lines[1])["level"] == "ERROR"


def test_secret_with_json_special_chars_masked(stream_logger: tuple[logging.Logger, io.StringIO]) -> None:
    logger, stream = stream_logger
    register_secret(JSON_REDACTION_FIXTURE)
    logger.info("x=%s", JSON_REDACTION_FIXTURE)
    assert json.loads(stream.getvalue())["message"] == "x=***"
    assert "fixture" not in stream.getvalue()


def test_empty_secret_ignored(stream_logger: tuple[logging.Logger, io.StringIO]) -> None:
    logger, stream = stream_logger
    register_secret("")
    logger.info("plain")
    assert json.loads(stream.getvalue())["message"] == "plain"


def test_setup_logging_installs_redacting_handler() -> None:
    stream = io.StringIO()
    root = logging.getLogger()
    saved = root.handlers[:]
    try:
        setup_logging("INFO", stream=stream)
        register_secret("tok")
        logging.getLogger("x").info("v=tok")
        assert "tok" not in stream.getvalue() and "v=***" in stream.getvalue()
    finally:
        root.handlers = saved


def _buffered_logger(capacity: int = 2000) -> tuple[logging.Logger, log_setup.MemoryLogHandler]:
    buffer = log_setup.MemoryLogHandler(capacity=capacity)
    logger = logging.getLogger("test.buffer")
    logger.handlers = [buffer]
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    return logger, buffer


def test_memory_buffer_is_bounded() -> None:
    logger, buffer = _buffered_logger(capacity=3)
    for i in range(5):
        logger.info("linea %d", i)
    assert [r["message"] for r in buffer.records()] == ["linea 2", "linea 3", "linea 4"]


def test_memory_buffer_default_capacity_is_2000() -> None:
    assert log_setup.MemoryLogHandler().capacity == 2000


def test_memory_buffer_records_are_redacted() -> None:
    logger, buffer = _buffered_logger()
    register_secret(REDACTION_FIXTURE)
    logger.info("pw=%s", REDACTION_FIXTURE)
    try:
        raise ValueError(REDACTION_FIXTURE)
    except ValueError:
        logger.exception("fallo")
    text = json.dumps(buffer.records())
    assert REDACTION_FIXTURE not in text
    assert buffer.records()[0]["message"] == "pw=***"


def test_memory_buffer_filters_by_level_text_and_task() -> None:
    logger, buffer = _buffered_logger()
    logger.debug("detalle")
    token = log_setup.current_task_id.set("task-a")
    try:
        logger.info("conectando al escritorio")
        logger.warning("primer cuadro lento")
    finally:
        log_setup.current_task_id.reset(token)
    logger.error("Conectando falló")
    assert [r["message"] for r in buffer.records(level="WARNING")] == ["primer cuadro lento", "Conectando falló"]
    assert [r["message"] for r in buffer.records(contains="conectando")] == [
        "conectando al escritorio", "Conectando falló",
    ]
    assert [r["message"] for r in buffer.records(task_id="task-a")] == [
        "conectando al escritorio", "primer cuadro lento",
    ]
    assert buffer.records(task_id="task-a")[0]["task_id"] == "task-a"
    assert len(buffer.records(level=None, contains="", task_id="")) == 4


def test_setup_logging_attaches_memory_buffer() -> None:
    stream = io.StringIO()
    buffer = log_setup.MemoryLogHandler()
    root = logging.getLogger()
    saved = root.handlers[:]
    try:
        setup_logging("INFO", stream=stream, buffer=buffer)
        logging.getLogger("x").info("hola")
        assert buffer in root.handlers
        assert buffer.records()[-1]["message"] == "hola"
    finally:
        root.handlers = saved
