"""The agent loop: streaming, persistence, tool calling and approval gates.

The LLM is faked at the HTTP layer so the engine's real code path (including
the client) is exercised without a network.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx

from pyassist.config import Config
from pyassist.core.engine import ChatEngine
from pyassist.core.events import (
    EngineError,
    MessageCompleted,
    TextDelta,
    ToolCallDenied,
    ToolCallFinished,
    ToolCallStarted,
    TurnFinished,
)
from pyassist.core.llm import LLMClient
from pyassist.tools.base import Tool, ToolContext, ToolRegistry, ToolResult


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------
def sse_text(*texts: str, finish: str = "stop") -> bytes:
    frames = [
        json.dumps({"choices": [{"index": 0, "delta": {"content": text}}]}) for text in texts
    ]
    frames.append(json.dumps({"choices": [{"index": 0, "delta": {}, "finish_reason": finish}]}))
    return "".join(f"data: {f}\n\n" for f in frames).encode() + b"data: [DONE]\n\n"


def sse_tool_call(name: str, arguments: str, call_id: str = "call_1") -> bytes:
    frames = [
        json.dumps(
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": call_id,
                                    "type": "function",
                                    "function": {"name": name, "arguments": arguments},
                                }
                            ]
                        },
                    }
                ]
            }
        ),
        json.dumps({"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]}),
    ]
    return "".join(f"data: {f}\n\n" for f in frames).encode() + b"data: [DONE]\n\n"


class ScriptedAPI:
    """Serves a queued list of SSE bodies, one per request."""

    def __init__(self, *bodies: bytes) -> None:
        self.bodies = list(bodies)
        self.requests: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(json.loads(request.content))
        body = self.bodies.pop(0) if self.bodies else sse_text("done")
        return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})


class EchoTool(Tool):
    name = "echo"
    description = "Echo the given text back."
    parameters = {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    }

    async def run(self, arguments: dict, context: ToolContext) -> ToolResult:
        return ToolResult(content=f"echo: {arguments.get('text', '')}")


class DangerTool(Tool):
    name = "danger"
    description = "Something that needs a confirmation."
    requires_approval = True
    parameters = {"type": "object", "properties": {}}

    def __init__(self) -> None:
        self.ran = False

    async def run(self, arguments: dict, context: ToolContext) -> ToolResult:
        self.ran = True
        return ToolResult(content="did the dangerous thing")


class BoomTool(Tool):
    name = "boom"
    description = "Always raises."
    parameters = {"type": "object", "properties": {}}

    async def run(self, arguments: dict, context: ToolContext) -> ToolResult:
        raise RuntimeError("kaboom")


def build_engine(
    api: ScriptedAPI,
    storage,
    workspace: Path,
    *,
    tools: list[Tool] | None = None,
    approve=None,
    **config_kwargs,
) -> ChatEngine:
    config = Config(
        api_key="k",
        base_url="https://api.test/v1",
        model="test-model",
        memory_enabled=False,
        **config_kwargs,
    )
    llm = LLMClient(config, httpx.AsyncClient(transport=httpx.MockTransport(api)))
    registry = ToolRegistry()
    for tool in tools or []:
        registry.register(tool)
    return ChatEngine(
        config, storage, llm, registry, workspace=workspace, approve=approve
    )


async def collect(agen) -> list:
    return [event async for event in agen]


# ----------------------------------------------------------------------
# plain streaming
# ----------------------------------------------------------------------
async def test_send_streams_and_persists(storage, workspace: Path) -> None:
    api = ScriptedAPI(sse_text("Hello", " world"))
    engine = build_engine(api, storage, workspace)
    convo = await storage.create_conversation()

    events = await collect(engine.send(convo.id, "hi"))

    text = "".join(e.text for e in events if isinstance(e, TextDelta))
    assert text == "Hello world"
    assert any(isinstance(e, TurnFinished) for e in events)

    # Both sides of the exchange must be on disk.
    messages = await storage.get_messages(convo.id)
    assert [m.role for m in messages] == ["user", "assistant"]
    assert messages[1].content == "Hello world"


async def test_system_prompt_is_sent_first(storage, workspace: Path) -> None:
    api = ScriptedAPI(sse_text("ok"))
    engine = build_engine(api, storage, workspace)
    convo = await storage.create_conversation()

    await collect(engine.send(convo.id, "hi"))

    messages = api.requests[0]["messages"]
    assert messages[0]["role"] == "system"
    assert messages[-1]["content"] == "hi"


async def test_history_is_replayed_on_second_turn(storage, workspace: Path) -> None:
    api = ScriptedAPI(sse_text("first"), sse_text("second"))
    engine = build_engine(api, storage, workspace)
    convo = await storage.create_conversation()

    await collect(engine.send(convo.id, "one"))
    await collect(engine.send(convo.id, "two"))

    roles = [m["role"] for m in api.requests[1]["messages"]]
    assert roles.count("user") == 2
    assert "assistant" in roles


async def test_conversation_title_derived_from_first_message(storage, workspace: Path) -> None:
    api = ScriptedAPI(sse_text("ok"))
    engine = build_engine(api, storage, workspace)
    convo = await storage.create_conversation()

    await collect(engine.send(convo.id, "Explain SQLite WAL mode please"))

    refreshed = await storage.get_conversation(convo.id)
    assert refreshed.title and refreshed.title != "New chat"


# ----------------------------------------------------------------------
# tool calling
# ----------------------------------------------------------------------
async def test_tool_call_round_trip(storage, workspace: Path) -> None:
    """Model asks for a tool, engine runs it and feeds the result back."""
    api = ScriptedAPI(
        sse_tool_call("echo", '{"text": "hi"}'),
        sse_text("The tool said hi."),
    )
    engine = build_engine(api, storage, workspace, tools=[EchoTool()])
    convo = await storage.create_conversation()

    events = await collect(engine.send(convo.id, "use echo"))

    started = [e for e in events if isinstance(e, ToolCallStarted)]
    finished = [e for e in events if isinstance(e, ToolCallFinished)]
    assert started[0].call.name == "echo"
    assert finished[0].ok
    assert "echo: hi" in finished[0].output

    # Second request must include the tool result as a `tool` message.
    roles = [m["role"] for m in api.requests[1]["messages"]]
    assert "tool" in roles

    text = "".join(e.text for e in events if isinstance(e, TextDelta))
    assert "The tool said hi." in text


async def test_tool_schemas_are_advertised(storage, workspace: Path) -> None:
    api = ScriptedAPI(sse_text("ok"))
    engine = build_engine(api, storage, workspace, tools=[EchoTool()])
    convo = await storage.create_conversation()

    await collect(engine.send(convo.id, "hi"))

    names = [t["function"]["name"] for t in api.requests[0]["tools"]]
    assert "echo" in names


async def test_tools_not_sent_when_disabled(storage, workspace: Path) -> None:
    api = ScriptedAPI(sse_text("ok"))
    engine = build_engine(api, storage, workspace, tools=[EchoTool()], tools_enabled=False)
    convo = await storage.create_conversation()

    await collect(engine.send(convo.id, "hi"))
    assert "tools" not in api.requests[0]


async def test_unknown_tool_reports_failure(storage, workspace: Path) -> None:
    api = ScriptedAPI(sse_tool_call("no_such_tool", "{}"), sse_text("recovered"))
    engine = build_engine(api, storage, workspace, tools=[EchoTool()])
    convo = await storage.create_conversation()

    events = await collect(engine.send(convo.id, "go"))

    finished = [e for e in events if isinstance(e, ToolCallFinished)]
    assert finished and not finished[0].ok


async def test_tool_exception_is_contained(storage, workspace: Path) -> None:
    """A crashing tool becomes a failed result, not a crashed turn."""
    api = ScriptedAPI(sse_tool_call("boom", "{}"), sse_text("handled"))
    engine = build_engine(api, storage, workspace, tools=[BoomTool()])
    convo = await storage.create_conversation()

    events = await collect(engine.send(convo.id, "go"))

    finished = [e for e in events if isinstance(e, ToolCallFinished)]
    assert finished and not finished[0].ok
    assert any(isinstance(e, TurnFinished) for e in events)


async def test_malformed_tool_arguments_are_handled(storage, workspace: Path) -> None:
    api = ScriptedAPI(sse_tool_call("echo", "{not valid json"), sse_text("ok"))
    engine = build_engine(api, storage, workspace, tools=[EchoTool()])
    convo = await storage.create_conversation()

    events = await collect(engine.send(convo.id, "go"))
    assert any(isinstance(e, (ToolCallFinished, EngineError)) for e in events)


# ----------------------------------------------------------------------
# approval
# ----------------------------------------------------------------------
async def test_approval_granted_runs_tool(storage, workspace: Path) -> None:
    tool = DangerTool()
    api = ScriptedAPI(sse_tool_call("danger", "{}"), sse_text("done"))

    async def approve(t, call) -> bool:
        return True

    engine = build_engine(api, storage, workspace, tools=[tool], approve=approve)
    convo = await storage.create_conversation()

    await collect(engine.send(convo.id, "go"))
    assert tool.ran is True


async def test_approval_denied_blocks_tool(storage, workspace: Path) -> None:
    tool = DangerTool()
    api = ScriptedAPI(sse_tool_call("danger", "{}"), sse_text("fine, skipping"))

    async def deny(t, call) -> bool:
        return False

    engine = build_engine(api, storage, workspace, tools=[tool], approve=deny)
    convo = await storage.create_conversation()

    events = await collect(engine.send(convo.id, "go"))

    assert tool.ran is False
    assert any(isinstance(e, ToolCallDenied) for e in events)


async def test_auto_approve_skips_the_prompt(storage, workspace: Path) -> None:
    tool = DangerTool()
    api = ScriptedAPI(sse_tool_call("danger", "{}"), sse_text("done"))

    async def refuse(t, call) -> bool:  # pragma: no cover
        raise AssertionError("must not ask when auto_approve_tools is on")

    engine = build_engine(
        api, storage, workspace, tools=[tool], approve=refuse, auto_approve_tools=True
    )
    convo = await storage.create_conversation()

    await collect(engine.send(convo.id, "go"))
    assert tool.ran is True


async def test_missing_approval_callback_denies_by_default(storage, workspace: Path) -> None:
    """No UI to ask: a tool needing approval must not run silently."""
    tool = DangerTool()
    api = ScriptedAPI(sse_tool_call("danger", "{}"), sse_text("ok"))
    engine = build_engine(api, storage, workspace, tools=[tool], approve=None)
    convo = await storage.create_conversation()

    await collect(engine.send(convo.id, "go"))
    assert tool.ran is False


async def test_session_approval_is_remembered(storage, workspace: Path) -> None:
    tool = DangerTool()
    api = ScriptedAPI(sse_tool_call("danger", "{}"), sse_text("done"))

    async def refuse(t, call) -> bool:  # pragma: no cover
        raise AssertionError("should not ask after approve_for_session")

    engine = build_engine(api, storage, workspace, tools=[tool], approve=refuse)
    engine.approve_for_session("danger")
    convo = await storage.create_conversation()

    await collect(engine.send(convo.id, "go"))
    assert tool.ran is True


# ----------------------------------------------------------------------
# limits and errors
# ----------------------------------------------------------------------
async def test_tool_iteration_cap_is_enforced(storage, workspace: Path) -> None:
    """A model stuck in a tool loop must be stopped."""
    api = ScriptedAPI(*[sse_tool_call("echo", '{"text": "again"}') for _ in range(10)])
    engine = build_engine(
        api, storage, workspace, tools=[EchoTool()], max_tool_iterations=3
    )
    convo = await storage.create_conversation()

    events = await collect(engine.send(convo.id, "loop"))

    assert len([e for e in events if isinstance(e, ToolCallStarted)]) <= 3
    assert any(isinstance(e, (TurnFinished, EngineError)) for e in events)


async def test_api_error_becomes_engine_error(storage, workspace: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "bad key"}})

    config = Config(api_key="k", base_url="https://api.test/v1", memory_enabled=False)
    llm = LLMClient(config, httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    engine = ChatEngine(config, storage, llm, ToolRegistry(), workspace=workspace)
    convo = await storage.create_conversation()

    events = await collect(engine.send(convo.id, "hi"))

    errors = [e for e in events if isinstance(e, EngineError)]
    assert errors
    # The user message is still saved so it isn't lost on failure.
    assert [m.role for m in await storage.get_messages(convo.id)] == ["user"]


async def test_regenerate_drops_previous_answer(storage, workspace: Path) -> None:
    api = ScriptedAPI(sse_text("first answer"), sse_text("second answer"))
    engine = build_engine(api, storage, workspace)
    convo = await storage.create_conversation()

    await collect(engine.send(convo.id, "question"))
    events = await collect(engine.regenerate(convo.id))

    text = "".join(e.text for e in events if isinstance(e, TextDelta))
    assert text == "second answer"

    messages = await storage.get_messages(convo.id)
    assert [m.role for m in messages] == ["user", "assistant"]
    assert messages[1].content == "second answer"


async def test_message_completed_event_carries_final_message(storage, workspace: Path) -> None:
    api = ScriptedAPI(sse_text("all", " done"))
    engine = build_engine(api, storage, workspace)
    convo = await storage.create_conversation()

    events = await collect(engine.send(convo.id, "hi"))
    completed = [e for e in events if isinstance(e, MessageCompleted)]
    assert completed
    assert completed[-1].message.content == "all done"


def _anthropic_sse(*events: dict) -> bytes:
    lines = []
    for event in events:
        etype = event.get("type", "")
        if etype:
            lines.append(f"event: {etype}\n")
        lines.append(f"data: {json.dumps(event)}\n\n")
    return "".join(lines).encode()


async def test_engine_full_turn_with_anthropic(storage, workspace: Path) -> None:
    """End-to-end engine loop with native Anthropic provider and tool calling."""
    # Turn 1: Claude decides to call the echo tool
    body_turn1 = _anthropic_sse(
        {"type": "message_start", "message": {"id": "m1", "usage": {"input_tokens": 15}}},
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "tool_use", "id": "call_echo_1", "name": "echo", "input": {}},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "input_json_delta", "partial_json": '{"text": "world"}'},
        },
        {"type": "content_block_stop", "index": 0},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "tool_use"},
            "usage": {"output_tokens": 10},
        },
        {"type": "message_stop"},
    )

    # Turn 2: Claude answers with the tool's result
    body_turn2 = _anthropic_sse(
        {"type": "message_start", "message": {"id": "m2", "usage": {"input_tokens": 25}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": "Echoed: world"},
        },
        {"type": "content_block_stop", "index": 0},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn"},
            "usage": {"output_tokens": 5},
        },
        {"type": "message_stop"},
    )

    responses = [body_turn1, body_turn2]
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        body = responses.pop(0) if responses else b""
        return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

    config = Config(
        provider="anthropic",
        api_key="sk-ant-test",
        base_url="https://api.anthropic.com/v1/",
        model="claude-haiku-5-5",
    )
    registry = ToolRegistry()
    registry.register(EchoTool())

    llm = LLMClient(config, httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    engine = ChatEngine(config, storage, llm, registry, workspace=workspace)

    convo = await storage.create_conversation()
    events = await collect(engine.send(convo.id, "echo world"))

    text = "".join(e.text for e in events if isinstance(e, TextDelta))
    assert text == "Echoed: world"

    # Verify tool was invoked
    started = [e for e in events if isinstance(e, ToolCallStarted)]
    assert len(started) == 1
    assert started[0].call.name == "echo"

    finished = [e for e in events if isinstance(e, TurnFinished)]
    assert finished

    # Verify storage has user, assistant (tool_calls), tool, assistant (final)
    messages = await storage.get_messages(convo.id)
    assert [m.role for m in messages] == ["user", "assistant", "tool", "assistant"]
    assert messages[1].tool_calls[0].name == "echo"
    assert messages[2].content == "echo: world"
    assert messages[3].content == "Echoed: world"

    # Verify requests sent to Anthropic
    assert len(requests) == 2
    # Request 2 includes the tool_result block in user turn
    req2_turns = requests[1]["messages"]
    assert req2_turns[-1]["role"] == "user"
    assert req2_turns[-1]["content"][0]["type"] == "tool_result"
    assert req2_turns[-1]["content"][0]["content"] == "echo: world"

