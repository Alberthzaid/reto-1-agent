"""Side-effect-free composition of per-task workers (imported by `agent.main` and by tests)."""

import logging
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from agent.application.manager import WorkerFactory
from agent.application.runtime import RuntimeConfig
from agent.application.worker import AgentWorker, DummyWorker, Worker
from agent.config import Settings
from agent.infra.falco_classifier import FalcoClassifier
from agent.infra.llm_model import ClaudeModel, build_client, supports_server_fallbacks
from agent.infra.tesseract_ocr import TesseractReader
from agent.infra.vnc.vnc_screen import VncScreen
from agent.observability.traces import TraceWriter
from agent.ports.interfaces import Memory

log = logging.getLogger(__name__)


def build_worker_factory(
    settings: Settings,
    classifier: FalcoClassifier,
    memory: Memory | None,
    runtime: RuntimeConfig,
    worker_cls: Callable[..., Worker] = AgentWorker,
) -> WorkerFactory:
    """Real agent workers when an API key is configured; otherwise the deterministic DummyWorker.

    Runtime values (models, limits, Falco, blocked combos, prompts) are read for every new task.
    """
    if not settings.anthropic_api_key:
        log.warning("ANTHROPIC_API_KEY no está configurada: se usa DummyWorker y no se opera el escritorio")
        return lambda _task_id, _instruction, _url, _password: DummyWorker()
    client = build_client(settings.anthropic_api_key, settings.anthropic_base_url)
    fallbacks = supports_server_fallbacks(settings.anthropic_base_url)
    reader = TesseractReader(settings.tesseract_cmd)
    data_dir = Path(settings.data_dir)

    def factory(task_id: str, instruction: str, url: str, password: str) -> Worker:
        values = runtime.current()
        task_settings = replace(
            settings,
            model_fast=values.model_fast,
            model_strong=values.model_strong,
            time_limit_sec=values.time_limit_sec,
            falco_enabled=values.falco_enabled,
        )
        return worker_cls(
            instruction,
            url,
            password,
            task_settings,
            ClaudeModel(client, values.model_fast, effort="medium", use_fallbacks=fallbacks),
            ClaudeModel(client, values.model_strong, effort="high", use_fallbacks=fallbacks),
            reader,
            VncScreen,
            trace_factory=lambda: TraceWriter(task_id, data_dir),
            blocked_combos=values.blocked_combos,
            classifier=classifier if values.falco_enabled else None,
            memory=memory,
            max_turns=values.max_turns,
            prompts_dir=values.prompts_dir,
        )

    return factory
