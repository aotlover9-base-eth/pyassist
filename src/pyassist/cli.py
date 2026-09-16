"""Command-line entry point.

Besides launching the TUI, this exposes a headless mode (``-p/--prompt``) that
runs one turn and streams the answer to stdout. That makes pyassist pipeable
and scriptable, and it exercises the same engine the UI uses.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from . import __version__, paths
from .config import Config, mask_secret
from .core.bootstrap import create_app_context, setup_logging
from .core.events import (
    EngineError,
    TextDelta,
    ToolCallDenied,
    ToolCallFinished,
    ToolCallStarted,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pyassist",
        description="A beautiful terminal AI assistant.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  pyassist                          launch the TUI\n"
            "  pyassist -p 'explain WAL mode'    headless, prints to stdout\n"
            "  pyassist --set api_key=sk-...     save a setting\n"
            "  pyassist --list-tools             show available tools\n"
        ),
    )
    parser.add_argument("--version", action="version", version=f"pyassist {__version__}")
    parser.add_argument(
        "-p", "--prompt", metavar="TEXT", help="run one prompt headlessly and exit"
    )
    parser.add_argument("-m", "--model", help="override the model for this run")
    parser.add_argument(
        "-w", "--workspace", metavar="DIR", help="workspace root for file tools (default: cwd)"
    )
    parser.add_argument(
        "-y",
        "--auto-approve",
        action="store_true",
        help="automatically approve tool executions without prompting",
    )
    parser.add_argument(
        "--set",
        dest="settings",
        action="append",
        metavar="KEY=VALUE",
        help="persist a setting (repeatable)",
    )
    parser.add_argument("--config", action="store_true", help="print current settings and exit")
    parser.add_argument("--list-tools", action="store_true", help="list tools and exit")
    parser.add_argument("--list-plugins", action="store_true", help="list plugins and exit")
    parser.add_argument("--plugin-dir", action="store_true", help="print the plugin directory")
    parser.add_argument(
        "--write-example-plugin", action="store_true", help="write an example plugin and exit"
    )
    parser.add_argument("--no-plugins", action="store_true", help="skip loading plugins")
    parser.add_argument("--debug", action="store_true", help="verbose logging to the log file")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(logging.DEBUG if args.debug else logging.WARNING)

    # --- commands that don't need the full app -------------------------
    if args.settings:
        return _apply_settings(args.settings)

    if args.config:
        return _print_config()

    if args.plugin_dir:
        print(paths.plugin_dir())
        return 0

    if args.write_example_plugin:
        from .plugins.loader import write_example_plugin

        target = write_example_plugin()
        print(f"Wrote {target}")
        print("Restart pyassist and try /hello, or ask the model to count words.")
        return 0

    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        return 130


async def _run(args: argparse.Namespace) -> int:
    config = Config.load()
    if args.model:
        config.model = args.model
    if args.auto_approve:
        config.auto_approve_tools = True

    workspace = Path(args.workspace).expanduser() if args.workspace else Path.cwd()
    if not workspace.is_dir():
        print(f"error: workspace {workspace} is not a directory", file=sys.stderr)
        return 2

    context = await create_app_context(
        config=config,
        workspace=workspace,
        load_plugins_enabled=not args.no_plugins,
    )

    try:
        if args.list_tools:
            return _print_tools(context)
        if args.list_plugins:
            return _print_plugins(context)
        if args.prompt:
            return await _headless(context, args.prompt)

        # The TUI closes the context itself on unmount.
        from .ui.app import PyAssistApp

        app = PyAssistApp(context)
        await app.run_async()
        return 0
    finally:
        await context.aclose()


async def _headless(context, prompt: str) -> int:  # noqa: ANN001
    """Run a single turn, streaming plain text to stdout."""
    if not context.config.is_configured:
        print(
            "error: no API key configured. Run: pyassist --set api_key=sk-...",
            file=sys.stderr,
        )
        return 2

    conversation = await context.storage.create_conversation(model=context.config.model)
    failed = False

    async for event in context.engine.send(conversation.id, prompt):
        if isinstance(event, TextDelta):
            sys.stdout.write(event.text)
            sys.stdout.flush()
        elif isinstance(event, ToolCallStarted):
            print(f"\n[{event.call.name}…]", file=sys.stderr)
        elif isinstance(event, ToolCallFinished):
            status = "ok" if event.ok else "failed"
            print(f"[{event.call.name}: {status}]", file=sys.stderr)
        elif isinstance(event, ToolCallDenied):
            print(f"[{event.call.name}: denied ({event.reason})]", file=sys.stderr)
        elif isinstance(event, EngineError):
            print(f"\nerror: {event.message}", file=sys.stderr)
            if event.detail:
                print(event.detail, file=sys.stderr)
            failed = True

    print()
    return 1 if failed else 0


def _apply_settings(pairs: list[str]) -> int:
    config = Config.load()
    changes: dict[str, str] = {}

    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep:
            print(f"error: expected KEY=VALUE, got {pair!r}", file=sys.stderr)
            return 2
        key = key.strip()
        if key not in Config._field_names():
            print(f"error: unknown setting {key!r}", file=sys.stderr)
            print(f"known: {', '.join(sorted(Config._field_names()))}", file=sys.stderr)
            return 2
        changes[key] = value.strip()

    config.update(**changes)
    config.save()

    for key in changes:
        shown = mask_secret(getattr(config, key)) if "key" in key else getattr(config, key)
        print(f"{key} = {shown}")
    print(f"\nSaved to {paths.config_file()}")
    return 0


def _print_config() -> int:
    config = Config.load()
    print(f"config file: {paths.config_file()}")
    print(f"database:    {paths.database_file()}")
    print(f"plugins:     {paths.plugin_dir()}\n")

    for key, value in config.redacted().items():
        if key == "system_prompt":
            value = f"{str(value)[:60]}…" if len(str(value)) > 60 else value
        print(f"{key:24} {value}")
    return 0


def _print_tools(context) -> int:  # noqa: ANN001
    tools = context.tools.available(context.config)
    print(f"{len(tools)} tools available:\n")
    for tool in tools:
        flag = " (asks first)" if tool.requires_approval else ""
        print(f"  {tool.name:16} {tool.description}{flag}")

    hidden = [t for t in context.tools.all() if t not in tools]
    if hidden:
        print("\ndisabled in settings:")
        for tool in hidden:
            print(f"  {tool.name}")
    return 0


def _print_plugins(context) -> int:  # noqa: ANN001
    plugins = context.plugins.plugins
    if not plugins:
        print("No plugins loaded.")
        print(f"Drop a .py file in {paths.plugin_dir()}")
        print("or run: pyassist --write-example-plugin")
        return 0

    for info in plugins:
        status = "ok" if info.ok else f"ERROR: {info.error}"
        print(f"{info.name} v{info.version} [{status}]")
        if info.description:
            print(f"  {info.description}")
        if info.tools:
            print(f"  tools: {', '.join(info.tools)}")
        if info.commands:
            print(f"  commands: {', '.join(info.commands)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
