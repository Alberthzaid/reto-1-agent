from typing import Any

import anthropic
import httpx2
import pytest
from anthropic.types.beta import BetaMessage

from agent.domain.budget import task_deadline
from agent.infra.llm_model import ClaudeModel, build_client, supports_server_fallbacks

REQUEST = httpx2.Request("POST", "https://api.test/v1/messages")
TOOLS = [
    {"name": "click", "description": "c", "input_schema": {"type": "object", "properties": {}}},
    {"name": "finish", "description": "f", "input_schema": {"type": "object", "properties": {}}},
]


def message(content: list[dict[str, Any]], stop_reason: str = "tool_use") -> BetaMessage:
    return BetaMessage.model_validate(
        {
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "model": "test-model",
            "content": content,
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }
    )


OK = message(
    [
        {"type": "thinking", "thinking": "", "signature": "sig-1"},
        {"type": "text", "text": "Hago clic."},
        {"type": "tool_use", "id": "toolu_1", "name": "click", "input": {"x": 10, "y": 20}},
    ]
)


def status_error(cls: type[anthropic.APIStatusError], status: int, headers: dict[str, str] | None = None,
                 text: str = "error") -> anthropic.APIStatusError:
    response = httpx2.Response(status, headers=headers or {}, request=REQUEST)
    return cls(text, response=response, body=None)


class FakeMessages:
    def __init__(self, script: list[Any]) -> None:
        self.script = script
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> BetaMessage:
        self.calls.append(kwargs)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeBeta:
    def __init__(self, messages: FakeMessages) -> None:
        self.messages = messages


class FakeClient:
    def __init__(self, script: list[Any]) -> None:
        self.messages_api = FakeMessages(script)
        self.beta = FakeBeta(self.messages_api)
        self.options: list[dict[str, Any]] = []

    def with_options(self, **kwargs: Any) -> "FakeClient":
        self.options.append(kwargs)
        return self


class Clock:
    def __init__(self) -> None:
        self.now = 100.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def make(script: list[Any], deadline: float | None = None, **kwargs: Any) -> tuple[ClaudeModel, FakeClient, Clock]:
    client = FakeClient(script)
    clock = Clock()
    model = ClaudeModel(
        client,  # type: ignore[arg-type]
        "test-model",
        effort="medium",
        deadline_fn=lambda: deadline,
        clock=clock,
        sleep=clock.sleep,
        rng=lambda: 0.0,
        **kwargs,
    )
    return model, client, clock


@pytest.mark.asyncio
async def test_maps_tool_use_text_and_raw_blocks() -> None:
    model, _, _ = make([OK])
    decision = await model.decide("sys", [{"role": "user", "content": "hola"}], TOOLS)
    assert decision.stop_reason == "tool_use"
    assert decision.text == "Hago clic."
    assert len(decision.tool_calls) == 1
    call = decision.tool_calls[0]
    assert (call.id, call.name, call.input) == ("toolu_1", "click", {"x": 10, "y": 20})
    assert decision.raw[0] == {"type": "thinking", "thinking": "", "signature": "sig-1"}
    assert decision.raw[2]["type"] == "tool_use"
    assert all(isinstance(block, dict) for block in decision.raw)


@pytest.mark.asyncio
async def test_request_shape_uses_context_management_auto_tool_choice_and_caching() -> None:
    model, client, _ = make([OK])
    messages = [{"role": "user", "content": "hola"}]
    await model.decide("system prompt", messages, TOOLS)
    kwargs = client.messages_api.calls[0]
    assert kwargs["model"] == "test-model"
    assert kwargs["tool_choice"] == {"type": "auto"}
    assert kwargs["thinking"] == {"type": "adaptive"}
    assert kwargs["output_config"] == {"effort": "medium"}
    assert "context-management-2025-06-27" in kwargs["betas"]
    assert "server-side-fallback-2026-07-01" in kwargs["betas"]
    assert kwargs["fallbacks"] == "default"
    edit = kwargs["context_management"]["edits"][0]
    assert edit["type"] == "clear_tool_uses_20250919"
    assert edit["trigger"] == {"type": "tool_uses", "value": 4}
    assert edit["keep"] == {"type": "tool_uses", "value": 3}
    assert kwargs["system"] == [{"type": "text", "text": "system prompt", "cache_control": {"type": "ephemeral"}}]
    assert kwargs["tools"][-1]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in TOOLS[-1], "caller tool specs must not be mutated"
    assert kwargs["messages"] is messages
    assert client.options[0] == {"timeout": 40.0, "max_retries": 0}


