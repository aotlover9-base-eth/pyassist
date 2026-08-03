"""Events emitted by :class:`pyassist.core.engine.ChatEngine`.

The engine is a pure async generator of these events; it knows nothing about
Textual. That keeps the agent loop testable in isolation and means the same
engine drives both the TUI and the headless ``-p/--print`` mode.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .models import Message, ToolCall, Usage


@dataclass(slots=True)
class TextDelta:
    """A chunk of assistant text."""

    text: str


@dataclass(slots=True)
class ReasoningDelta:
    """A chunk of reasoning/thinking text, when the provider exposes it."""

    text: str


@dataclass(slots=True)
class ToolCallStarted:
    call: ToolCall


@dataclass(slots=True)
class ToolCallFinished:
    call: ToolCall
    ok: bool
    output: str
    duration: float = 0.0


@dataclass(slots=True)
class ToolCallDenied:
    call: ToolCall
    reason: str = "denied by user"


@dataclass(slots=True)
class MessageCompleted:
    """An assistant message was finalised and persisted."""

    message: Message


@dataclass(slots=True)
class UsageReported:
    usage: Usage


@dataclass(slots=True)
class TurnFinished:
    """The whole turn is done: no more tool calls pending."""

    usage: Usage = field(default_factory=Usage)


@dataclass(slots=True)
class EngineError:
    """A recoverable failure; the UI shows this instead of a traceback."""

    message: str
    detail: str = ""
    payload: dict[str, Any] = field(default_factory=dict)


#: Union of everything the engine can yield.
EngineEvent = (
    TextDelta
    | ReasoningDelta
    | ToolCallStarted
    | ToolCallFinished
    | ToolCallDenied
    | MessageCompleted
    | UsageReported
    | TurnFinished
    | EngineError
)

__all__ = [
    "EngineError",
    "EngineEvent",
    "MessageCompleted",
    "ReasoningDelta",
    "TextDelta",
    "ToolCallDenied",
    "ToolCallFinished",
    "ToolCallStarted",
    "TurnFinished",
    "UsageReported",
]
