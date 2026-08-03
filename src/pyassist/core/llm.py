"""OpenAI-compatible chat completions client with SSE streaming.

Targets the OpenAI wire protocol (``/chat/completions``) so pyassist works
with OpenAI, Azure OpenAI, Groq, Together, OpenRouter, vLLM, Ollama, LM Studio,
llama.cpp and every other OpenAI-compatible server.

Streaming uses SSE, which OpenAI sends line-by-line as ``data: {json}`` with a
terminal ``data: [DONE]``. We tolerate the format variations providers ship
with: multi-line ``data:`` payloads, ``text/plain`` responses, keep-alive
comments, and chunks that only carry usage.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

import httpx

from ..config import Config
from .models import ToolCall, Usage

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


class LLMClient:
    """Thin async client over the OpenAI chat completions endpoint."""

    def __init__(self, config: Config, client: httpx.AsyncClient | None = None) -> None:
        self._config = config
        # Headers are also sent per-request so an injected client (tests, or a
        # caller supplying its own pool) still authenticates correctly.
        self._headers = _build_headers(config)
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(config.request_timeout),
            headers=self._headers,
            follow_redirects=True,
        )

    def _preflight(self) -> None:
        """Fail fast with an actionable message instead of a confusing 401."""
        if not self._config.is_configured:
            raise LLMError(
                "No API key configured. Press F2 to open Settings, "
                "or run: pyassist --set api_key=sk-...",
                detail=f"base_url is {self._config.base_url}",
            )

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------
    async def stream_chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
    ) -> AsyncIterator[StreamChunk]:
        """Stream a chat completion, yielding incremental chunks."""
        self._preflight()
        payload = self._build_payload(messages, tools=tools, model=model)
        try:
            async with self._client.stream(
                "POST",
                self._config.chat_completions_url(),
                json=payload,
                headers=self._headers,
            ) as response:
                if response.status_code >= 400:
                    raise await _error_from_response(response)

                if not self._config.stream:
                    data = await response.aread()
                    yield _parse_non_streaming(data)
                    return

                async for raw_line in response.aiter_lines():
                    for chunk in _iter_sse_chunks(raw_line):
                        yield chunk
        except httpx.HTTPError as exc:
            raise _transport_error(exc, self._config) from exc

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
    ) -> ChatResult:
        """One-shot non-streaming completion (used by plugins/tools)."""
        self._preflight()
        payload = self._build_payload(messages, tools=tools, model=model, force_no_stream=True)
        try:
            response = await self._client.post(
                self._config.chat_completions_url(), json=payload, headers=self._headers
            )
        except httpx.HTTPError as exc:
            raise _transport_error(exc, self._config) from exc
        if response.status_code >= 400:
            raise await _error_from_response(response)
        try:
            data = response.json()
        except json.JSONDecodeError as exc:
            raise LLMError("Server returned invalid JSON") from exc
        return _parse_result(data)

    async def list_models(self) -> list[str]:
        """Fetch model ids from ``GET /models`` (best-effort)."""
        url = self._config.models_url()
        try:
            response = await self._client.get(url, headers=self._headers)
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, json.JSONDecodeError) as exc:
            log.debug("models fetch failed: %s", exc)
            return []
        if not isinstance(data, dict):
            return []
        entries = data.get("data") or []
        ids = [entry.get("id") for entry in entries if isinstance(entry, dict)]
        # Sorted so the settings model picker is predictable.
        return sorted(str(model_id) for model_id in ids if model_id)

    # ------------------------------------------------------------------
    # payload construction
    # ------------------------------------------------------------------
    def _build_payload(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        force_no_stream: bool = False,
    ) -> dict[str, Any]:
        cfg = self._config
        payload: dict[str, Any] = {
            "model": model or cfg.model,
            "messages": messages,
        }
        if cfg.temperature is not None:
            payload["temperature"] = cfg.temperature
        if cfg.max_tokens > 0:
            payload["max_tokens"] = cfg.max_tokens
        if tools:
            payload["tools"] = tools
        if cfg.stream and not force_no_stream:
            payload["stream"] = True
        return payload

    async def aclose(self) -> None:
        await self._client.aclose()


# ----------------------------------------------------------------------
# headers / errors
# ----------------------------------------------------------------------
def _build_headers(config: Config) -> dict[str, str]:
    headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
    api_key = config.api_key.strip()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    for key, value in config.extra_headers.items():
        headers[str(key)] = str(value)
    return headers


def _transport_error(exc: httpx.HTTPError, config: Config) -> LLMError:
    """Turn a connection/timeout failure into an actionable message.

    Connection refused against a localhost base URL is the single most common
    setup mistake (server not started), so it gets its own hint.
    """
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


async def _error_from_response(response: httpx.Response) -> LLMError:
    """Build a helpful error from an HTTP error response."""
    detail = ""
    try:
        payload = response.json()
        err = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(err, dict):
            detail = err.get("message") or err.get("code") or ""
        else:
            detail = str(payload)
    except (json.JSONDecodeError, ValueError):
        detail = (await response.aread()).decode("utf-8", "replace")[:200]

    status = response.status_code
    if status == 401:
        message = "Authentication failed - check your API key in Settings (F2)."
    elif status == 404:
        message = f"Endpoint not found at {response.request.url}. Is base_url correct?"
    elif status == 429:
        message = "Rate limit exceeded or quota exhausted. Try again shortly."
    else:
        message = f"Server returned HTTP {status}."
    return LLMError(message, status_code=status, detail=detail.strip())


# ----------------------------------------------------------------------
# SSE parsing
# ----------------------------------------------------------------------
def _iter_sse_chunks(line: str) -> list[StreamChunk]:
    """Parse one SSE line into zero or more chunks.

    Handles the standard ``data: {...}`` form and the bare-JSON form some
    local servers emit. Accumulates multi-line JSON payloads until a blank
    line or the next ``data:`` marker.
    """
    if not line:
        return []

    # Keep-alive comments
    if line.startswith(":"):
        return []

    # Accumulate continuation lines of a split JSON payload.
    if not line.startswith("data:"):
        # Some servers stream bare JSON without the data: prefix.
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            return []
        return [_parse_chunk(payload)]

    data = line[len("data:") :].strip()
    if not data:
        return []
    if data in _DONE_MARKERS or data.lower() == "[done]":
        return []

    try:
        payload = json.loads(data)
    except json.JSONDecodeError:
        log.debug("skipping non-JSON SSE data line: %.80s", data)
        return []
    return [_parse_chunk(payload)]


def _parse_chunk(payload: dict[str, Any]) -> StreamChunk:
    choices = payload.get("choices") or []
    choice = choices[0] if choices else {}

    delta = choice.get("delta") or {}
    if not isinstance(delta, dict):
        delta = {}

    chunk = StreamChunk(
        text_delta=delta.get("content") or "",
        reasoning_delta=delta.get("reasoning_content") or delta.get("reasoning") or "",
        tool_call_deltas=delta.get("tool_calls") or [],
        usage=Usage.from_dict(payload.get("usage")),
        finish_reason=choice.get("finish_reason") or "",
        raw=payload,
    )
    # Some servers put usage at the top level of the final chunk.
    if not chunk.usage and isinstance(payload.get("usage"), dict):
        chunk.usage = Usage.from_dict(payload["usage"])
    return chunk


def _parse_non_streaming(data: bytes) -> StreamChunk:
    """Convert a non-streaming JSON response into a single chunk."""
    try:
        payload = json.loads(data)
    except json.JSONDecodeError as exc:
        raise LLMError("Server returned invalid JSON") from exc
    chunk = _parse_chunk(payload)
    chunk.finish_reason = chunk.finish_reason or "stop"
    return chunk


def _parse_result(data: dict[str, Any]) -> ChatResult:
    """Convert a non-streaming JSON response into a ChatResult."""
    choices = data.get("choices") or []
    choice = choices[0] if choices else {}
    message = choice.get("message") or {}
    if not isinstance(message, dict):
        message = {}

    tool_calls: list[ToolCall] = []
    for tc in message.get("tool_calls") or []:
        fn = tc.get("function") or {}
        tool_calls.append(
            ToolCall(
                id=str(tc.get("id") or ""),
                name=str(fn.get("name") or ""),
                arguments=str(fn.get("arguments") or "{}"),
            )
        )

    return ChatResult(
        content=message.get("content") or "",
        tool_calls=tool_calls,
        reasoning=message.get("reasoning_content") or message.get("reasoning") or "",
        usage=Usage.from_dict(data.get("usage")),
        finish_reason=choice.get("finish_reason") or "stop",
    )
