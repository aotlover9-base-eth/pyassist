"""Plugin discovery and loading.

Two discovery mechanisms:

1. **Entry points** - installed packages declaring::

       [project.entry-points."pyassist.plugins"]
       weather = "my_pkg.weather"

2. **User directory** - any ``*.py`` file in ``<config>/plugins/`` (override
   with ``PYASSIST_PLUGIN_DIR``). These are loaded by path, so users can drop
   in a script without packaging anything.

Isolation is the priority: a plugin that raises during import or ``register()``
is recorded with its error message and skipped. One broken plugin must never
prevent the app from starting.
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import sys
import traceback
from pathlib import Path
from types import ModuleType

from .. import paths
from ..tools.base import ToolRegistry
from .api import PluginInfo, PluginRegistry

log = logging.getLogger(__name__)

ENTRY_POINT_GROUP = "pyassist.plugins"


def load_plugins(
    tools: ToolRegistry,
    *,
    plugin_dir: Path | None = None,
    load_entry_points: bool = True,
) -> PluginRegistry:
    """Discover and register every available plugin."""
    registry = PluginRegistry(tools)

    if load_entry_points:
        _load_entry_points(registry)

    directory = plugin_dir or paths.plugin_dir()
    if directory.is_dir():
        _load_directory(registry, directory)

    return registry


def _load_entry_points(registry: PluginRegistry) -> None:
    try:
        from importlib.metadata import entry_points
    except ImportError:  # pragma: no cover - Python < 3.8
        return

    try:
        discovered = entry_points(group=ENTRY_POINT_GROUP)
    except TypeError:  # pragma: no cover - older API shape
        discovered = entry_points().get(ENTRY_POINT_GROUP, [])  # type: ignore[attr-defined]

    for entry in discovered:
        info = PluginInfo(name=entry.name, source=f"entry-point:{entry.value}")
        try:
            module = entry.load()
        except Exception as exc:  # noqa: BLE001 - never let a plugin break startup
            info.error = f"import failed: {exc}"
            log.warning("plugin %s failed to import: %s", entry.name, exc)
            registry._begin(info)
            continue
        _register_module(registry, module, info)


def _load_directory(registry: PluginRegistry, directory: Path) -> None:
    for path in sorted(directory.glob("*.py")):
        if path.name.startswith("_"):
            continue
        info = PluginInfo(name=path.stem, source=str(path))
        module = _import_from_path(path, info)
        if module is None:
            registry._begin(info)
            continue
        _register_module(registry, module, info)


def _import_from_path(path: Path, info: PluginInfo) -> ModuleType | None:
    """Import a standalone .py file under a namespaced module name."""
    module_name = f"pyassist_plugin_{path.stem}"
    try:
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            info.error = "could not create module spec"
            return None
        module = importlib.util.module_from_spec(spec)
        # Register before exec so dataclasses/pickle inside the plugin resolve.
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        return module
    except Exception as exc:  # noqa: BLE001
        sys.modules.pop(module_name, None)
        info.error = f"import failed: {exc}"
        log.warning("plugin %s failed to import:\n%s", path.name, traceback.format_exc())
        return None


def _register_module(registry: PluginRegistry, module: ModuleType, info: PluginInfo) -> None:
    """Call ``module.register(registry)``, capturing metadata and failures."""
    info.version = str(getattr(module, "__version__", "0.0.0"))
    info.description = (getattr(module, "__doc__", "") or "").strip().split("\n")[0]
    registry._begin(info)

    register = getattr(module, "register", None)
    if not callable(register):
        info.error = "missing a register(registry) function"
        log.warning("plugin %s has no register() function", info.name)
        return

    try:
        register(registry)
    except Exception as exc:  # noqa: BLE001
        info.error = f"register() failed: {exc}"
        log.warning("plugin %s register() failed:\n%s", info.name, traceback.format_exc())


EXAMPLE_PLUGIN = '''\
"""Example pyassist plugin - copy me and start hacking.

Drop this file in your plugin directory and restart pyassist; the tool and
command below become available immediately.
"""

from pyassist.plugins.api import PluginRegistry, ToolContext, ToolResult

__version__ = "1.0.0"


async def word_count(args: dict, ctx: ToolContext) -> ToolResult:
    text = str(args.get("text", ""))
    words = len(text.split())
    return ToolResult(
        content=f"{words} words, {len(text)} characters",
        display=f"Counted {words} words",
    )


async def hello_command(args: str, ctx: ToolContext) -> str:
    name = args.strip() or "world"
    return f"Hello, {name}! (from the example plugin)"


def register(registry: PluginRegistry) -> None:
    registry.add_function_tool(
        word_count,
        name="word_count",
        description="Count the words and characters in a piece of text.",
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string", "description": "Text to measure."}},
            "required": ["text"],
        },
    )
    registry.add_command("/hello", hello_command, "Say hello")
'''


def write_example_plugin(directory: Path | None = None) -> Path:
    """Write ``example_plugin.py`` into the plugin directory."""
    directory = directory or paths.plugin_dir()
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "example_plugin.py"
    target.write_text(EXAMPLE_PLUGIN, encoding="utf-8")
    return target
