"""FastAPI entrypoint wiring the process-local task service."""

import logging
from pathlib import Path

from dotenv import load_dotenv

from agent.api.app import create_app
from agent.application.manager import TaskManager
from agent.application.runtime import RuntimeConfig
from agent.config import Settings
from agent.infra.falco_classifier import FalcoClassifier
from agent.infra.sqlite_memory import SqliteMemory
from agent.infra.vnc.keys import BLOCKED_COMBOS
from agent.observability.log_setup import log_buffer, register_secret, setup_logging
from agent.ui.mount import mount_ui
from agent.ui.views import PanelContext
from agent.wiring import build_worker_factory

log = logging.getLogger(__name__)

load_dotenv()
settings = Settings.from_env()
setup_logging(settings.log_level)
for _secret in (settings.anthropic_api_key, settings.team_token, settings.desktop_password, settings.ui_password):
    register_secret(_secret)

runtime = RuntimeConfig.from_settings(settings, BLOCKED_COMBOS)
memory = SqliteMemory(Path(settings.data_dir) / "agent.db")
# Always enabled at the adapter; whether it is consulted is decided per task by `runtime.falco_enabled`.
falco = FalcoClassifier(True, settings.falco_base_url)
manager = TaskManager(
    memory=memory,
    worker_factory=build_worker_factory(settings, falco, memory, runtime),
    time_limit_sec=lambda: runtime.current().time_limit_sec,
    max_concurrent_tasks=settings.max_concurrent_tasks,
)
app = create_app(
    manager,
    settings.team_token,
)
mount_ui(
    app,
    PanelContext(
        manager=manager,
        runtime=runtime,
        logs=log_buffer,
        data_dir=Path(settings.data_dir),
        uses_llm=bool(settings.anthropic_api_key),
        desktop_url=settings.desktop_url,
        desktop_password=settings.desktop_password,
        procedures=memory,
    ),
    enabled=settings.ui_enabled,
    password=settings.panel_password,
)
