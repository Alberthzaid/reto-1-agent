"""FastAPI entrypoint wiring the process-local task service."""

import logging
from pathlib import Path

from dotenv import load_dotenv

from agent.api.app import create_app
from agent.application.manager import TaskManager
from agent.application.worker import DummyWorker
from agent.config import Settings
from agent.infra.sqlite_memory import SqliteMemory
from agent.observability.log_setup import register_secret, setup_logging

log = logging.getLogger(__name__)

load_dotenv()
settings = Settings.from_env()
setup_logging(settings.log_level)
for _secret in (settings.anthropic_api_key, settings.team_token, settings.desktop_password):
    register_secret(_secret)

memory = SqliteMemory(Path(settings.data_dir) / "agent.db")
manager = TaskManager(
    memory=memory,
    worker_factory=lambda _instruction, _url, _password: DummyWorker(),
    time_limit_sec=settings.time_limit_sec,
    max_concurrent_tasks=settings.max_concurrent_tasks,
)
app = create_app(manager, settings.team_token)
