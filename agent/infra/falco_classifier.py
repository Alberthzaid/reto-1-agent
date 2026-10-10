"""Optional Falco HTTP classifier used only as a cautious planning hint."""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

import httpx

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


FalcoRequester = Callable[[str, Mapping[str, Any]], Awaitable[Mapping[str, Any]]]


class FalcoClassifier:
    """Calls Falco's decision endpoint and degrades to ``None`` on every failure mode."""

    def __init__(
        self,
        enabled: bool,
        base_url: str,
        requester: FalcoRequester | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._enabled = enabled
        self._decide_url = f"{base_url.rstrip('/')}/v1/decide"
        self._requester = requester or _request_falco
        self._clock = clock
        self._consecutive_failures = 0
        self._disabled_until = 0.0

    @property
    def available(self) -> bool:
        return self._enabled and self._clock() >= self._disabled_until

    async def classify(self, instruction: str) -> Classification | None:
        if not self._enabled or self._clock() < self._disabled_until:
            return None
        try:
            prediction = await asyncio.wait_for(
                self._requester(
                    self._decide_url, {"state": instruction, "questions": FALCO_QUESTIONS}
                ),
                timeout=CLASSIFICATION_TIMEOUT_S,
            )
            self._consecutive_failures = 0
            return _to_classification(prediction)
        except Exception as exc:
            self._record_failure(exc)
            return None

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


async def _request_falco(url: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
    async with httpx.AsyncClient(timeout=CLASSIFICATION_TIMEOUT_S) as client:
        response = await client.post(url, json=payload)
        response.raise_for_status()
        prediction = response.json()
    if not isinstance(prediction, Mapping):
        raise ValueError("La respuesta de Falco debe ser un objeto JSON")
    return prediction