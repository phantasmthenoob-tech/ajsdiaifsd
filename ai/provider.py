"""Provider-independent AI interface.

The rest of the application only ever talks to :class:`AIProvider`.  A concrete
implementation for any OpenAI-compatible ``/chat/completions`` endpoint is
provided, plus a deterministic ``EchoProvider`` used by tests and as a safe
fallback when no credentials are configured.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence

import aiohttp

from config import AIConfig

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Messages & results
# ---------------------------------------------------------------------------
@dataclass
class ChatMessage:
    """A single message sent to or received from a provider."""

    role: str
    content: str
    name: Optional[str] = None

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.name:
            payload["name"] = self.name
        return payload

    @classmethod
    def user(cls, content: str, name: Optional[str] = None) -> "ChatMessage":
        return cls("user", content, name)

    @classmethod
    def assistant(cls, content: str) -> "ChatMessage":
        return cls("assistant", content)

    @classmethod
    def system(cls, content: str) -> "ChatMessage":
        return cls("system", content)


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


@dataclass
class CompletionResult:
    text: str
    model: str = ""
    usage: Usage = field(default_factory=Usage)
    finish_reason: Optional[str] = None
    raw: Optional[dict[str, Any]] = None


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------
class AIError(Exception):
    """Base class for all AI-layer failures."""


class AIConfigurationError(AIError):
    """The provider is misconfigured (missing key, bad URL, ...)."""


class AIRequestError(AIError):
    """The provider returned an error response."""

    def __init__(self, message: str, *, status: Optional[int] = None, retryable: bool = False):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


class AIRateLimitError(AIRequestError):
    def __init__(self, message: str, *, retry_after: Optional[float] = None):
        super().__init__(message, status=429, retryable=True)
        self.retry_after = retry_after


class AITimeoutError(AIRequestError):
    def __init__(self, message: str = "AI request timed out"):
        super().__init__(message, retryable=True)


class AIAuthError(AIRequestError):
    def __init__(self, message: str = "AI provider rejected the credentials"):
        super().__init__(message, status=401, retryable=False)


class AIResponseError(AIError):
    """The provider responded with a body we could not parse."""


# ---------------------------------------------------------------------------
# Provider interface
# ---------------------------------------------------------------------------
class AIProvider(ABC):
    """Common interface every AI backend must implement."""

    #: Human-readable provider name for logs/embeds.
    name: str = "provider"

    @abstractmethod
    async def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> CompletionResult:
        """Return a single completion for the given messages."""

    async def close(self) -> None:  # pragma: no cover - default no-op
        return None


DEFAULT_BASE_URLS: Mapping[str, Optional[str]] = {
    "openai": "https://api.openai.com/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "groq": "https://api.groq.com/openai/v1",
    "together": "https://api.together.xyz/v1",
    "deepseek": "https://api.deepseek.com/v1",
    "mistral": "https://api.mistral.ai/v1",
    "ollama": "http://localhost:11434/v1",
    "lmstudio": "http://localhost:1234/v1",
    "azure-openai": None,  # base URL must be supplied
    "openai_compatible": None,  # base URL must be supplied
}


class OpenAICompatibleProvider(AIProvider):
    """Talks to any endpoint implementing OpenAI's ``/chat/completions`` API."""

    def __init__(self, config: AIConfig, *, session: Optional[aiohttp.ClientSession] = None) -> None:
        self.config = config
        self.name = config.provider
        base = config.base_url or DEFAULT_BASE_URLS.get(config.provider)
        if not base:
            raise AIConfigurationError(
                f"AI_BASE_URL is required for provider {config.provider!r}"
            )
        self.base_url = base.rstrip("/")
        if not config.api_key and config.provider not in {"ollama", "lmstudio"}:
            raise AIConfigurationError("AI_API_KEY is required for this provider")
        self._session = session
        self._owns_session = session is None

    # -- HTTP plumbing ------------------------------------------------------
    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=self.config.timeout)
            self._session = aiohttp.ClientSession(timeout=timeout)
            self._owns_session = True
        return self._session

    def _endpoint(self) -> str:
        url = f"{self.base_url}/chat/completions"
        if self.config.provider == "azure-openai":
            version = self.config.api_version or "2024-06-01"
            separator = "&" if "?" in url else "?"
            url = f"{url}{separator}api-version={version}"
        return url

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.config.api_key:
            if self.config.provider == "azure-openai":
                headers["api-key"] = self.config.api_key
            else:
                headers["Authorization"] = f"Bearer {self.config.api_key}"
        if self.config.provider == "openrouter":
            headers.setdefault("X-Title", "Discord AI Chatbot")
        return headers

    def _payload(
        self,
        messages: Sequence[ChatMessage],
        model: Optional[str],
        temperature: Optional[float],
        max_tokens: Optional[int],
    ) -> dict[str, Any]:
        return {
            "model": model or self.config.model,
            "messages": [m.to_payload() for m in messages],
            "temperature": self.config.temperature if temperature is None else temperature,
            "max_tokens": self.config.max_tokens if max_tokens is None else max_tokens,
            "stream": False,
        }

    @staticmethod
    def _extract_error(body: Any) -> str:
        if isinstance(body, dict):
            error = body.get("error")
            if isinstance(error, dict) and error.get("message"):
                return str(error["message"])
            if isinstance(error, str):
                return error
            if body.get("message"):
                return str(body["message"])
        return "provider returned an error"

    @staticmethod
    def _parse_result(body: Any, model: str) -> CompletionResult:
        try:
            choice = body["choices"][0]
        except (KeyError, IndexError, TypeError) as exc:
            raise AIResponseError("Provider response did not contain any choices") from exc
        message = choice.get("message") or {}
        content = message.get("content")
        if content is None and choice.get("text") is not None:  # legacy completions shape
            content = choice["text"]
        if not isinstance(content, str):
            raise AIResponseError("Provider response did not contain message content")
        usage_raw = body.get("usage") or {}
        usage = Usage(
            prompt_tokens=int(usage_raw.get("prompt_tokens", 0) or 0),
            completion_tokens=int(usage_raw.get("completion_tokens", 0) or 0),
            total_tokens=int(usage_raw.get("total_tokens", 0) or 0),
        )
        return CompletionResult(
            text=content,
            model=str(body.get("model") or model),
            usage=usage,
            finish_reason=choice.get("finish_reason"),
            raw=body if isinstance(body, dict) else None,
        )

    @staticmethod
    def _retry_delay(attempt: int, retry_after: Optional[float]) -> float:
        if retry_after is not None:
            return max(0.0, min(retry_after, 30.0))
        # Exponential backoff with full jitter, capped at 20 seconds.
        base = min(2 ** attempt, 20.0)
        return random.uniform(0, base)

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> CompletionResult:
        session = await self._get_session()
        payload = self._payload(messages, model, temperature, max_tokens)
        last_error: Optional[Exception] = None

        for attempt in range(self.config.max_retries + 1):
            try:
                async with session.post(
                    self._endpoint(), headers=self._headers(), json=payload
                ) as response:
                    text = await response.text()
                    if response.status >= 500:
                        raise AIRequestError(
                            f"Provider server error ({response.status})",
                            status=response.status,
                            retryable=True,
                        )
                    if response.status == 429:
                        retry_after = response.headers.get("Retry-After")
                        raise AIRateLimitError(
                            "Provider rate limit reached",
                            retry_after=float(retry_after) if retry_after else None,
                        )
                    if response.status in (401, 403):
                        raise AIAuthError()
                    if response.status >= 400:
                        body = _safe_json(text)
                        raise AIRequestError(
                            self._extract_error(body), status=response.status, retryable=False
                        )
                    body = _safe_json(text)
                    if body is None:
                        raise AIResponseError("Provider response was not valid JSON")
                    return self._parse_result(body, model or self.config.model)

            except (asyncio.TimeoutError, aiohttp.ClientError) as exc:
                last_error = AITimeoutError(f"AI request failed: {exc.__class__.__name__}")
            except AIRequestError as exc:
                if not exc.retryable:
                    raise
                last_error = exc

            if attempt < self.config.max_retries:
                delay = self._retry_delay(
                    attempt,
                    getattr(last_error, "retry_after", None)
                    if isinstance(last_error, AIRateLimitError)
                    else None,
                )
                logger.warning(
                    "AI request failed (attempt %d/%d): %s; retrying in %.1fs",
                    attempt + 1,
                    self.config.max_retries + 1,
                    last_error,
                    delay,
                )
                await asyncio.sleep(delay)

        assert last_error is not None
        raise last_error

    async def close(self) -> None:
        if self._owns_session and self._session is not None and not self._session.closed:
            await self._session.close()
            self._session = None


class EchoProvider(AIProvider):
    """Deterministic fallback used by tests and when no provider is reachable."""

    name = "echo"

    def __init__(self, *, prefix: str = "[echo] ") -> None:
        self.prefix = prefix
        self.calls: list[list[ChatMessage]] = []

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> CompletionResult:
        self.calls.append(list(messages))
        last_user = next(
            (m for m in reversed(messages) if m.role == "user"), None
        )
        text = f"{self.prefix}{(last_user.content if last_user else '')}".strip()
        return CompletionResult(text=text, model=model or "echo")


def _safe_json(text: str) -> Optional[Any]:
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def create_provider(config: AIConfig, *, session: Optional[aiohttp.ClientSession] = None) -> AIProvider:
    """Factory that maps ``AI_PROVIDER`` onto a concrete implementation."""

    provider = (config.provider or "openai").lower()
    if provider == "echo":
        return EchoProvider()
    return OpenAICompatibleProvider(config, session=session)
