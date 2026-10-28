"""OpenAI-compatible chat completions provider."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

import httpx

from ..models import ToolCall, Usage
from .base import (
    _DONE_MARKERS,
    BaseProvider,
    ChatResult,
    LLMError,
    StreamChunk,
    error_from_response,
    transport_error,
)

if TYPE_CHECKING:
    from ...config import Config

log = logging.getLogger(__name__)


class OpenAIProvider(BaseProvider):
    """Provider for OpenAI and OpenAI-compatible endpoints (/chat/completions)."""

    def __init__(self, config: Config, client: httpx.AsyncClient | None = None) -> None:
        super().__init__(config, client)
        self._headers = _build_headers(config)
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(config.request_timeout),
            headers=self._headers,
            follow_redirects=True,
        )

    @property
    def headers(self) -> dict[str, str]:
        return self._headers

    def preflight(self) -> None:
        if not self._config.is_configured:
            raise LLMError(
                "No API key configured. Press F2 to open Settings, "
                "or run: pyassist --set api_key=sk-...",
                detail=f"base_url is {self._config.base_url}",
            )

    async def stream_chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
    ) -> AsyncIterator[StreamChunk]:
        self.preflight()
        payload = self._build_payload(messages, tools=tools, model=model)
        try:
            async with self._client.stream(
                "POST",
                self._config.chat_completions_url(),
                json=payload,
                headers=self._headers,
            ) as response:
                if response.status_code >= 400:
                    raise await error_from_response(response)

                if not self._config.stream:
                    data = await response.aread()
                    yield _parse_non_streaming(data)
                    return

                async for raw_line in response.aiter_lines():
                    for chunk in _iter_sse_chunks(raw_line):
                        yield chunk
        except httpx.HTTPError as exc:
            raise transport_error(exc, self._config) from exc

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
    ) -> ChatResult:
        self.preflight()
        payload = self._build_payload(messages, tools=tools, model=model, force_no_stream=True)
        try:
            response = await self._client.post(
                self._config.chat_completions_url(), json=payload, headers=self._headers
            )
        except httpx.HTTPError as exc:
            raise transport_error(exc, self._config) from exc
        if response.status_code >= 400:
            raise await error_from_response(response)
        try:
            data = response.json()
        except json.JSONDecodeError as exc:
            raise LLMError("Server returned invalid JSON") from exc
        return _parse_result(data)

    async def list_models(self) -> list[str]:
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
        return sorted(str(model_id) for model_id in ids if model_id)

    async def aclose(self) -> None:
        await self._client.aclose()

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


def _build_headers(config: Config) -> dict[str, str]:
    headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
    api_key = config.api_key.strip()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    for key, value in config.extra_headers.items():
        headers[str(key)] = str(value)
    return headers


def _iter_sse_chunks(line: str) -> list[StreamChunk]:
    if not line:
        return []
    if line.startswith(":"):
        return []
    if not line.startswith("data:"):
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
    if not chunk.usage and isinstance(payload.get("usage"), dict):
        chunk.usage = Usage.from_dict(payload["usage"])
    return chunk


def _parse_non_streaming(data: bytes) -> StreamChunk:
    try:
        payload = json.loads(data)
    except json.JSONDecodeError as exc:
        raise LLMError("Server returned invalid JSON") from exc
    chunk = _parse_chunk(payload)
    chunk.finish_reason = chunk.finish_reason or "stop"
    return chunk


def _parse_result(data: dict[str, Any]) -> ChatResult:
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
