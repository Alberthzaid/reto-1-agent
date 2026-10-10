"""Anthropic Messages API implementation of the `Model` port."""

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable
from typing import Any

import anthropic

from agent.domain.budget import current_deadline
from agent.domain.models import Decision, ToolCall

log = logging.getLogger(__name__)

CONTEXT_MANAGEMENT_BETA = "context-management-2025-06-27"
FALLBACK_BETA = "server-side-fallback-2026-07-01"
CONTEXT_MANAGEMENT: dict[str, Any] = {
    "edits": [
        {
            "type": "clear_tool_uses_20250919",
            "trigger": {"type": "tool_uses", "value": 4},
            "keep": {"type": "tool_uses", "value": 3},
        }
    ]
}
EPHEMERAL: dict[str, str] = {"type": "ephemeral"}
MIN_CALL_WINDOW_S = 2.0  # do not start a call (or sleep) when less than this would remain


def supports_server_fallbacks(base_url: str | None) -> bool:
    """Anthropic's `fallbacks: "default"` only exists on the first-party API."""
    return base_url is None or "api.anthropic.com" in base_url


def build_client(api_key: str, base_url: str | None) -> anthropic.AsyncAnthropic:
    """Shared client; Anthropic-compatible gateways such as OpenRouter take the key as a bearer token."""
    if base_url is None:
        return anthropic.AsyncAnthropic(api_key=api_key)
    return anthropic.AsyncAnthropic(api_key=api_key, auth_token=api_key, base_url=base_url)


class ClaudeModel:
    """Calls Claude with our own tools; append-only history, server-side context editing."""

    def __init__(
        self,
        client: anthropic.AsyncAnthropic,
        model: str,
        effort: str = "medium",
        deadline_fn: Callable[[], float | None] | None = None,
        max_tokens: int = 8192,
        timeout: float = 40.0,
        max_attempts: int = 4,
        base_delay: float = 1.0,
        max_delay: float = 8.0,
        use_fallbacks: bool = True,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rng: Callable[[], float] = random.random,
    ) -> None:
        self._client = client
        self._model = model
        self._effort = effort
        self._deadline_fn = deadline_fn or current_deadline
        self._max_tokens = max_tokens
        self._timeout = timeout
        self._max_attempts = max_attempts
        self._base_delay = base_delay
        self._max_delay = max_delay
        self._use_fallbacks = use_fallbacks
        self._clock = clock
        self._sleep = sleep
        self._rng = rng

    def _request(self, system: str, messages: list[Any], tools: list[Any]) -> dict[str, Any]:
        cached_tools = [dict(tool) for tool in tools]
        if cached_tools:
            cached_tools[-1]["cache_control"] = EPHEMERAL
        betas = [CONTEXT_MANAGEMENT_BETA]
        request: dict[str, Any] = {
            "model": self._model,
            "max_tokens": self._max_tokens,
            "system": [{"type": "text", "text": system, "cache_control": EPHEMERAL}],
            "messages": messages,
            "tools": cached_tools,
            "tool_choice": {"type": "auto"},
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": self._effort},
            "context_management": CONTEXT_MANAGEMENT,
            "betas": betas,
        }
        if self._use_fallbacks:
            betas.append(FALLBACK_BETA)
            request["fallbacks"] = "default"
        return request

    def _remaining(self) -> float | None:
        deadline = self._deadline_fn()
        return None if deadline is None else deadline - self._clock()

    async def decide(self, system: str, messages: list[Any], tools: list[Any]) -> Decision:
        attempt = 0
        while True:
            remaining = self._remaining()
            timeout = self._timeout if remaining is None else max(1.0, min(self._timeout, remaining))
            request = self._request(system, messages, tools)
            try:
                api = self._client.with_options(timeout=timeout, max_retries=0)
                response = await api.beta.messages.create(**request)
                return _to_decision(response)
            except anthropic.BadRequestError as exc:
                if self._use_fallbacks and "fallback" in str(exc).lower():
                    self._use_fallbacks = False
                    log.warning("la API rechazó el respaldo del servidor; se reintenta sin él")
                    continue
                raise
            except anthropic.RateLimitError as exc:
                delay = self._delay(attempt, _retry_after(exc))
                error: Exception = exc
            except anthropic.APIStatusError as exc:
                if exc.status_code < 500:
                    raise
                delay = self._delay(attempt, _retry_after(exc))
                error = exc
            except anthropic.APIConnectionError as exc:
                delay = self._delay(attempt, None)
                error = exc
            attempt += 1
            if attempt >= self._max_attempts:
                raise error
            remaining = self._remaining()
            if remaining is not None and delay + MIN_CALL_WINDOW_S >= remaining:
                log.warning("no queda tiempo para reintentar la llamada al modelo")
                raise error
            log.warning("reintento %d de la llamada al modelo en %.1f s (%s)", attempt, delay, type(error).__name__)
            await self._sleep(delay)

    def _delay(self, attempt: int, retry_after: float | None) -> float:
        if retry_after is not None:
            return retry_after
        backoff = min(self._max_delay, self._base_delay * (2**attempt))
        return backoff + self._rng() * self._base_delay


def _retry_after(exc: anthropic.APIStatusError) -> float | None:
    raw = exc.response.headers.get("retry-after") if exc.response is not None else None
    if raw is None:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return None


def _to_decision(response: Any) -> Decision:
    calls: list[ToolCall] = []
    texts: list[str] = []
    raw: list[dict[str, Any]] = []
    for block in response.content:
        raw.append(block.model_dump(exclude_none=True))
        if block.type == "tool_use":
            payload = block.input if isinstance(block.input, dict) else {}
            calls.append(ToolCall(id=block.id, name=block.name, input=dict(payload)))
        elif block.type == "text":
            texts.append(block.text)
    return Decision(
        tool_calls=tuple(calls),
        text="\n".join(texts),
        stop_reason=response.stop_reason or "",
        raw=tuple(raw),
    )
