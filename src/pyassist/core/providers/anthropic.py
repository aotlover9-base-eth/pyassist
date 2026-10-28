"""Native Anthropic Messages API provider."""

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

#: Current official Anthropic Claude model identifiers.
OFFICIAL_ANTHROPIC_MODELS: list[str] = [
    "claude-fable-5-1",
    "claude-haiku-5-5",
    "claude-opus-5-5",
    "claude-sonnet-5-5",
    "claude-3-7-sonnet-20250219",
    "claude-3-7-sonnet-latest",
    "claude-3-5-sonnet-20241022",
    "claude-3-5-sonnet-latest",
    "claude-3-5-haiku-20241022",
    "claude-3-5-haiku-latest",
    "claude-3-opus-20240229",
    "claude-3-opus-latest",
    "claude-3-sonnet-20240229",
    "claude-3-haiku-20240307",
]


class AnthropicProvider(BaseProvider):
    """Provider implementing the native Anthropic Messages API (POST /v1/messages)."""

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
        if not self._config.api_key.strip():
            raise LLMError(
                "No Anthropic API key configured. Set ANTHROPIC_API_KEY, "
                "press F2 to open Settings, or run: pyassist --set api_key=sk-ant-...",
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
        url = self._config.anthropic_messages_url()
        payload = format_anthropic_payload(
            self._config, messages, tools=tools, model=model
        )

        try:
            async with self._client.stream(
                "POST",
                url,
                json=payload,
                headers=self._headers,
            ) as response:
                if response.status_code >= 400:
                    raise await error_from_response(response)

                if not self._config.stream:
                    data = await response.aread()
                    yield _parse_non_streaming_chunk(data)
                    return

                async for chunk in _iter_anthropic_sse(response):
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
        url = self._config.anthropic_messages_url()
        payload = format_anthropic_payload(
            self._config, messages, tools=tools, model=model, force_no_stream=True
        )

        try:
            response = await self._client.post(url, json=payload, headers=self._headers)
        except httpx.HTTPError as exc:
            raise transport_error(exc, self._config) from exc

        if response.status_code >= 400:
            raise await error_from_response(response)

        try:
            data = response.json()
        except json.JSONDecodeError as exc:
            raise LLMError("Server returned invalid JSON") from exc

        return _parse_chat_result(data)

    async def list_models(self) -> list[str]:
        url = self._config.anthropic_models_url()
        try:
            response = await self._client.get(url, headers=self._headers)
            if response.status_code == 200:
                data = response.json()
                if isinstance(data, dict):
                    entries = data.get("data") or []
                    ids = [e.get("id") for e in entries if isinstance(e, dict) and e.get("id")]
                    if ids:
                        return sorted(str(m) for m in ids)
        except Exception as exc:
            log.debug("anthropic models fetch failed: %s", exc)

        return sorted(OFFICIAL_ANTHROPIC_MODELS)

    async def aclose(self) -> None:
        await self._client.aclose()


# ----------------------------------------------------------------------
# headers
# ----------------------------------------------------------------------
def _build_headers(config: Config) -> dict[str, str]:
    version = getattr(config, "anthropic_version", None) or "2023-06-01"
    headers = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "anthropic-version": version,
    }
    api_key = config.api_key.strip()
    if api_key:
        headers["x-api-key"] = api_key
    for key, value in config.extra_headers.items():
        headers[str(key)] = str(value)
    return headers


# ----------------------------------------------------------------------
# payload conversion
# ----------------------------------------------------------------------
def format_anthropic_payload(
    config: Config,
    messages: list[dict[str, Any]],
    *,
    tools: list[dict[str, Any]] | None = None,
    model: str | None = None,
    force_no_stream: bool = False,
) -> dict[str, Any]:
    """Convert pyassist/OpenAI-format messages to Anthropic's Messages API format."""
    system_parts: list[str] = []
    converted_turns: list[dict[str, Any]] = []

    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")

        # 1. System prompt
        if role == "system":
            if isinstance(content, str) and content.strip():
                system_parts.append(content.strip())
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("text"):
                        system_parts.append(str(part["text"]).strip())
                    elif isinstance(part, str) and part.strip():
                        system_parts.append(part.strip())
            continue

        # 2. Tool result -> user turn with tool_result block
        if role == "tool":
            tool_id = msg.get("tool_call_id") or ""
            text_content = content if isinstance(content, str) else json.dumps(content)
            is_err = text_content.startswith("Error:")
            block: dict[str, Any] = {
                "type": "tool_result",
                "tool_use_id": tool_id,
                "content": text_content,
            }
            if is_err:
                block["is_error"] = True
            converted_turns.append({"role": "user", "content": [block]})
            continue

        # 3. Assistant turn -> text and/or tool_use blocks
        if role == "assistant":
            blocks: list[dict[str, Any]] = []
            if isinstance(content, str) and content.strip():
                blocks.append({"type": "text", "text": content})
            elif isinstance(content, list):
                blocks.extend(content)

            for tc in msg.get("tool_calls") or []:
                fn = tc.get("function") or {}
                name = fn.get("name") or tc.get("name") or ""
                args = fn.get("arguments") if "arguments" in fn else tc.get("arguments")
                parsed_args: dict[str, Any] = {}
                if isinstance(args, str):
                    if args.strip():
                        try:
                            parsed_args = json.loads(args)
                            if not isinstance(parsed_args, dict):
                                parsed_args = {"value": parsed_args}
                        except Exception:
                            parsed_args = {}
                elif isinstance(args, dict):
                    parsed_args = args

                blocks.append(
                    {
                        "type": "tool_use",
                        "id": tc.get("id") or "call_0",
                        "name": name,
                        "input": parsed_args,
                    }
                )

            if not blocks:
                blocks.append({"type": "text", "text": " "})
            converted_turns.append({"role": "assistant", "content": blocks})
            continue

        # 4. User turn -> text and/or image blocks
        blocks = []
        if isinstance(content, str):
            if content:
                blocks.append({"type": "text", "text": content})
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, str) and part:
                    blocks.append({"type": "text", "text": part})
                elif isinstance(part, dict):
                    ptype = part.get("type")
                    if ptype == "text":
                        if part.get("text"):
                            blocks.append(part)
                    elif ptype == "image_url":
                        img_url = part.get("image_url", {}).get("url", "")
                        if img_url.startswith("data:"):
                            prefix, _, b64 = img_url.partition(";base64,")
                            media_type = prefix.removeprefix("data:") or "image/png"
                            blocks.append(
                                {
                                    "type": "image",
                                    "source": {
                                        "type": "base64",
                                        "media_type": media_type,
                                        "data": b64,
                                    },
                                }
                            )
                    elif ptype in ("image", "tool_result"):
                        blocks.append(part)
                    else:
                        blocks.append(part)

        if not blocks:
            blocks.append({"type": "text", "text": " "})
        converted_turns.append({"role": "user", "content": blocks})

    # Merge consecutive turns with the same role (required by Anthropic)
    merged: list[dict[str, Any]] = []
    for turn in converted_turns:
        if not merged:
            merged.append(turn)
        else:
            prev = merged[-1]
            if prev["role"] == turn["role"]:
                prev_content = (
                    prev["content"]
                    if isinstance(prev["content"], list)
                    else [{"type": "text", "text": str(prev["content"])}]
                )
                turn_content = (
                    turn["content"]
                    if isinstance(turn["content"], list)
                    else [{"type": "text", "text": str(turn["content"])}]
                )
                prev["content"] = prev_content + turn_content
            else:
                merged.append(turn)

    # Ensure first turn is user
    if not merged or merged[0]["role"] != "user":
        merged.insert(0, {"role": "user", "content": [{"type": "text", "text": "Hello"}]})

    payload: dict[str, Any] = {
        "model": model or config.model,
        "messages": merged,
    }

    if system_parts:
        payload["system"] = "\n\n".join(system_parts)

    # Anthropic strictly requires max_tokens
    if config.max_tokens > 0:
        payload["max_tokens"] = config.max_tokens
    else:
        payload["max_tokens"] = 4096

    # Newer Anthropic models (Claude 4.7+, Claude 5.x) deprecate temperature and
    # reject requests containing it.
    model_name = model or config.model
    is_newer_model = any(
        model_name.startswith(prefix)
        for prefix in (
            "claude-5",
            "claude-haiku-5",
            "claude-sonnet-5",
            "claude-opus-5",
            "claude-fable-5",
            "claude-4",
        )
    )
    if not is_newer_model and config.temperature is not None:
        payload["temperature"] = max(0.0, min(1.0, float(config.temperature)))

    if tools:
        anthropic_tools = []
        for t in tools:
            if "function" in t:
                fn = t["function"]
                anthropic_tools.append(
                    {
                        "name": fn.get("name", ""),
                        "description": fn.get("description", ""),
                        "input_schema": fn.get(
                            "parameters", {"type": "object", "properties": {}}
                        ),
                    }
                )
            elif "input_schema" in t:
                anthropic_tools.append(t)
            else:
                anthropic_tools.append(
                    {
                        "name": t.get("name", ""),
                        "description": t.get("description", ""),
                        "input_schema": t.get(
                            "parameters", {"type": "object", "properties": {}}
                        ),
                    }
                )
        payload["tools"] = anthropic_tools

    if config.stream and not force_no_stream:
        payload["stream"] = True

    return payload


