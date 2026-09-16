"""Modal screens: settings, approval, search, file picker, help.

Each is a ``ModalScreen[T]`` that dismisses with a typed result, so callers can
simply ``await self._ask(SettingsScreen(cfg))`` and act on what comes back.
That keeps all the flow control in the app instead of scattering callbacks
across the UI.

Initial focus is declared with ``AUTO_FOCUS`` rather than a ``query_one(...)``
call in ``on_mount``: a screen receives ``Mount`` before its ``compose`` output
is attached, so querying for a child there is a race.
"""


from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from rich.markdown import Markdown
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Checkbox,
    DirectoryTree,
    Input,
    Label,
    ListItem,
    ListView,
    Static,
    TextArea,
)

from ..config import Config
from ..core.models import Conversation, Message, ToolCall
from ..tools.base import Tool


class SettingsScreen(ModalScreen[dict[str, Any] | None]):
    """Edit connection and behaviour settings.

    Dismisses with a dict of changes, or ``None`` if cancelled.
    """

    BINDINGS = [("escape", "cancel", "Cancel")]
    AUTO_FOCUS = "#base_url"

    def __init__(self, config: Config) -> None:
        super().__init__()
        self._config = config

    def compose(self) -> ComposeResult:
        cfg = self._config
        with VerticalScroll(id="dialog"):
            yield Static("Settings", id="dialog-title")
            yield Static(
                "Environment variables (OPENAI_API_KEY, OPENAI_BASE_URL) "
                "override these values.",
                id="dialog-hint",
            )

            yield Label("Base URL", classes="field-label")
            yield Input(value=cfg.base_url, id="base_url", placeholder="https://api.openai.com/v1")

            yield Label("API key", classes="field-label")
            yield Input(
                value=cfg.api_key,
                id="api_key",
                password=True,
                placeholder="sk-… (leave blank for local servers)",
            )

            yield Label("Model", classes="field-label")
            yield Input(value=cfg.model, id="model", placeholder="gpt-4o-mini")

            yield Label("Temperature", classes="field-label")
            yield Input(value=str(cfg.temperature), id="temperature")

            yield Label("Max tokens (0 = server default)", classes="field-label")
            yield Input(value=str(cfg.max_tokens), id="max_tokens")

            yield Label("System prompt", classes="field-label")
            yield TextArea(cfg.system_prompt, id="system_prompt", soft_wrap=True)

            yield Label("Behaviour", classes="field-label")
            yield Checkbox("Stream responses", value=cfg.stream, id="stream")
            yield Checkbox("Enable tools", value=cfg.tools_enabled, id="tools_enabled")
            yield Checkbox(
                "Auto-approve tools (skips confirmation)",
                value=cfg.auto_approve_tools,
                id="auto_approve_tools",
            )
            yield Checkbox(
                "Allow shell tool (runs commands)",
                value=cfg.allow_shell_tool,
                id="allow_shell_tool",
            )
            yield Checkbox(
                "Allow network tool", value=cfg.allow_network_tool, id="allow_network_tool"
            )
            yield Checkbox("Enable memory", value=cfg.memory_enabled, id="memory_enabled")
            yield Checkbox("Enable vision/images", value=cfg.vision_enabled, id="vision_enabled")

            with Horizontal(id="button-row"):
                yield Button("Cancel", id="cancel")
                yield Button("Save", id="save", variant="primary")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "save":
            self.dismiss(self._collect())
        else:
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _collect(self) -> dict[str, Any]:
        """Read every field back out. Config.from_dict does the coercion."""
        changes: dict[str, Any] = {
            "base_url": self.query_one("#base_url", Input).value.strip(),
            "api_key": self.query_one("#api_key", Input).value.strip(),
            "model": self.query_one("#model", Input).value.strip(),
            "system_prompt": self.query_one("#system_prompt", TextArea).text,
        }

        # Numeric fields fall back to the current value if the user typed junk.
        for field, current in (
            ("temperature", self._config.temperature),
            ("max_tokens", self._config.max_tokens),
        ):
            raw = self.query_one(f"#{field}", Input).value.strip()
            try:
                changes[field] = type(current)(raw)
            except (TypeError, ValueError):
                changes[field] = current

        for field in (
            "stream",
            "tools_enabled",
            "auto_approve_tools",
            "allow_shell_tool",
            "allow_network_tool",
            "memory_enabled",
            "vision_enabled",
        ):
            changes[field] = self.query_one(f"#{field}", Checkbox).value

        return changes


