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
