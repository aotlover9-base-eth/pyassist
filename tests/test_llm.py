"""LLM client: SSE parsing, tool-call assembly and error mapping.

All HTTP is served by ``httpx.MockTransport``, so these tests are hermetic and
never touch the network.
"""

from __future__ import annotations

import json

import httpx
import pytest

from pyassist.config import Config
from pyassist.core.llm import LLMClient, LLMError


def sse(*events: dict | str) -> bytes:
    """Encode dicts/strings as an OpenAI-style SSE stream."""
    lines = []
    for event in events:
        payload = event if isinstance(event, str) else json.dumps(event)
        lines.append(f"data: {payload}\n\n")
    lines.append("data: [DONE]\n\n")
    return "".join(lines).encode()


def delta_event(**delta: object) -> dict:
    return {"choices": [{"index": 0, "delta": delta}]}


def make_client(handler, **overrides) -> LLMClient:
    config = Config(api_key="k", base_url="https://api.test/v1", **overrides)
    transport = httpx.MockTransport(handler)
    return LLMClient(config, httpx.AsyncClient(transport=transport))


# ----------------------------------------------------------------------
# streaming
# ----------------------------------------------------------------------
async def test_stream_yields_text_deltas() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=sse(
                delta_event(content="Hel"),
                delta_event(content="lo"),
                {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
            ),
            headers={"content-type": "text/event-stream"},
        )

    client = make_client(handler)
    try:
        text = ""
        finish = ""
        async for chunk in client.stream_chat([{"role": "user", "content": "hi"}]):
            text += chunk.text_delta
            finish = chunk.finish_reason or finish
        assert text == "Hello"
        assert finish == "stop"
    finally:
        await client.aclose()


async def test_stream_ignores_malformed_json_lines() -> None:
    """A bad frame must not kill an otherwise healthy stream."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=sse("{not json", delta_event(content="ok")),
            headers={"content-type": "text/event-stream"},
        )

    client = make_client(handler)
    try:
        text = "".join(
            [c.text_delta async for c in client.stream_chat([{"role": "user", "content": "x"}])]
        )
        assert text == "ok"
    finally:
        await client.aclose()


async def test_stream_surfaces_tool_call_deltas() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=sse(
                delta_event(
                    tool_calls=[
                        {
                            "index": 0,
                            "id": "call_a",
                            "type": "function",
                            "function": {"name": "read_file", "arguments": '{"pa'},
                        }
                    ]
                ),
                delta_event(
                    tool_calls=[{"index": 0, "function": {"arguments": 'th": "a.txt"}'}}]
                ),
            ),
            headers={"content-type": "text/event-stream"},
        )

    client = make_client(handler)
    try:
        deltas = [
            d
            async for chunk in client.stream_chat([{"role": "user", "content": "x"}])
            for d in chunk.tool_call_deltas
        ]
        assert deltas  # the engine is responsible for stitching these together
    finally:
        await client.aclose()


async def test_stream_reports_usage() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=sse(
                delta_event(content="hi"),
                {
                    "choices": [],
                    "usage": {
                        "prompt_tokens": 10,
                        "completion_tokens": 3,
                        "total_tokens": 13,
                    },
                },
            ),
            headers={"content-type": "text/event-stream"},
        )

    client = make_client(handler)
    try:
        totals = [
            chunk.usage.total_tokens
            async for chunk in client.stream_chat([{"role": "user", "content": "x"}])
        ]
        assert 13 in totals
    finally:
        await client.aclose()


# ----------------------------------------------------------------------
# non-streaming
# ----------------------------------------------------------------------
async def test_chat_parses_content_and_tool_calls() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        # Omitting `stream` is valid (the API defaults to false); what matters
        # is that a one-shot call never asks for a stream.
        assert not json.loads(request.content).get("stream")
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "done",
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {"name": "current_time", "arguments": "{}"},
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
            },
        )

    client = make_client(handler)
    try:
        result = await client.chat([{"role": "user", "content": "hi"}])
        assert result.content == "done"
        assert result.tool_calls[0].name == "current_time"
        assert result.usage.total_tokens == 7
    finally:
        await client.aclose()


# ----------------------------------------------------------------------
# request shape
# ----------------------------------------------------------------------
async def test_request_includes_auth_and_model() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    client = make_client(handler, model="my-model", temperature=0.3)
    try:
        await client.chat([{"role": "user", "content": "hi"}])
        assert seen["auth"] == "Bearer k"
        assert seen["url"] == "https://api.test/v1/chat/completions"
        assert seen["body"]["model"] == "my-model"
        assert seen["body"]["temperature"] == 0.3
        # max_tokens defaults to 0 => must be omitted, not sent as 0.
        assert "max_tokens" not in seen["body"]
    finally:
        await client.aclose()


async def test_model_override_wins() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["model"] = json.loads(request.content)["model"]
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    client = make_client(handler, model="default-model")
    try:
        await client.chat([{"role": "user", "content": "hi"}], model="override")
        assert seen["model"] == "override"
    finally:
        await client.aclose()


# ----------------------------------------------------------------------
# errors
# ----------------------------------------------------------------------
@pytest.mark.parametrize("status", [401, 404, 429, 500])
async def test_http_errors_become_llm_error(status: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": {"message": "nope"}})

    client = make_client(handler)
    try:
        with pytest.raises(LLMError) as excinfo:
            await client.chat([{"role": "user", "content": "hi"}])
        assert excinfo.value.status_code == status
    finally:
        await client.aclose()


async def test_transport_error_becomes_llm_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    client = make_client(handler)
    try:
        with pytest.raises(LLMError):
            await client.chat([{"role": "user", "content": "hi"}])
    finally:
        await client.aclose()


async def test_missing_api_key_fails_fast() -> None:
    """No key and the default OpenAI URL: fail before hitting the network."""

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("must not send a request without a key")

    config = Config(api_key="", base_url="https://api.openai.com/v1")
    client = LLMClient(config, httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    try:
        with pytest.raises(LLMError):
            await client.chat([{"role": "user", "content": "hi"}])
    finally:
        await client.aclose()


async def test_list_models() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url).endswith("/models")
        return httpx.Response(200, json={"data": [{"id": "m-b"}, {"id": "m-a"}]})

    client = make_client(handler)
    try:
        assert await client.list_models() == ["m-a", "m-b"]  # sorted
    finally:
        await client.aclose()


async def test_list_models_returns_empty_on_error() -> None:
    """Model discovery is best-effort; the settings screen must still open."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    client = make_client(handler)
    try:
        assert await client.list_models() == []
    finally:
        await client.aclose()


# ----------------------------------------------------------------------
# Anthropic Provider Tests
# ----------------------------------------------------------------------
def anthropic_sse(*events: dict | str) -> bytes:
    """Encode dicts/strings as an Anthropic-style SSE stream."""
    lines = []
    for event in events:
        if isinstance(event, str):
            lines.append(f"data: {event}\n\n")
        else:
            etype = event.get("type", "")
            if etype:
                lines.append(f"event: {etype}\n")
            lines.append(f"data: {json.dumps(event)}\n\n")
    return "".join(lines).encode()


def make_anthropic_client(handler, **overrides) -> LLMClient:
    config = Config(
        api_key="sk-ant-testkey",
        base_url="https://api.anthropic.com/v1/",
        model="claude-haiku-5-5",
        **overrides,
    )
    transport = httpx.MockTransport(handler)
    return LLMClient(config, httpx.AsyncClient(transport=transport))


async def test_anthropic_auth_headers_and_url() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["api_key"] = request.headers.get("x-api-key")
        seen["version"] = request.headers.get("anthropic-version")
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "content": [{"type": "text", "text": "hello from claude"}],
                "model": "claude-haiku-5-5",
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 10, "output_tokens": 5},
            },
        )

    client = make_anthropic_client(handler)
    try:
        result = await client.chat([{"role": "user", "content": "hi"}])
        assert result.content == "hello from claude"
        assert seen["url"] == "https://api.anthropic.com/v1/messages"
        assert seen["api_key"] == "sk-ant-testkey"
        assert seen["version"] == "2023-06-01"
        assert seen["auth"] is None  # Anthropic must use x-api-key, not Bearer
        assert seen["body"]["model"] == "claude-haiku-5-5"
        assert seen["body"]["max_tokens"] == 4096  # default required max_tokens
    finally:
        await client.aclose()


async def test_anthropic_payload_structure_system_and_tools() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "id": "msg_2",
                "content": [{"type": "text", "text": "ok"}],
                "usage": {"input_tokens": 5, "output_tokens": 2},
            },
        )

    client = make_anthropic_client(handler, temperature=0.5, max_tokens=2048)
    tools = [
        {
            "type": "function",
            "function": {
                "name": "lookup",
                "description": "Lookup information",
                "parameters": {"type": "object", "properties": {"q": {"type": "string"}}},
            },
        }
    ]
    messages = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "search something"},
    ]
    try:
        await client.chat(messages, tools=tools)
        body = seen["body"]
        # System prompt extracted to top-level
        assert body["system"] == "You are a helpful assistant."
        # Messages array only has user/assistant turns
        assert len(body["messages"]) == 1
        assert body["messages"][0]["role"] == "user"
        # Tool converted to Anthropic input_schema
        assert body["tools"][0]["name"] == "lookup"
        assert body["tools"][0]["description"] == "Lookup information"
        assert "input_schema" in body["tools"][0]
        assert body["tools"][0]["input_schema"]["properties"]["q"]["type"] == "string"
        assert body["max_tokens"] == 2048
        # claude-haiku-5-5 is a Claude 5 model where temperature is deprecated and omitted
        assert "temperature" not in body

        # Legacy models still receive temperature
        await client.chat(messages, model="claude-3-5-sonnet-20241022")
        assert seen["body"]["temperature"] == 0.5
    finally:
        await client.aclose()