class ApprovalScreen(ModalScreen[str]):
    """Ask before running a tool.

    Dismisses with ``"once"``, ``"session"`` or ``"deny"``.
    """

    BINDINGS = [("escape", "deny", "Deny")]
    AUTO_FOCUS = "#once"

    def __init__(self, tool: Tool, call: ToolCall) -> None:
        super().__init__()
        self._tool = tool
        self._call = call

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="dialog"):
            yield Static(f"Run tool: {self._tool.name}?", id="dialog-title")
            yield Static(self._tool.description, id="dialog-hint")

            args = self._call.parsed_arguments()
            pretty = json.dumps(args, indent=2) if args else "(no arguments)"
            yield Static(Text(pretty), id="approval-detail", markup=False)

            with Horizontal(id="button-row"):
                yield Button("Deny", id="deny", variant="error")
                yield Button("Allow once", id="once", variant="primary")
                yield Button("Allow for session", id="session")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id or "deny")

    def action_deny(self) -> None:
        self.dismiss("deny")


class SearchScreen(ModalScreen[str | None]):
    """Full-text search over history; dismisses with a conversation id."""

    BINDINGS = [("escape", "cancel", "Cancel")]
    AUTO_FOCUS = "#search-input"

    def __init__(self, results_provider) -> None:  # noqa: ANN001 - async callable
        super().__init__()
        self._provider = results_provider
        self._matches: list[tuple[Conversation, Message]] = []

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="dialog", classes="wide"):
            yield Static("Search history", id="dialog-title")
            yield Input(placeholder="Type to search…", id="search-input")
            yield ListView(id="results-list")

    async def on_input_changed(self, event: Input.Changed) -> None:
        query = event.value.strip()
        listview = self.query_one("#results-list", ListView)
        await listview.clear()

        if len(query) < 2:
            self._matches = []
            return

        self._matches = await self._provider(query)
        for convo, message in self._matches:
            preview = " ".join(message.content.split())[:70]
            label = Text()
            label.append(f"{convo.title}  ", style="bold")
            label.append(preview, style="#7d8590")
            await listview.append(ListItem(Static(label)))

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        index = event.list_view.index
        if index is not None and 0 <= index < len(self._matches):
            self.dismiss(self._matches[index][0].id)

    def action_cancel(self) -> None:
        self.dismiss(None)


class FilePickerScreen(ModalScreen[str | None]):
    """Pick a file to attach, by browsing or by typing a path."""

    BINDINGS = [("escape", "cancel", "Cancel")]
    AUTO_FOCUS = "#path-input"

    def __init__(self, root: Path) -> None:
        super().__init__()
        self._root = root

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="dialog", classes="wide"):
            yield Static("Attach a file", id="dialog-title")
            yield Static(
                "Select a file below, or type a path and press Enter.", id="dialog-hint"
            )
            yield Input(placeholder=str(self._root / "example.py"), id="path-input")
            yield DirectoryTree(str(self._root), id="file-tree")
            with Horizontal(id="button-row"):
                yield Button("Cancel", id="cancel")

    def on_directory_tree_file_selected(self, event: DirectoryTree.FileSelected) -> None:
        self.dismiss(str(event.path))

    def on_input_submitted(self, event: Input.Submitted) -> None:
        value = event.value.strip()
        if value:
            self.dismiss(value)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class HelpScreen(ModalScreen[None]):
    """Keyboard shortcuts and slash commands."""

    BINDINGS = [("escape", "close", "Close"), ("q", "close", "Close")]
    AUTO_FOCUS = "#close"

    def __init__(self, body: str) -> None:
        super().__init__()
        self._body = body

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="dialog", classes="wide"):
            yield Static("Help", id="dialog-title")
            yield Static(Markdown(self._body), id="help-body")
            with Horizontal(id="button-row"):
                yield Button("Close", id="close", variant="primary")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(None)

    def action_close(self) -> None:
        self.dismiss(None)


class ConfirmScreen(ModalScreen[bool]):
    """Yes/no confirmation."""

    BINDINGS = [("escape", "cancel", "Cancel")]
    AUTO_FOCUS = "#cancel"

    def __init__(self, question: str, *, confirm_label: str = "Confirm") -> None:
        super().__init__()
        self._question = question
        self._confirm_label = confirm_label

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="dialog"):
            yield Static(self._question, id="dialog-title")
            with Horizontal(id="button-row"):
                yield Button("Cancel", id="cancel")
                yield Button(self._confirm_label, id="confirm", variant="error")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm")

    def action_cancel(self) -> None:
        self.dismiss(False)
