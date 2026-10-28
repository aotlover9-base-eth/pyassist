"""LLM client dispatching to provider implementations (OpenAI, Anthropic)."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx

from ..config import Config
from .models import ToolCall, Usage
from .providers import (
    BaseProvider,
    ChatResult,
    LLMError,
    StreamChunk,
    create_provider,
    error_from_response,
    transport_error,
)
from .providers.openai import (
    _build_headers,
    _iter_sse_chunks,
    _parse_chunk,
    _parse_non_streaming,
    _parse_result,
)

log = logging.getLogger(__name__)

__all__ = [
    "ChatResult",
    "LLMClient",
    "LLMError",
    "StreamChunk",
    "ToolCall",
    "Usage",
    "_build_headers",
    "_error_from_response",
    "_iter_sse_chunks",
    "_parse_chunk",
    "_parse_non_streaming",
    "_parse_result",
    "_transport_error",
]


class LLMClient:
    """Async client that dispatches to the active provider (OpenAI or Anthropic)."""

    def __init__(self, config: Config, client: httpx.AsyncClient | None = None) -> None:
        self._config = config
        self._client = client
        self._provider: BaseProvider = create_provider(config, client=client)
        self._headers = self._provider.headers

    @property
    def provider(self) -> BaseProvider:
        return self._provider

    def _preflight(self) -> None:
        """Fail fast with an actionable message instead of a confusing 401."""
        self._provider.preflight()

    async def stream_chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
    ) -> AsyncIterator[StreamChunk]:
        """Stream a chat completion, yielding incremental chunks."""
        async for chunk in self._provider.stream_chat(messages, tools=tools, model=model):
            yield chunk

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
    ) -> ChatResult:
        """One-shot non-streaming completion."""
        return await self._provider.chat(messages, tools=tools, model=model)

    async def list_models(self) -> list[str]:
        """Fetch model ids (best-effort)."""
        return await self._provider.list_models()

    def _build_payload(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        force_no_stream: bool = False,
    ) -> dict[str, Any]:
        """Backward-compatible helper for building the provider payload."""
        if hasattr(self._provider, "_build_payload"):
            return self._provider._build_payload(  # type: ignore[attr-defined]
                messages, tools=tools, model=model, force_no_stream=force_no_stream
            )
        from .providers.anthropic import format_anthropic_payload

        return format_anthropic_payload(
            self._config, messages, tools=tools, model=model, force_no_stream=force_no_stream
        )

    async def aclose(self) -> None:
        await self._provider.aclose()


# Backward-compatible alias functions
_transport_error = transport_error
_error_from_response = error_from_response