async def test_anthropic_streaming_text_and_thinking() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = anthropic_sse(
            {"type": "message_start", "message": {"id": "m1", "usage": {"input_tokens": 12}}},
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "thinking", "thinking": ""},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "thinking_delta", "thinking": "analyzing..."},
            },
            {"type": "content_block_stop", "index": 0},
            {
                "type": "content_block_start",
                "index": 1,
                "content_block": {"type": "text", "text": ""},
            },
            {
                "type": "content_block_delta",
                "index": 1,
                "delta": {"type": "text_delta", "text": "Claude "},
            },
            {
                "type": "content_block_delta",
                "index": 1,
                "delta": {"type": "text_delta", "text": "response"},
            },
            {"type": "content_block_stop", "index": 1},
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn"},
                "usage": {"output_tokens": 6},
            },
            {"type": "message_stop"},
        )
        return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

    client = make_anthropic_client(handler)
    try:
        text = ""
        reasoning = ""
        finish = ""
        usages = []
        async for chunk in client.stream_chat([{"role": "user", "content": "hello"}]):
            if chunk.text_delta:
                text += chunk.text_delta
            if chunk.reasoning_delta:
                reasoning += chunk.reasoning_delta
            if chunk.finish_reason:
                finish = chunk.finish_reason
            if chunk.usage:
                usages.append(chunk.usage)

        assert text == "Claude response"
        assert reasoning == "analyzing..."
        assert finish == "stop"  # mapped from end_turn
        assert any(u.total_tokens == 18 for u in usages)
    finally:
        await client.aclose()


async def test_anthropic_streaming_tool_call() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = anthropic_sse(
            {"type": "message_start", "message": {"id": "m1", "usage": {"input_tokens": 10}}},
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {
                    "type": "tool_use",
                    "id": "toolu_01xyz",
                    "name": "calculate",
                    "input": {},
                },
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "input_json_delta", "partial_json": '{"expr": '},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "input_json_delta", "partial_json": '"2 + 2"}'},
            },
            {"type": "content_block_stop", "index": 0},
            {
                "type": "message_delta",
                "delta": {"stop_reason": "tool_use"},
                "usage": {"output_tokens": 15},
            },
            {"type": "message_stop"},
        )
        return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

    client = make_anthropic_client(handler)
    try:
        chunks = [c async for c in client.stream_chat([{"role": "user", "content": "calc"}])]
        tool_deltas = [d for c in chunks for d in c.tool_call_deltas]
        assert tool_deltas
        # First delta has id and name
        assert tool_deltas[0]["id"] == "toolu_01xyz"
        assert tool_deltas[0]["function"]["name"] == "calculate"
        # Subsequent deltas append arguments
        args_chunks = [
            d["function"].get("arguments", "")
            for d in tool_deltas
            if "arguments" in d["function"]
        ]
        assert "".join(args_chunks) == '{"expr": "2 + 2"}'
        finish_reasons = [c.finish_reason for c in chunks if c.finish_reason]
        assert "tool_calls" in finish_reasons  # mapped from tool_use
    finally:
        await client.aclose()