@pytest.mark.asyncio
async def test_retries_rate_limit_respecting_retry_after() -> None:
    model, client, clock = make([status_error(anthropic.RateLimitError, 429, {"retry-after": "3"}), OK])
    decision = await model.decide("s", [], TOOLS)
    assert decision.tool_calls
    assert len(client.messages_api.calls) == 2
    assert clock.sleeps == [3.0]


@pytest.mark.asyncio
async def test_retries_server_errors_and_connection_errors() -> None:
    model, client, clock = make(
        [
            status_error(anthropic.InternalServerError, 500),
            anthropic.APIConnectionError(request=REQUEST),
            anthropic.APITimeoutError(request=REQUEST),
            OK,
        ]
    )
    await model.decide("s", [], TOOLS)
    assert len(client.messages_api.calls) == 4
    assert len(clock.sleeps) == 3
    assert clock.sleeps[0] < clock.sleeps[1] < clock.sleeps[2]


@pytest.mark.asyncio
async def test_bad_request_is_not_retried() -> None:
    model, client, clock = make([status_error(anthropic.BadRequestError, 400, text="invalid tools"), OK])
    with pytest.raises(anthropic.BadRequestError):
        await model.decide("s", [], TOOLS)
    assert len(client.messages_api.calls) == 1
    assert clock.sleeps == []


@pytest.mark.asyncio
async def test_gives_up_after_max_attempts() -> None:
    errors = [status_error(anthropic.InternalServerError, 503) for _ in range(5)]
    model, client, _ = make(errors, max_attempts=3)
    with pytest.raises(anthropic.APIStatusError):
        await model.decide("s", [], TOOLS)
    assert len(client.messages_api.calls) == 3


@pytest.mark.asyncio
async def test_never_sleeps_past_the_deadline() -> None:
    model, client, clock = make(
        [status_error(anthropic.RateLimitError, 429, {"retry-after": "30"}), OK], deadline=110.0
    )
    with pytest.raises(anthropic.RateLimitError):
        await model.decide("s", [], TOOLS)
    assert clock.sleeps == []
    assert len(client.messages_api.calls) == 1


@pytest.mark.asyncio
async def test_timeout_is_capped_by_remaining_time() -> None:
    model, client, _ = make([OK], deadline=115.0)
    await model.decide("s", [], TOOLS)
    assert client.options[0]["timeout"] == pytest.approx(15.0)


@pytest.mark.asyncio
async def test_deadline_defaults_to_task_context() -> None:
    client = FakeClient([status_error(anthropic.RateLimitError, 429, {"retry-after": "30"}), OK])
    clock = Clock()
    model = ClaudeModel(client, "m", effort="high", clock=clock, sleep=clock.sleep)  # type: ignore[arg-type]
    token = task_deadline.set(105.0)
    try:
        with pytest.raises(anthropic.RateLimitError):
            await model.decide("s", [], TOOLS)
    finally:
        task_deadline.reset(token)
    assert clock.sleeps == []


@pytest.mark.asyncio
async def test_rejected_fallbacks_are_dropped_once_and_retried() -> None:
    model, client, _ = make(
        [status_error(anthropic.BadRequestError, 400, text="fallbacks: not supported for this model"), OK, OK]
    )
    await model.decide("s", [], TOOLS)
    assert len(client.messages_api.calls) == 2
    retry = client.messages_api.calls[1]
    assert "fallbacks" not in retry
    assert "server-side-fallback-2026-07-01" not in retry["betas"]
    await model.decide("s", [], TOOLS)
    assert "fallbacks" not in client.messages_api.calls[2]


@pytest.mark.asyncio
async def test_response_without_tool_use_maps_to_empty_calls() -> None:
    model, _, _ = make([message([{"type": "text", "text": "listo"}], stop_reason="end_turn")])
    decision = await model.decide("s", [], TOOLS)
    assert decision.tool_calls == ()
    assert decision.stop_reason == "end_turn"
    assert decision.text == "listo"


def test_build_client_targets_anthropic_by_default() -> None:
    client = build_client("redaction-fixture-value", None)
    assert "api.anthropic.com" in str(client.base_url)
    assert client.api_key == "redaction-fixture-value"


def test_build_client_uses_bearer_auth_for_openrouter() -> None:
    client = build_client("redaction-fixture-value", "https://openrouter.ai/api")
    assert str(client.base_url).startswith("https://openrouter.ai/api")
    assert client.auth_token == "redaction-fixture-value"


@pytest.mark.parametrize(
    "base_url,expected",
    [(None, True), ("https://api.anthropic.com", True), ("https://openrouter.ai/api", False)],
)
def test_server_fallbacks_only_on_anthropic(base_url: str | None, expected: bool) -> None:
    assert supports_server_fallbacks(base_url) is expected