def map_anthropic_stop_reason(stop_reason: str | None) -> str:
    """Map Anthropic stop reasons to pyassist/OpenAI-equivalent stop reasons."""
    if not stop_reason:
        return ""
    mapping = {
        "end_turn": "stop",
        "stop_sequence": "stop",
        "tool_use": "tool_calls",
        "max_tokens": "length",
    }
    return mapping.get(stop_reason, stop_reason)


# ----------------------------------------------------------------------
# SSE stream parsing
# ----------------------------------------------------------------------
async def _iter_anthropic_sse(response: httpx.Response) -> AsyncIterator[StreamChunk]:
    """Parse an Anthropic SSE stream, yielding standardized StreamChunks."""
    input_tokens = 0
    data_lines: list[str] = []

    async def flush_lines() -> dict[str, Any] | None:
        if not data_lines:
            return None
        combined = "\n".join(data_lines).strip()
        data_lines.clear()
        if not combined or combined in _DONE_MARKERS:
            return None
        try:
            payload = json.loads(combined)
            if isinstance(payload, dict):
                return payload
        except json.JSONDecodeError:
            log.debug("skipping unparseable SSE line: %.80s", combined)
        return None

    async for line in response.aiter_lines():
        line = line.rstrip("\r\n")
        if not line:
            payload = await flush_lines()
            if payload:
                chunk, input_tokens = _process_sse_event(payload, input_tokens)
                if chunk is not None:
                    yield chunk
            continue

        if line.startswith(":"):
            # keep-alive or comment
            continue

        if line.startswith("event:"):
            # event header
            continue

        if line.startswith("data:"):
            data_part = line[len("data:") :].strip()
            data_lines.append(data_part)
            continue

        # bare JSON fallback
        try:
            payload = json.loads(line)
            if isinstance(payload, dict):
                chunk, input_tokens = _process_sse_event(payload, input_tokens)
                if chunk is not None:
                    yield chunk
                continue
        except json.JSONDecodeError:
            if data_lines:
                data_lines.append(line)

    final_payload = await flush_lines()
    if final_payload:
        chunk, input_tokens = _process_sse_event(final_payload, input_tokens)
        if chunk is not None:
            yield chunk


