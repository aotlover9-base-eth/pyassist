"""The public API that plugin authors import.

A plugin is any Python module exposing a ``register(registry)`` function::

    from pyassist.plugins.api import PluginRegistry, tool, ToolResult

    def register(registry: PluginRegistry) -> None:
        registry.add_tool(my_tool)
        registry.add_command("/weather", weather_handler, "Show the forecast")

Plugins can contribute three things:

1. **Tools** the model can call.
2. **Slash commands** the user can type.
3. **Startup hooks** that run once the app is ready.

Keeping this module dependency-free (it only re-exports) means a plugin never
has to import from ``pyassist.core``, so internals can change freely.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from ..tools.base import FunctionTool, Tool, ToolContext, ToolRegistry, ToolResult

log = logging.getLogger(__name__)

#: Signature of a slash-command handler: ``async def(args: str, ctx) -> str``.
CommandHandler = Callable[[str, "ToolContext"], Awaitable[str]]
#: Signature of a startup hook.
StartupHook = Callable[["ToolContext"], Awaitable[None]]


@dataclass(slots=True)
class SlashCommand:
    """A user-typed command such as ``/weather London``."""

    name: str
    handler: CommandHandler
    description: str = ""
    plugin: str = ""


@dataclass(slots=True)
class PluginInfo:
    """Metadata about a loaded plugin, shown by ``/plugins``."""

    name: str
    version: str = "0.0.0"
    description: str = ""
    source: str = ""
    tools: list[str] = field(default_factory=list)
    commands: list[str] = field(default_factory=list)
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error


class PluginRegistry:
    """Handed to each plugin's ``register()`` function.

    It records which plugin contributed what, so ``/plugins`` can show
    attribution and a failing plugin can be isolated.
    """

    def __init__(self, tools: ToolRegistry) -> None:
        self._tools = tools
        self._commands: dict[str, SlashCommand] = {}
        self._hooks: list[tuple[str, StartupHook]] = []
        self._current_plugin = ""
        self._contributions: dict[str, PluginInfo] = {}

    # -- used by the loader ------------------------------------------------
    def _begin(self, info: PluginInfo) -> None:
        self._current_plugin = info.name
        self._contributions[info.name] = info

    def _info(self) -> PluginInfo | None:
        return self._contributions.get(self._current_plugin)

    # -- plugin-facing API -------------------------------------------------
    def add_tool(self, tool: Tool, *, replace: bool = False) -> None:
        """Register a :class:`Tool` subclass instance."""
        self._tools.register(tool, replace=replace)
        if info := self._info():
            info.tools.append(tool.name)

    def add_function_tool(
        self,
        func: Callable[[dict[str, Any], ToolContext], Awaitable[ToolResult | str]],
        *,
        name: str,
        description: str,
        parameters: dict[str, Any] | None = None,
        requires_approval: bool = False,
    ) -> None:
        """Register a bare async function as a tool."""
        self.add_tool(
            FunctionTool(
                func,
                name=name,
                description=description,
                parameters=parameters,
                requires_approval=requires_approval,
                category=self._current_plugin or "plugin",
            )
        )

    def add_command(
        self, name: str, handler: CommandHandler, description: str = ""
    ) -> None:
        """Register a slash command. ``name`` may omit the leading slash."""
        key = name if name.startswith("/") else f"/{name}"
        self._commands[key] = SlashCommand(
            name=key,
            handler=handler,
            description=description,
            plugin=self._current_plugin,
        )
        if info := self._info():
            info.commands.append(key)

    def add_startup_hook(self, hook: StartupHook) -> None:
        """Run ``hook`` once, after the app has started."""
        self._hooks.append((self._current_plugin, hook))

    # -- consumed by the app ----------------------------------------------
    @property
    def commands(self) -> dict[str, SlashCommand]:
        return dict(self._commands)

    @property
    def hooks(self) -> list[tuple[str, StartupHook]]:
        return list(self._hooks)

    @property
    def plugins(self) -> list[PluginInfo]:
        return sorted(self._contributions.values(), key=lambda p: p.name)


def tool(
    *,
    name: str,
    description: str,
    parameters: dict[str, Any] | None = None,
    requires_approval: bool = False,
) -> Callable[..., FunctionTool]:
    """Decorator turning an async function into a :class:`FunctionTool`::

        @tool(name="ping", description="Ping a host")
        async def ping(args, ctx): ...
    """

    def decorator(
        func: Callable[[dict[str, Any], ToolContext], Awaitable[ToolResult | str]],
    ) -> FunctionTool:
        return FunctionTool(
            func,
            name=name,
            description=description,
            parameters=parameters,
            requires_approval=requires_approval,
        )

    return decorator


__all__ = [
    "CommandHandler",
    "PluginInfo",
    "PluginRegistry",
    "SlashCommand",
    "StartupHook",
    "Tool",
    "ToolContext",
    "ToolResult",
    "tool",
]
