"""Base provider interfaces, data models, and shared utilities."""

from __future__ import annotations

import contextlib
import json
import logging
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import httpx

from ..models import ToolCall, Usage

if TYPE_CHECKING:
    from ...config import Config

log = logging.getLogger(__name__)

_DONE_MARKERS = ("[DONE]", "data: [DONE]", "[done]")


@dataclass(slots=True)
class ChatResult:
    """The fully accumulated result of one non-streaming request."""

    content: str
    tool_calls: list[ToolCall]
    reasoning: str = ""
    usage: Usage = field(default_factory=Usage)
    finish_reason: str = ""


@dataclass(slots=True)
class StreamChunk:
    """One SSE chunk from the stream."""

    text_delta: str = ""
    reasoning_delta: str = ""
    tool_call_deltas: list[dict[str, Any]] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    finish_reason: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


class LLMError(Exception):
    """Raised for transport, protocol and 4xx/5xx failures."""

    def __init__(self, message: str, *, status_code: int | None = None, detail: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.detail = detail


class BaseProvider(ABC):
    """Abstract interface implemented by all LLM providers."""

    def __init__(self, config: Config, client: httpx.AsyncClient | None = None) -> None:
        self._config = config
        self._client = client

    @property
    @abstractmethod
    def headers(self) -> dict[str, str]:
        """Headers sent with outgoing HTTP requests."""

    @abstractmethod
    def preflight(self) -> None:
        """Validate prerequisites (e.g. API keys) before sending requests."""

    @abstractmethod
    def stream_chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
    ) -> AsyncIterator[StreamChunk]:
        """Stream chat completions from the provider."""

    @abstractmethod
    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
    ) -> ChatResult:
        """Execute a one-shot non-streaming completion."""

    @abstractmethod
    async def list_models(self) -> list[str]:
        """Fetch available model IDs from the provider."""

    @abstractmethod
    async def aclose(self) -> None:
        """Clean up HTTP transport resources."""


def transport_error(exc: httpx.HTTPError, config: Config) -> LLMError:
    """Turn a connection/timeout failure into an actionable message."""
    if isinstance(exc, httpx.TimeoutException):
        return LLMError(
            f"Request timed out after {config.request_timeout:g}s.",
            detail=str(exc),
        )
    if isinstance(exc, httpx.ConnectError):
        return LLMError(
            f"Could not connect to {config.base_url}. "
            "Check the base URL in Settings (F2), and that the server is running.",
            detail=str(exc),
        )
    return LLMError(f"Network error talking to {config.base_url}.", detail=str(exc))


async def error_from_response(response: httpx.Response) -> LLMError:
    """Build a helpful error from an HTTP error response.

    Always reads response bytes first to prevent 'ResponseNotRead' errors when
    inspecting streaming responses.
    """
    detail = ""
    status = response.status_code
    try:
        raw_bytes = await response.aread()
        payload = json.loads(raw_bytes)
        if isinstance(payload, dict):
            # Anthropic: {"type": "error", "error": {"type": "...", "message": "..."}}
            # OpenAI: {"error": {"message": "...", "code": "...", "type": "..."}}
            err = payload.get("error")
            if isinstance(err, dict):
                msg = str(err.get("message") or "")
                code_or_type = str(err.get("type") or err.get("code") or "")
                if code_or_type and msg and code_or_type != msg:
                    detail = f"{msg} ({code_or_type})"
                else:
                    detail = msg or code_or_type
            elif isinstance(err, str):
                detail = err
            else:
                detail = payload.get("message") or str(payload)
    except Exception:
        with contextlib.suppress(Exception):
            raw = await response.aread()
            detail = raw.decode("utf-8", "replace")[:300]

    if status == 401:
        message = "Authentication failed - check your API key in Settings (F2)."
    elif status == 403:
        message = "Permission denied - check your API key permissions and account tier."
    elif status == 404:
        message = f"Endpoint not found at {response.request.url}. Is base_url correct?"
    elif status == 429:
        message = "Rate limit exceeded or quota exhausted. Try again shortly."
    elif status == 529:
        message = "Server is overloaded. Try again shortly."
    else:
        message = f"Server returned HTTP {status}."

    return LLMError(message, status_code=status, detail=detail.strip())
