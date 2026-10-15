"""Provider HTTP behaviour tests using a fake aiohttp session (no network)."""

from __future__ import annotations

import pytest

from ai.provider import (
    AIAuthError,
    AIConfigurationError,
    AIRateLimitError,
    AIRequestError,
    ChatMessage,
    OpenAICompatibleProvider,
)
from config import AIConfig

SUCCESS_BODY = (
    '{"model":"gpt-4o-mini","choices":[{"message":{"content":"hello"},'
    '"finish_reason":"stop"}],"usage":{"prompt_tokens":3,"completion_tokens":1,'
    '"total_tokens":4}}'
)


class FakeResponse:
    def __init__(self, status: int, body: str, headers: dict | None = None) -> None:
        self.status = status
        self._body = body
        self.headers = headers or {}

    async def text(self) -> str:
        return self._body

    async def __aenter__(self) -> "FakeResponse":
        return self

    async def __aexit__(self, *exc) -> bool:
        return False


class FakeSession:
    def __init__(self, responses) -> None:
        self._responses = list(responses)
        self.calls: list[tuple] = []
        self.closed = False

    def post(self, url, headers=None, json=None):  # noqa: A002 - mirrors aiohttp
        self.calls.append((url, headers, json))
        return self._responses.pop(0)


def make_config(**overrides) -> AIConfig:
    defaults = dict(
        provider="openai",
        model="gpt-4o-mini",
        api_key="sk-test",
        base_url=None,
        temperature=0.7,
        max_tokens=100,
        timeout=5.0,
        max_retries=2,
    )
    defaults.update(overrides)
    return AIConfig(**defaults)


async def test_successful_completion():
    session = FakeSession([FakeResponse(200, SUCCESS_BODY)])
    provider = OpenAICompatibleProvider(make_config(), session=session)
    result = await provider.complete([ChatMessage.user("hi")])
    assert result.text == "hello"
    assert result.usage.total_tokens == 4
    url, headers, payload = session.calls[0]
    assert url.endswith("/chat/completions")
    assert headers["Authorization"] == "Bearer sk-test"
    assert payload["model"] == "gpt-4o-mini"
    assert payload["messages"] == [{"role": "user", "content": "hi"}]


async def test_rate_limit_is_retried_then_succeeds():
    session = FakeSession(
        [FakeResponse(429, "", {"Retry-After": "0"}), FakeResponse(200, SUCCESS_BODY)]
    )
    provider = OpenAICompatibleProvider(make_config(), session=session)
    result = await provider.complete([ChatMessage.user("hi")])
    assert result.text == "hello"
    assert len(session.calls) == 2


async def test_server_errors_are_retried_then_fail():
    session = FakeSession([FakeResponse(500, "boom"), FakeResponse(500, "boom"), FakeResponse(500, "boom")])
    provider = OpenAICompatibleProvider(make_config(max_retries=2), session=session)
    with pytest.raises(AIRequestError):
        await provider.complete([ChatMessage.user("hi")])


async def test_auth_error_is_not_retried():
    session = FakeSession([FakeResponse(401, '{"error":{"message":"bad key"}}')])
    provider = OpenAICompatibleProvider(make_config(), session=session)
    with pytest.raises(AIAuthError):
        await provider.complete([ChatMessage.user("hi")])
    assert len(session.calls) == 1


async def test_rate_limit_error_type():
    session = FakeSession([FakeResponse(429, "", {"Retry-After": "0"})] * 3)
    provider = OpenAICompatibleProvider(make_config(max_retries=2), session=session)
    with pytest.raises(AIRateLimitError):
        await provider.complete([ChatMessage.user("hi")])


def test_missing_base_url_raises_configuration_error():
    with pytest.raises(AIConfigurationError):
        OpenAICompatibleProvider(make_config(provider="openai_compatible", base_url=None))
