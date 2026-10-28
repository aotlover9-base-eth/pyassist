"""LLM Provider abstractions and factory."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .anthropic import OFFICIAL_ANTHROPIC_MODELS, AnthropicProvider
from .base import (
    BaseProvider,
    ChatResult,
    LLMError,
    StreamChunk,
    error_from_response,
    transport_error,
)
from .openai import OpenAIProvider

if TYPE_CHECKING:
    import httpx

    from ...config import Config

__all__ = [
    "OFFICIAL_ANTHROPIC_MODELS",
    "AnthropicProvider",
    "BaseProvider",
    "ChatResult",
    "LLMError",
    "OpenAIProvider",
    "StreamChunk",
    "create_provider",
    "error_from_response",
    "transport_error",
]


def create_provider(config: Config, client: httpx.AsyncClient | None = None) -> BaseProvider:
    """Instantiate the appropriate provider for the given configuration."""
    if config.resolved_provider == "anthropic":
        return AnthropicProvider(config, client=client)
    return OpenAIProvider(config, client=client)
