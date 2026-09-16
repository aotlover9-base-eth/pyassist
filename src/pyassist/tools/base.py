"""Tool protocol and registry.

A *tool* is a callable the model can invoke through OpenAI function calling.
Every tool declares a JSON Schema for its parameters; the registry converts
that into the ``tools[]`` array sent with each request.

Safety model
------------
Tools declare ``requires_approval``. The engine asks the UI for confirmation
before running those, unless ``auto_approve_tools`` is on. Tools also declare
``is_available(config)`` so dangerous ones (shell, network) stay hidden until
explicitly enabled in Settings.
"""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..config import Config
    from ..core.storage import Storage

log = logging.getLogger(__name__)


@dataclass(slots=True)
class ToolResult:
    """What a tool returns.

    ``content`` is fed back to the model as the tool message, so it should be
    compact and informative. ``display`` optionally overrides what the UI shows.
    """

    content: str
    ok: bool = True
    display: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def error(cls, message: str) -> ToolResult:
        return cls(content=f"Error: {message}", ok=False)

    @classmethod
    def json(cls, payload: Any, **kwargs: Any) -> ToolResult:
        return cls(content=json.dumps(payload, indent=2, default=str), **kwargs)

    def summary(self, limit: int = 400) -> str:
        """Short, single-paragraph form for the transcript."""
        text = self.display or self.content
        text = text.strip()
        if len(text) <= limit:
            return text
        return text[:limit].rstrip() + " …"


@dataclass(slots=True)
class ToolContext:
    """Services handed to a tool at call time."""

    config: Config
    storage: Storage
    workspace: Path
    conversation_id: str = ""
    #: Emit a transient status line in the UI (``await notify("...")``).
    notify: Callable[[str], Awaitable[None]] | None = None

    async def say(self, message: str) -> None:
        if self.notify is not None:
            await self.notify(message)


class Tool(ABC):
    """Base class for all tools."""

    #: Function name exposed to the model. Must match ``^[a-zA-Z0-9_-]{1,64}$``.
    name: str = ""
    #: Shown to the model; this is the primary signal for *when* to call it.
    description: str = ""
    #: JSON Schema (draft-07 subset) describing the arguments object.
    parameters: dict[str, Any] = {"type": "object", "properties": {}}
    #: Ask the user before running.
    requires_approval: bool = False
    #: Grouping label used by ``/tools``.
    category: str = "general"

    @abstractmethod
    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        """Execute the tool. Must not raise; return ``ToolResult.error`` instead."""

    def is_available(self, config: Config) -> bool:  # noqa: ARG002
        """Whether this tool should be offered to the model at all."""
        return True

    def to_schema(self) -> dict[str, Any]:
        """OpenAI ``tools[]`` entry."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Tool {self.name}>"


class ToolRegistry:
    """Name-keyed collection of tools, filtered by config at request time."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool, *, replace: bool = False) -> None:
        """Add a tool. Raises :class:`ValueError` on a duplicate name."""
        if not tool.name:
            raise ValueError(f"{type(tool).__name__} must define a name")
        if tool.name in self._tools and not replace:
            raise ValueError(f"Tool {tool.name!r} is already registered")
        self._tools[tool.name] = tool
        log.debug("registered tool %s", tool.name)

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def all(self) -> list[Tool]:
        return sorted(self._tools.values(), key=lambda t: (t.category, t.name))

    def available(self, config: Config) -> list[Tool]:
        return [tool for tool in self.all() if tool.is_available(config)]

    def schemas(self, config: Config) -> list[dict[str, Any]]:
        """The ``tools[]`` payload, or ``[]`` when tools are disabled."""
        if not config.tools_enabled:
            return []
        return [tool.to_schema() for tool in self.available(config)]

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: object) -> bool:
        return name in self._tools


class FunctionTool(Tool):
    """Adapter that turns a plain async function into a :class:`Tool`.

    Lets plugin authors skip subclassing::

        @plugin_tool(name="ping", description="Ping a host", parameters={...})
        async def ping(args, ctx): ...
    """

    def __init__(
        self,
        func: Callable[[dict[str, Any], ToolContext], Awaitable[ToolResult | str]],
        *,
        name: str,
        description: str,
        parameters: dict[str, Any] | None = None,
        requires_approval: bool = False,
        category: str = "plugin",
    ) -> None:
        self._func = func
        self.name = name
        self.description = description
        self.parameters = parameters or {"type": "object", "properties": {}}
        self.requires_approval = requires_approval
        self.category = category

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        result = await self._func(args, ctx)
        if isinstance(result, ToolResult):
            return result
        return ToolResult(content=str(result))