def _process_sse_event(
    event: dict[str, Any], input_tokens: int
) -> tuple[StreamChunk | None, int]:
    """Transform one Anthropic SSE event into a StreamChunk."""
    event_type = event.get("type", "")

    if event_type == "message_start":
        msg = event.get("message", {})
        usage = msg.get("usage", {})
        in_tok = int(usage.get("input_tokens") or 0)
        u = Usage(prompt_tokens=in_tok, completion_tokens=0, total_tokens=in_tok)
        return StreamChunk(usage=u, raw=event), in_tok

    if event_type == "content_block_start":
        idx = int(event.get("index") or 0)
        block = event.get("content_block", {})
        btype = block.get("type")
        if btype == "tool_use":
            call_id = str(block.get("id") or f"call_{idx}")
            name = str(block.get("name") or "")
            chunk = StreamChunk(
                tool_call_deltas=[
                    {"index": idx, "id": call_id, "function": {"name": name}}
                ],
                raw=event,
            )
            return chunk, input_tokens
        if btype == "text" and block.get("text"):
            return StreamChunk(text_delta=block["text"], raw=event), input_tokens
        if btype == "thinking" and block.get("thinking"):
            return (
                StreamChunk(reasoning_delta=block["thinking"], raw=event),
                input_tokens,
            )
        return None, input_tokens

    if event_type == "content_block_delta":
        idx = int(event.get("index") or 0)
        delta = event.get("delta", {})
        dtype = delta.get("type")
        if dtype == "text_delta":
            return StreamChunk(text_delta=delta.get("text") or "", raw=event), input_tokens
        if dtype == "input_json_delta":
            partial = delta.get("partial_json") or ""
            chunk = StreamChunk(
                tool_call_deltas=[{"index": idx, "function": {"arguments": partial}}],
                raw=event,
            )
            return chunk, input_tokens
        if dtype == "thinking_delta":
            return (
                StreamChunk(reasoning_delta=delta.get("thinking") or "", raw=event),
                input_tokens,
            )
        return None, input_tokens

    if event_type == "message_delta":
        delta = event.get("delta", {})
        stop = map_anthropic_stop_reason(delta.get("stop_reason"))
        usage_dict = event.get("usage", {})
        out_tok = int(usage_dict.get("output_tokens") or 0)
        tot = input_tokens + out_tok
        u = Usage(prompt_tokens=input_tokens, completion_tokens=out_tok, total_tokens=tot)
        return StreamChunk(finish_reason=stop, usage=u, raw=event), input_tokens

    if event_type == "error":
        err = event.get("error", {})
        err_type = str(err.get("type") or "api_error")
        err_msg = str(err.get("message") or "Anthropic stream error")
        raise LLMError(f"Anthropic error: {err_msg}", detail=err_type)

    # ping, content_block_stop, message_stop
    return None, input_tokens


# ----------------------------------------------------------------------
# non-streaming parsing
# ----------------------------------------------------------------------
def _parse_chat_result(data: dict[str, Any]) -> ChatResult:
    text_parts: list[str] = []
    thinking_parts: list[str] = []
    tool_calls: list[ToolCall] = []

    for block in data.get("content") or []:
        btype = block.get("type")
        if btype == "text":
            text_parts.append(block.get("text") or "")
        elif btype == "thinking":
            thinking_parts.append(block.get("thinking") or "")
        elif btype == "tool_use":
            call_id = str(block.get("id") or f"call_{len(tool_calls)}")
            name = str(block.get("name") or "")
            inp = block.get("input", {})
            args_str = json.dumps(inp) if isinstance(inp, dict) else str(inp)
            tool_calls.append(ToolCall(id=call_id, name=name, arguments=args_str))

    stop_reason = map_anthropic_stop_reason(data.get("stop_reason"))
    usage_dict = data.get("usage") or {}
    prompt_tokens = int(usage_dict.get("input_tokens") or 0)
    completion_tokens = int(usage_dict.get("output_tokens") or 0)
    u = Usage(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=prompt_tokens + completion_tokens,
    )

    return ChatResult(
        content="".join(text_parts),
        tool_calls=tool_calls,
        reasoning="".join(thinking_parts),
        usage=u,
        finish_reason=stop_reason or "stop",
    )


def _parse_non_streaming_chunk(data: bytes) -> StreamChunk:
    try:
        payload = json.loads(data)
    except json.JSONDecodeError as exc:
        raise LLMError("Server returned invalid JSON") from exc

    result = _parse_chat_result(payload)
    tool_deltas = [
        {
            "index": i,
            "id": tc.id,
            "function": {"name": tc.name, "arguments": tc.arguments},
        }
        for i, tc in enumerate(result.tool_calls)
    ]
    return StreamChunk(
        text_delta=result.content,
        reasoning_delta=result.reasoning,
        tool_call_deltas=tool_deltas,
        usage=result.usage,
        finish_reason=result.finish_reason,
        raw=payload,
    )
