
"""The agent loop.

One "turn" is:

1. Persist the user message.
2. Build the request: system prompt (+ memory block) + trimmed history.
3. Stream the assistant response, yielding :class:`TextDelta` events.
4. If the model requested tool calls: ask for approval, run them, persist the
   results as ``tool`` messages, then go back to step 3 so the model can use
   the output.
5. Stop when the model returns text with no tool calls, or after
   ``max_tool_iterations`` rounds (a hard guard against infinite loops).

The engine is an async generator of :mod:`pyassist.core.events`, so the UI just
renders whatever arrives and the whole loop is testable with a fake client.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable

from ..config import Config
from ..tools.base import Tool, ToolContext, ToolRegistry, ToolResult
from . import memory as memory_mod
from .events import (
    EngineError,
    EngineEvent,
    MessageCompleted,
    ReasoningDelta,
    TextDelta,
    ToolCallDenied,
    ToolCallFinished,
    ToolCallStarted,
    TurnFinished,
    UsageReported,
)
from .llm import LLMClient, LLMError
from .models import Attachment, Conversation, Message, ToolCall, Usage
from .storage import Storage

log = logging.getLogger(__name__)

#: ``await approve(tool, call) -> bool``. Returning False denies the call.
ApprovalCallback = Callable[[Tool, ToolCall], Awaitable[bool]]
#: ``await notify("status text")``
NotifyCallback = Callable[[str], Awaitable[None]]


class _ToolCallAccumulator:
    """Reassembles streamed tool calls.

    Providers send tool calls in fragments keyed by ``index``: the first chunk
    carries ``id``/``name``, later chunks append to ``arguments``. This buffers
    them until the stream ends.
    """

    def __init__(self) -> None:
        self._calls: dict[int, dict[str, str]] = {}

    def add(self, deltas: list[dict[str, Any]]) -> None:
        for delta in deltas:
            if not isinstance(delta, dict):
                continue
            index = int(delta.get("index") or 0)
            slot = self._calls.setdefault(index, {"id": "", "name": "", "arguments": ""})

            if call_id := delta.get("id"):
                slot["id"] = str(call_id)

            function = delta.get("function") or {}
            if isinstance(function, dict):
                if name := function.get("name"):
                    slot["name"] = str(name)
                arguments = function.get("arguments")
                if arguments:
                    slot["arguments"] += str(arguments)

    def finish(self) -> list[ToolCall]:
        calls: list[ToolCall] = []
        for index in sorted(self._calls):
            slot = self._calls[index]
            if not slot["name"]:
                continue  # incomplete fragment; nothing callable
            calls.append(
                ToolCall(
                    id=slot["id"] or f"call_{index}",
                    name=slot["name"],
                    arguments=slot["arguments"] or "{}",
                )
            )
        return calls

    def __bool__(self) -> bool:
        return bool(self._calls)


@dataclass(slots=True)
class _StreamOutcome:
    """Everything gathered from one streaming pass."""

    text: str = ""
    reasoning: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    finish_reason: str = ""


class ChatEngine:
    """Orchestrates conversation, streaming and tool execution."""

    def __init__(
        self,
        config: Config,
        storage: Storage,
        llm: LLMClient,
        tools: ToolRegistry,
        *,
        workspace: Path | None = None,
        approve: ApprovalCallback | None = None,
        notify: NotifyCallback | None = None,
    ) -> None:
        self.config = config
        self.storage = storage
        self.llm = llm
        self.tools = tools
        self.workspace = workspace or Path.cwd()
        self._approve = approve
        self._notify = notify
        #: Tools the user approved "for this session".
        self._session_approved: set[str] = set()

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------
    def approve_for_session(self, tool_name: str) -> None:
        self._session_approved.add(tool_name)

    async def send(
        self,
        conversation_id: str,
        text: str,
        *,
        attachments: list[Attachment] | None = None,
    ) -> AsyncIterator[EngineEvent]:
        """Run a full turn, yielding events as they happen."""
        user_message = Message(
            role="user",
            content=text,
            conversation_id=conversation_id,
            attachments=attachments or [],
        )
        await self.storage.add_message(user_message)
        await self._autotitle(conversation_id, text)

        async for event in self._run_turn(conversation_id, user_message):
            yield event

    async def _autotitle(self, conversation_id: str, text: str) -> None:
        """Name a still-untitled conversation after its first message.

        Done here rather than in the UI so headless runs (``-p``) and any other
        frontend get the same behaviour.
        """
        conversation = await self.storage.get_conversation(conversation_id)
        if conversation is None or conversation.title != "New chat":
            return
        title = Conversation.title_from_text(text)
        if title and title != "New chat":
            await self.storage.rename_conversation(conversation_id, title)

    async def regenerate(self, conversation_id: str) -> AsyncIterator[EngineEvent]:
        """Drop the trailing assistant turn and answer the last user message again."""
        history = await self.storage.get_messages(conversation_id)
        last_user = next((m for m in reversed(history) if m.role == "user"), None)
        if last_user is None:
            yield EngineError("Nothing to regenerate yet.")
            return

        await self.storage.delete_messages_after(conversation_id, last_user.created_at)
        async for event in self._run_turn(conversation_id, last_user):
            yield event

    # ------------------------------------------------------------------
    # the loop
    # ------------------------------------------------------------------
    async def _run_turn(
        self, conversation_id: str, trigger: Message
    ) -> AsyncIterator[EngineEvent]:
        total_usage = Usage()

        for _ in range(max(1, self.config.max_tool_iterations)):
            history = await self.storage.get_messages(conversation_id)
            payload = await self._build_messages(history, trigger.content)
            schemas = self.tools.schemas(self.config)

            # `outcome` is filled in as the stream is consumed; deltas are
            # yielded to the UI the instant they arrive.
            outcome = _StreamOutcome()
            try:
                async for event in self._consume_stream(payload, schemas, outcome):
                    yield event
            except LLMError as exc:
                yield EngineError(exc.message, detail=exc.detail)
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - surface, don't crash the UI
                log.exception("unexpected engine failure")
                yield EngineError("Unexpected error while contacting the model.", detail=str(exc))
                return

            total_usage = _merge_usage(total_usage, outcome.usage)
            if outcome.usage:
                yield UsageReported(outcome.usage)

            assistant = Message(
                role="assistant",
                content=outcome.text,
                conversation_id=conversation_id,
                tool_calls=outcome.tool_calls,
                reasoning=outcome.reasoning,
                metadata={"model": self.config.model, "finish_reason": outcome.finish_reason},
            )
            await self.storage.add_message(assistant)
            yield MessageCompleted(assistant)

            if not outcome.tool_calls:
                await self.storage.touch_conversation(conversation_id, model=self.config.model)
                yield TurnFinished(total_usage)
                return

            # Execute each requested tool, persisting a tool message per call so
            # the next request contains a valid tool_call -> tool pairing.
            for call in outcome.tool_calls:
                async for event in self._execute_call(call, conversation_id):
                    yield event

        yield EngineError(
            f"Stopped after {self.config.max_tool_iterations} tool rounds "
            "to avoid an infinite loop."
        )
        yield TurnFinished(total_usage)

    async def _consume_stream(
        self,
        payload: list[dict[str, Any]],
        schemas: list[dict[str, Any]],
        outcome: _StreamOutcome,
    ) -> AsyncIterator[EngineEvent]:
        """Drain one streaming response, yielding deltas as they arrive.

        The accumulated result is written into ``outcome`` (an out-parameter)
        because an async generator cannot also return a value.
        """
        accumulator = _ToolCallAccumulator()

        async for chunk in self.llm.stream_chat(payload, tools=schemas or None):
            if chunk.text_delta:
                outcome.text += chunk.text_delta
                yield TextDelta(chunk.text_delta)
            if chunk.reasoning_delta:
                outcome.reasoning += chunk.reasoning_delta
                yield ReasoningDelta(chunk.reasoning_delta)
            if chunk.tool_call_deltas:
                accumulator.add(chunk.tool_call_deltas)
            if chunk.usage:
                outcome.usage = chunk.usage
            if chunk.finish_reason:
                outcome.finish_reason = chunk.finish_reason

        outcome.tool_calls = accumulator.finish()

    async def _execute_call(
        self, call: ToolCall, conversation_id: str
    ) -> AsyncIterator[EngineEvent]:
        """Approve, run and persist a single tool call."""
        tool = self.tools.get(call.name)

        if tool is None:
            message = f"Unknown tool {call.name!r}."
            await self._persist_tool_result(call, conversation_id, message)
            yield ToolCallFinished(call, ok=False, output=message)
            return

        if not tool.is_available(self.config):
            message = (
                f"Tool {call.name!r} is disabled in settings. "
                "Tell the user to enable it if they want this action."
            )
            await self._persist_tool_result(call, conversation_id, message)
            yield ToolCallFinished(call, ok=False, output=message)
            return

        if not await self._is_approved(tool, call):
            message = "The user denied permission to run this tool."
            await self._persist_tool_result(call, conversation_id, message)
            yield ToolCallDenied(call)
            return

        yield ToolCallStarted(call)
        started = time.perf_counter()

        ctx = ToolContext(
            config=self.config,
            storage=self.storage,
            workspace=self.workspace,
            conversation_id=conversation_id,
            notify=self._notify,
        )

        try:
            result = await tool.run(call.parsed_arguments(), ctx)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a bad tool must not kill the turn
            log.exception("tool %s raised", call.name)
            result = ToolResult.error(f"{type(exc).__name__}: {exc}")

        duration = time.perf_counter() - started
        await self._persist_tool_result(call, conversation_id, result.content)
        yield ToolCallFinished(
            call, ok=result.ok, output=result.summary(), duration=duration
        )

    async def _is_approved(self, tool: Tool, call: ToolCall) -> bool:
        if not tool.requires_approval:
            return True
        if self.config.auto_approve_tools:
            return True
        if tool.name in self._session_approved:
            return True
        if self._approve is None:
            # No UI to ask: fail closed rather than run something destructive.
            return False
        return await self._approve(tool, call)

    async def _persist_tool_result(
        self, call: ToolCall, conversation_id: str, content: str
    ) -> None:
        await self.storage.add_message(
            Message(
                role="tool",
                content=content,
                conversation_id=conversation_id,
                tool_call_id=call.id,
                tool_name=call.name,
            )
        )

    # ------------------------------------------------------------------
    # request assembly
    # ------------------------------------------------------------------
    async def _build_messages(
        self, history: list[Message], query: str
    ) -> list[dict[str, Any]]:
        """System prompt (+ memory) followed by trimmed history."""
        system = self.config.system_prompt.strip()

        if self.config.memory_enabled:
            block = await memory_mod.build_context_block(
                self.storage, query, self.config.memory_context_limit
            )
            if block.text:
                system = f"{system}\n\n## Remembered context\n{block.text}"

        payload: list[dict[str, Any]] = []
        if system:
            payload.append({"role": "system", "content": system})

        payload.extend(m.to_api() for m in _trim_history(history, self.config.history_char_budget))
        return payload


def _trim_history(messages: list[Message], budget: int) -> list[Message]:
    """Keep the newest messages that fit in ``budget`` characters.

    Walking backwards keeps the most recent context, which matters most. Tool
    messages are kept together with the assistant message that requested them:
    dropping an assistant tool-call message while keeping its ``tool`` reply
    makes the request invalid, so we discard orphaned tool messages.
    """
    if budget <= 0:
        return messages

    kept: list[Message] = []
    used = 0
    for message in reversed(messages):
        cost = message.char_cost()
        if used + cost > budget and kept:
            break
        kept.append(message)
        used += cost

    kept.reverse()

    # Drop leading tool messages whose originating assistant turn was trimmed.
    valid_ids = {tc.id for m in kept for tc in m.tool_calls}
    return [m for m in kept if m.role != "tool" or (m.tool_call_id in valid_ids)]


def _merge_usage(total: Usage, delta: Usage) -> Usage:
    if not delta:
        return total
    return Usage(
        prompt_tokens=total.prompt_tokens + delta.prompt_tokens,
        completion_tokens=total.completion_tokens + delta.completion_tokens,
        total_tokens=total.total_tokens + delta.total_tokens,
    )