async def test_anthropic_tool_results_history_and_merging() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "content": [{"type": "text", "text": "result is 4"}],
                "usage": {"input_tokens": 20, "output_tokens": 5},
            },
        )

    client = make_anthropic_client(handler)
    messages = [
        {"role": "user", "content": "calculate 2+2 and 3+3"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "calc", "arguments": '{"x": 2}'},
                },
                {
                    "id": "call_2",
                    "type": "function",
                    "function": {"name": "calc", "arguments": '{"x": 3}'},
                },
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "4"},
        {"role": "tool", "tool_call_id": "call_2", "content": "Error: calculation overflow"},
    ]
    try:
        await client.chat(messages)
        body = seen["body"]
        turns = body["messages"]
        # Turn 0: user
        assert turns[0]["role"] == "user"
        # Turn 1: assistant with 2 tool_use blocks
        assert turns[1]["role"] == "assistant"
        assert len(turns[1]["content"]) == 2
        assert turns[1]["content"][0]["type"] == "tool_use"
        assert turns[1]["content"][0]["id"] == "call_1"
        assert turns[1]["content"][0]["input"] == {"x": 2}
        assert turns[1]["content"][1]["id"] == "call_2"
        # Turn 2: consecutive tool messages merged into single user turn with 2 tool_result blocks
        assert turns[2]["role"] == "user"
        assert len(turns[2]["content"]) == 2
        assert turns[2]["content"][0]["type"] == "tool_result"
        assert turns[2]["content"][0]["tool_use_id"] == "call_1"
        assert turns[2]["content"][0]["content"] == "4"
        assert turns[2]["content"][1]["type"] == "tool_result"
        assert turns[2]["content"][1]["tool_use_id"] == "call_2"
        assert turns[2]["content"][1].get("is_error") is True
    finally:
        await client.aclose()


async def test_anthropic_image_attachment_conversion() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"content": [{"type": "text", "text": "image received"}]})

    client = make_anthropic_client(handler)
    b64_img = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUg=="
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "describe this:"},
                {"type": "image_url", "image_url": {"url": b64_img}},
            ],
        }
    ]
    try:
        await client.chat(messages)
        content_blocks = seen["body"]["messages"][0]["content"]
        assert content_blocks[0] == {"type": "text", "text": "describe this:"}
        img_block = content_blocks[1]
        assert img_block["type"] == "image"
        assert img_block["source"]["type"] == "base64"
        assert img_block["source"]["media_type"] == "image/png"
        assert img_block["source"]["data"] == "iVBORw0KGgoAAAANSUhEUg=="
    finally:
        await client.aclose()


@pytest.mark.parametrize(
    ("status", "error_type", "error_msg"),
    [
        (400, "invalid_request_error", "max_tokens: Field required"),
        (401, "authentication_error", "Invalid API key"),
        (403, "permission_error", "Account has not completed verification"),
        (429, "rate_limit_error", "Rate limit exceeded"),
        (529, "overloaded_error", "Overloaded"),
    ],
)
async def test_anthropic_http_errors_and_streaming_safety(
    status: int, error_type: str, error_msg: str
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status,
            json={"type": "error", "error": {"type": error_type, "message": error_msg}},
        )

    client = make_anthropic_client(handler)
    try:
        # Crucial test: in streaming mode, must NOT raise ResponseNotRead
        with pytest.raises(LLMError) as excinfo:
            async for _ in client.stream_chat([{"role": "user", "content": "hi"}]):
                pass
        assert excinfo.value.status_code == status
        assert error_msg in excinfo.value.detail or error_type in excinfo.value.detail
    finally:
        await client.aclose()


async def test_anthropic_malformed_sse_and_ping() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = (
            b": ping comment\n\n"
            b"data: {broken json\n\n"
            b'event: ping\ndata: {"type": "ping"}\n\n'
            + anthropic_sse(
                {"type": "message_start", "message": {"id": "m1"}},
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "text", "text": "ok"},
                },
                {"type": "message_delta", "delta": {"stop_reason": "end_turn"}},
            )
        )
        return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

    client = make_anthropic_client(handler)
    try:
        chunks = [c async for c in client.stream_chat([{"role": "user", "content": "hi"}])]
        text = "".join(c.text_delta for c in chunks)
        assert text == "ok"
    finally:
        await client.aclose()


async def test_anthropic_stream_error_event() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = anthropic_sse(
            {"type": "error", "error": {"type": "overloaded_error", "message": "Server overloaded"}}
        )
        return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

    client = make_anthropic_client(handler)
    try:
        with pytest.raises(LLMError) as excinfo:
            async for _ in client.stream_chat([{"role": "user", "content": "hi"}]):
                pass
        assert "overloaded" in excinfo.value.message.lower()
    finally:
        await client.aclose()


async def test_anthropic_list_models() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url).endswith("/models")
        assert request.headers.get("x-api-key") == "sk-ant-testkey"
        return httpx.Response(
            200,
            json={
                "data": [
                    {"id": "claude-sonnet-5-5"},
                    {"id": "claude-haiku-5-5"},
                ]
            },
        )

    client = make_anthropic_client(handler)
    try:
        models = await client.list_models()
        assert models == ["claude-haiku-5-5", "claude-sonnet-5-5"]
    finally:
        await client.aclose()


async def test_anthropic_list_models_fallback_on_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="error")

    client = make_anthropic_client(handler)
    try:
        models = await client.list_models()
        assert "claude-haiku-5-5" in models
        assert "claude-sonnet-5-5" in models
    finally:
        await client.aclose()
