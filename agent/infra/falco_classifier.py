"""Optional local Falco Swift classifier used only as a cautious planning hint."""

import asyncio
import importlib
import importlib.util
import logging
import sys
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Protocol

from agent.domain.models import Classification

log = logging.getLogger(__name__)

CLASSIFICATION_TIMEOUT_S = 0.3
MIN_CONFIDENCE = 0.8
MAX_CONSECUTIVE_FAILURES = 3
CIRCUIT_BREAKER_SECONDS = 600.0

FALCO_QUESTIONS: dict[str, dict[str, Any]] = {
    "tipo": {
        "type": "choice",
        "instructions": "Tipo principal de la tarea de automatización.",
        "options": ["crear", "modificar", "eliminar", "exportar", "pregunta"],
    },
    "destructiva": {
        "type": "noul",
        "instructions": "La tarea elimina datos o puede afectar irreversiblemente varios registros.",
    },
    "dos_datos": {
        "type": "noul",
        "instructions": "La tarea pide responder exactamente dos datos distintos.",
    },
}


class FalcoModel(Protocol):
    def prediction(self, state: str, questions: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]: ...


FalcoLoader = Callable[[str], FalcoModel]


class FalcoClassifier:
    """Loads Falco off the event loop and degrades to ``None`` on every failure mode."""

    def __init__(
        self,
        enabled: bool,
        model_dir: str,
        loader: FalcoLoader | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._enabled = enabled
        self._model_dir = model_dir
        self._loader = loader or _load_falco_model
        self._clock = clock
        self._model: FalcoModel | None = None
        self._load_task: asyncio.Task[None] | None = None
        self._consecutive_failures = 0
        self._disabled_until = 0.0

    @property
    def available(self) -> bool:
        return self._enabled and self._model is not None and self._clock() >= self._disabled_until

    def start(self) -> asyncio.Task[None] | None:
        """Begin loading after application startup without delaying readiness."""
        if not self._enabled or self._load_task is not None:
            return self._load_task
        self._load_task = asyncio.create_task(self._load())
        return self._load_task

    async def stop(self) -> None:
        if self._load_task is not None and not self._load_task.done():
            self._load_task.cancel()
            await asyncio.gather(self._load_task, return_exceptions=True)

    async def classify(self, instruction: str) -> Classification | None:
        if not self._enabled or self._clock() < self._disabled_until:
            return None
        if self._model is None:
            self.start()
            return None
        try:
            prediction = await asyncio.wait_for(
                asyncio.to_thread(self._model.prediction, instruction, FALCO_QUESTIONS),
                timeout=CLASSIFICATION_TIMEOUT_S,
            )
            self._consecutive_failures = 0
            return _to_classification(prediction)
        except Exception as exc:
            self._record_failure(exc)
            return None

    async def _load(self) -> None:
        try:
            self._model = await asyncio.to_thread(self._loader, self._model_dir)
            self._consecutive_failures = 0
            log.info("Falco quedó disponible como pista opcional")
        except Exception as exc:
            self._record_failure(exc)

    def _record_failure(self, exc: Exception) -> None:
        self._consecutive_failures += 1
        log.warning("Falco no está disponible: %s", type(exc).__name__)
        if self._consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
            self._disabled_until = self._clock() + CIRCUIT_BREAKER_SECONDS
            self._consecutive_failures = 0
            log.warning("Falco se desactivó durante %s segundos tras fallos consecutivos", CIRCUIT_BREAKER_SECONDS)


def _to_classification(prediction: Mapping[str, Any]) -> Classification | None:
    task_type = _choice(prediction.get("tipo"))
    destructive = _noul(prediction.get("destructiva"))
    two_data = _noul(prediction.get("dos_datos"))
    if task_type is None and destructive is None and two_data is None:
        return None
    confidence = max(
        (
            _confidence(prediction.get(name))
            for name in ("tipo", "destructiva", "dos_datos")
            if prediction.get(name) is not None
        ),
        default=0.0,
    )
    return Classification(
        app="",
        intent=task_type or "",
        confidence=confidence,
        task_type=task_type,
        destructive=destructive,
        two_data=two_data,
    )


def _choice(value: Any) -> str | None:
    if not isinstance(value, Mapping) or _confidence(value) < MIN_CONFIDENCE:
        return None
    choice = value.get("choice")
    return str(choice) if choice in FALCO_QUESTIONS["tipo"]["options"] else None


def _noul(value: Any) -> bool | None:
    if not isinstance(value, Mapping):
        return None
    probability = value.get("noul")
    if not isinstance(probability, (int, float)):
        return None
    confidence = max(float(probability), 1.0 - float(probability))
    if confidence < MIN_CONFIDENCE:
        return None
    return float(probability) >= 0.5


def _confidence(value: Any) -> float:
    if not isinstance(value, Mapping):
        return 0.0
    if value.get("type") == "noul" and isinstance(value.get("noul"), (int, float)):
        probability = float(value["noul"])
        return max(probability, 1.0 - probability)
    confidence = value.get("confidence")
    return float(confidence) if isinstance(confidence, (int, float)) else 0.0


def _load_falco_model(model_dir: str) -> FalcoModel:
    """Import optional dependencies only when Falco was explicitly enabled."""
    importlib.import_module("torch")
    importlib.import_module("transformers")
    directory = Path(model_dir).resolve()
    source_root = directory.parent
    model_path = source_root / "model.py"
    if not model_path.is_file():
        raise FileNotFoundError(f"No se encontró model.py junto a FALCO_MODEL_DIR={directory}")
    sys.path.insert(0, str(source_root))
    try:
        spec = importlib.util.spec_from_file_location("_agent_qa_falco_model", model_path)
        if spec is None or spec.loader is None:
            raise ImportError("No se pudo cargar el módulo de Falco")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.Classifier(str(directory))
    finally:
        sys.path.pop(0)