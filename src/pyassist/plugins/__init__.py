"""Plugin API and loader."""

from __future__ import annotations

from .api import PluginInfo, PluginRegistry, SlashCommand, tool
from .loader import load_plugins, write_example_plugin

__all__ = [
    "PluginInfo",
    "PluginRegistry",
    "SlashCommand",
    "load_plugins",
    "tool",
    "write_example_plugin",
]
