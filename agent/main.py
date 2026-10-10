"""FastAPI entrypoint wiring the process-local task service."""

import logging
import uuid
from pathlib import Path

from anthropic import AsyncAnthropic
from dotenv import load_dotenv

from agent.api.app import create_app
from agent.application.manager import TaskManager, WorkerFactory
from agent.application.worker import AgentWorker, DummyWorker, Worker
from agent.config import Settings
from agent.infra.llm_model import ClaudeModel
from agent.infra.sqlite_memory import SqliteMemory
from agent.infra.tesseract_ocr import TesseractReader
from agent.infra.vnc.keys import BLOCKED_COMBOS
from agent.infra.vnc.vnc_screen import VncScreen
from agent.observability.log_setup import register_secret, setup_logging
from agent.observability.traces import TraceWriter

log = logging.getLogger(__name__)


def build_worker_factory(settings: Settings) -> WorkerFactory:
    """Real agent workers when an API key is configured; otherwise the deterministic DummyWorker."""
    if not settings.anthropic_api_key:
        log.warning("ANTHROPIC_API_KEY no está configurada: se usa DummyWorker y no se opera el escritorio")
        return lambda _instruction, _url, _password: DummyWorker()
    client = AsyncAnthropic(api_key=settings.anthropic_api_key)
    model_fast = ClaudeModel(client, settings.model_fast, effort="medium")
    model_strong = ClaudeModel(client, settings.model_strong, effort="high")
    reader = TesseractReader(settings.tesseract_cmd)
    data_dir = Path(settings.data_dir)

    def factory(instruction: str, url: str, password: str) -> Worker:
        return AgentWorker(
            instruction,
            url,
            password,
            settings,
            model_fast,
            model_strong,
            reader,
            VncScreen,
            trace_factory=lambda: TraceWriter(uuid.uuid4().hex, data_dir),
            blocked_combos=BLOCKED_COMBOS,
        )

    return factory


load_dotenv()
settings = Settings.from_env()
setup_logging(settings.log_level)
for _secret in (settings.anthropic_api_key, settings.team_token, settings.desktop_password):
    register_secret(_secret)

memory = SqliteMemory(Path(settings.data_dir) / "agent.db")
manager = TaskManager(
    memory=memory,
    worker_factory=build_worker_factory(settings),
    time_limit_sec=settings.time_limit_sec,
    max_concurrent_tasks=settings.max_concurrent_tasks,
)
app = create_app(manager, settings.team_token)
