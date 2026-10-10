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
