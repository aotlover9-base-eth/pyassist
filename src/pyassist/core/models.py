"""Domain models.

These dataclasses mirror the OpenAI chat schema closely enough that converting
to wire format is trivial (:meth:`Message.to_api`), while staying friendly to
SQLite persistence (everything is either a scalar or JSON-encodable).
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

Role = Literal["system", "user", "assistant", "tool"]


def new_id() -> str:
    """Short, sortable-enough unique id (hex, 16 chars)."""
    return uuid.uuid4().hex[:16]


def now() -> float:
    return time.time()


@dataclass(slots=True)
class ToolCall:
    """A function call requested by the model."""

    id: str
    name: str
    arguments: str = "{}"  # raw JSON string, exactly as the model produced it

    def parsed_arguments(self) -> dict[str, Any]:
        """Parse arguments defensively - models do emit malformed JSON."""
        if not self.arguments.strip():
            return {}
        try:
            data = json.loads(self.arguments)
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}

    def to_api(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": "function",
            "function": {"name": self.name, "arguments": self.arguments or "{}"},
        }

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "arguments": self.arguments}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ToolCall:
        return cls(
            id=str(data.get("id") or new_id()),
            name=str(data.get("name") or ""),
            arguments=str(data.get("arguments") or "{}"),
        )


@dataclass(slots=True)
class Attachment:
    """A file the user attached to a message."""

    id: str = field(default_factory=new_id)
    path: str = ""
    name: str = ""
    mime_type: str = "text/plain"
    size_bytes: int = 0
    kind: Literal["text", "image"] = "text"
    #: Inlined text content, or a ``data:`` URL for images.
    content: str = ""
    truncated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "path": self.path,
            "name": self.name,
            "mime_type": self.mime_type,
            "size_bytes": self.size_bytes,
            "kind": self.kind,
            "content": self.content,
            "truncated": self.truncated,
        }


@dataclass(slots=True)
class Message:
    """One turn in a conversation."""

    role: Role
    content: str = ""
    id: str = field(default_factory=new_id)
    conversation_id: str = ""
    created_at: float = field(default_factory=now)

    # assistant -> requested tool calls
    tool_calls: list[ToolCall] = field(default_factory=list)
    # tool -> which call this responds to
    tool_call_id: str | None = None
    tool_name: str | None = None

    attachments: list[Attachment] = field(default_factory=list)
    #: Reasoning text from providers that expose it (DeepSeek R1 et al).
    reasoning: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------
    def to_api(self) -> dict[str, Any]:
        """Convert to an OpenAI ``messages[]`` entry."""
        if self.role == "tool":
            payload = {
                "role": "tool",
                "tool_call_id": self.tool_call_id or "",
                "content": self.content,
            }
            if self.tool_name:
                payload["name"] = self.tool_name
            return payload

        payload: dict[str, Any] = {"role": self.role}

        image_parts = [
            {"type": "image_url", "image_url": {"url": a.content}}
            for a in self.attachments
            if a.kind == "image" and a.content
        ]
        if image_parts:
            # Multimodal messages must use the content-parts form.
            parts: list[dict[str, Any]] = []
            if self.content:
                parts.append({"type": "text", "text": self.content})
            parts.extend(image_parts)
            payload["content"] = parts
        else:
            payload["content"] = self.content

        if self.tool_calls:
            payload["tool_calls"] = [tc.to_api() for tc in self.tool_calls]
            # Assistant tool-call turns may legitimately have empty content, but
            # some servers reject `null`, so keep it an empty string.
            payload.setdefault("content", "")
        return payload

    @property
    def display_content(self) -> str:
        """Content plus a compact attachment summary, for the UI."""
        if not self.attachments:
            return self.content
        names = ", ".join(a.name for a in self.attachments)
        suffix = f"\n\n*attached: {names}*"
        return f"{self.content}{suffix}" if self.content else suffix.strip()

    def char_cost(self) -> int:
        """Rough size used by the history trimmer."""
        total = len(self.content or "") + len(self.reasoning or "")
        for tc in self.tool_calls:
            total += len(tc.name or "") + len(tc.arguments or "")
        for att in self.attachments:
            total += len(att.content or "")
        return total


@dataclass(slots=True)
class Conversation:
    """A titled sequence of messages."""

    id: str = field(default_factory=new_id)
    title: str = "New chat"
    created_at: float = field(default_factory=now)
    updated_at: float = field(default_factory=now)
    model: str = ""
    message_count: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def title_from_text(text: str, *, limit: int = 48) -> str:
        """Derive a readable title from the first user message."""
        cleaned = " ".join(text.strip().split())
        if not cleaned:
            return "New chat"
        if len(cleaned) <= limit:
            return cleaned
        return cleaned[: limit - 1].rstrip() + "…"


@dataclass(slots=True)
class Memory:
    """A durable fact the assistant should remember across conversations."""

    key: str
    value: str
    id: str = field(default_factory=new_id)
    created_at: float = field(default_factory=now)
    updated_at: float = field(default_factory=now)
    source_conversation_id: str = ""


@dataclass(slots=True)
class Usage:
    """Token accounting reported by the server."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> Usage:
        data = data or {}
        prompt = int(data.get("prompt_tokens") or 0)
        completion = int(data.get("completion_tokens") or 0)
        total = int(data.get("total_tokens") or (prompt + completion))
        return cls(prompt_tokens=prompt, completion_tokens=completion, total_tokens=total)

    def __bool__(self) -> bool:
        return bool(self.prompt_tokens or self.completion_tokens or self.total_tokens)
