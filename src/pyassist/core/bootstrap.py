"""Dependency wiring.

A single place that builds the whole object graph (config → storage → LLM →
tools → plugins → engine). Both the TUI and the headless CLI use this, so they
can never drift apart, and tests can build a fully-wired context against a
temp directory in one call.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path

from .. import paths
from ..config import Config
from ..plugins.api import PluginRegistry
from ..plugins.loader import load_plugins
from ..tools.base import ToolRegistry
from ..tools.builtin import build_default_registry
from .engine import ApprovalCallback, ChatEngine, NotifyCallback
from .llm import LLMClient
from .storage import Storage

log = logging.getLogger(__name__)


@dataclass(slots=True)
class AppContext:
    """Everything the application needs, wired together."""

    config: Config
    storage: Storage
    llm: LLMClient
    tools: ToolRegistry
    plugins: PluginRegistry
    engine: ChatEngine
    workspace: Path
    warnings: list[str] = field(default_factory=list)

    async def aclose(self) -> None:
        await self.llm.aclose()
        await self.storage.close()

    def rebuild_llm(self) -> None:
        """Recreate the HTTP client after settings change.

        The API key and base URL live in the client's headers/URL, so a new
        client is required whenever they change.
        """
        old = self.llm
        self.llm = LLMClient(self.config)
        self.engine.llm = self.llm
        # The old client owns a connection pool; closing it is fire-and-forget.
        try:
            asyncio.get_running_loop().create_task(old.aclose())
        except RuntimeError:  # pragma: no cover - no loop (sync context)
            pass


async def create_app_context(
    *,
    config: Config | None = None,
    workspace: Path | None = None,
    db_path: Path | None = None,
    plugin_dir: Path | None = None,
    load_plugins_enabled: bool = True,
    approve: ApprovalCallback | None = None,
    notify: NotifyCallback | None = None,
) -> AppContext:
    """Build and connect every component."""
    paths.ensure_dirs()

    config = config or Config.load()
    workspace = (workspace or Path.cwd()).resolve()
    warnings: list[str] = []

    storage = Storage(db_path or paths.database_file())
    await storage.connect()

    tools = build_default_registry()

    plugins = PluginRegistry(tools)
    if load_plugins_enabled:
        plugins = load_plugins(tools, plugin_dir=plugin_dir)
        for info in plugins.plugins:
            if not info.ok:
                warnings.append(f"plugin '{info.name}' failed: {info.error}")

    llm = LLMClient(config)

    engine = ChatEngine(
        config,
        storage,
        llm,
        tools,
        workspace=workspace,
        approve=approve,
        notify=notify,
    )

    if not config.is_configured:
        warnings.append(
            "No API key configured. Press F2 to open Settings, or set OPENAI_API_KEY."
        )

    return AppContext(
        config=config,
        storage=storage,
        llm=llm,
        tools=tools,
        plugins=plugins,
        engine=engine,
        workspace=workspace,
        warnings=warnings,
    )


def setup_logging(level: int = logging.WARNING) -> None:
    """Log to a file, never to stdout - stdout belongs to the TUI."""
    paths.ensure_dirs()
    logging.basicConfig(
        level=level,
        filename=str(paths.log_file()),
        filemode="a",
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )
