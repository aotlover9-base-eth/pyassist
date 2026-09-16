"""The Textual application.

Responsibilities
----------------
* Own the widget tree and key bindings.
* Translate engine events into transcript widgets.
* Implement slash commands (including any contributed by plugins).

Concurrency: the engine runs in a worker task so the UI never blocks. The task
handle is kept in ``self._stream_task`` so ``Esc`` can cancel mid-stream.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Footer, Header, ListItem, ListView, Static

from ..config import mask_secret
from ..core import attachments as attachments_mod
from ..core.bootstrap import AppContext
from ..core.events import (
    EngineError,
    MessageCompleted,
    ReasoningDelta,
    TextDelta,
    ToolCallDenied,
    ToolCallFinished,
    ToolCallStarted,
    TurnFinished,
    UsageReported,
)
from ..core.models import Attachment, Conversation, ToolCall
from ..plugins.api import SlashCommand
from ..tools.base import Tool, ToolContext
from . import theme
from .screens import (
    ApprovalScreen,
    ConfirmScreen,
    FilePickerScreen,
    HelpScreen,
    SearchScreen,
    SettingsScreen,
)
from .widgets import (
    Composer,
    MessageBubble,
    NoticePanel,
    ReasoningPanel,
    ToolCallPanel,
    WelcomePanel,
)

HELP_TEXT = """
### Keys

| Key | Action |
| --- | --- |
| `Enter` | Send message |
| `Ctrl+J` | Newline in composer |
| `F2` | Settings |
| `Ctrl+N` | New chat |
| `Ctrl+B` | Toggle sidebar |
| `Ctrl+F` | Search history |
| `Ctrl+U` | Attach a file |
| `Ctrl+R` | Regenerate last reply |
| `Esc` | Stop streaming |
| `Ctrl+Q` | Quit |

### Slash commands

| Command | Action |
| --- | --- |
| `/help` | This screen |
| `/new` | Start a new chat |
| `/clear` | Clear the current chat |
| `/model [name]` | Show or switch model |
| `/models` | List models from the server |
| `/settings` | Open settings |
| `/upload <path>` | Attach a file |
| `/tools` | List available tools |
| `/plugins` | List loaded plugins |
| `/memory` | Show stored memories |
| `/remember k=v` | Store a fact |
| `/forget <key>` | Delete a fact |
| `/search <query>` | Search history |
| `/export` | Export chat as Markdown |
| `/stats` | Database statistics |
| `/quit` | Exit |
"""


class PyAssistApp(App[None]):
    """pyassist's Textual front-end."""

    CSS = theme.CSS
    TITLE = "pyassist"

    BINDINGS = [
        ("ctrl+q", "quit", "Quit"),
        ("f2", "settings", "Settings"),
        ("ctrl+n", "new_chat", "New"),
        ("ctrl+b", "toggle_sidebar", "Sidebar"),
        ("ctrl+f", "search", "Search"),
        ("ctrl+u", "attach", "Attach"),
        ("ctrl+r", "regenerate", "Regenerate"),
        ("escape", "stop", "Stop"),
        ("f1", "help", "Help"),
    ]

    def __init__(self, context: AppContext) -> None:
        super().__init__()
        self.ctx = context
        self.conversation: Conversation | None = None
        self._conversations: list[Conversation] = []
        self._pending: list[Attachment] = []
        self._stream_task: asyncio.Task[None] | None = None
        self._current_bubble: MessageBubble | None = None
        self._reasoning_panel: ReasoningPanel | None = None
        self._tool_panels: dict[str, ToolCallPanel] = {}
        # The engine calls back into the UI for approvals and status text.
        self.ctx.engine._approve = self._approve_tool
        self.ctx.engine._notify = self._notify

    # ------------------------------------------------------------------
    # layout
    # ------------------------------------------------------------------
    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Horizontal(id="body"):
            with Vertical(id="sidebar"):
                yield Static("CHATS", id="sidebar-title")
                yield ListView(id="conversation-list")
            with Vertical(id="chat-pane"):
                yield VerticalScroll(id="transcript")
                with Vertical(id="composer-area"):
                    yield Static("", id="attachment-bar", classes="hidden")
                    yield Composer()
                yield Static("", id="status-bar")
        yield Footer()

    async def on_mount(self) -> None:
        if not self.ctx.config.show_sidebar:
            self.query_one("#sidebar").add_class("hidden")

        await self._refresh_conversations()
        await self.action_new_chat()

        for warning in self.ctx.warnings:
            self.notify(warning, severity="warning", timeout=8)

        self.query_one(Composer).focus()
        self._update_status()

    # ------------------------------------------------------------------
    # conversations
    # ------------------------------------------------------------------
    async def _refresh_conversations(self) -> None:
        self._conversations = await self.ctx.storage.list_conversations(limit=100)
        listview = self.query_one("#conversation-list", ListView)
        await listview.clear()
        for convo in self._conversations:
            listview.append(ListItem(Static(convo.title)))

    async def _load_conversation(self, conversation_id: str) -> None:
        convo = await self.ctx.storage.get_conversation(conversation_id)
        if convo is None:
            return
        self.conversation = convo

        transcript = self.query_one("#transcript", VerticalScroll)
        await transcript.remove_children()

        messages = await self.ctx.storage.get_messages(conversation_id)
        for message in messages:
            if message.role == "user":
                await transcript.mount(MessageBubble("user", message.display_content))
            elif message.role == "assistant":
                if message.content:
                    await transcript.mount(MessageBubble("assistant", message.content))
                for call in message.tool_calls:
                    panel = ToolCallPanel(call)
                    await transcript.mount(panel)
            elif message.role == "tool":
                # Tool output is already summarised by its panel above.
                continue

        if not messages:
            await transcript.mount(
                WelcomePanel(
                    self.ctx.config.model,
                    len(self.ctx.tools.available(self.ctx.config)),
                    self.ctx.config.is_configured,
                )
            )
        transcript.scroll_end(animate=False)
        self._update_status()

    async def on_list_view_selected(self, event: ListView.Selected) -> None:
        if event.list_view.id != "conversation-list":
            return
        index = event.list_view.index
        if index is not None and 0 <= index < len(self._conversations):
            await self._load_conversation(self._conversations[index].id)
            self.query_one(Composer).focus()

    # ------------------------------------------------------------------
    # sending
    # ------------------------------------------------------------------
    def on_composer_submitted(self, event: Composer.Submitted) -> None:
        """Handle Enter in the composer.

        Submission runs in a worker rather than inline: a slash command may
        open a modal, and awaiting one on the message pump would block the very
        pump that delivers the keypress dismissing it.
        """
        event.stop()
        self.run_worker(self._submit(), group="submit")

    async def _submit(self) -> None:
        composer = self.query_one(Composer)
        text = composer.value.strip()
        if not text:
            return

        if text.startswith("/"):
            composer.clear_input()
            await self._run_command(text)
            return

        if self._stream_task and not self._stream_task.done():
            self.notify("Still responding - press Esc to stop.", severity="warning")
            return

        composer.clear_input()
        await self._send(text)

    async def _send(self, text: str) -> None:
        if self.conversation is None:
            await self.action_new_chat()
        assert self.conversation is not None

        if not self.ctx.config.is_configured:
            await self._mount(
                NoticePanel(
                    "No API key configured.",
                    detail="Press F2 to open Settings, or set OPENAI_API_KEY.",
                )
            )
            return

        await self._clear_welcome()
        await self._mount(MessageBubble("user", text))

        attachments = list(self._pending)
        self._pending.clear()
        self._update_attachment_bar()

        self._stream_task = asyncio.create_task(self._stream(text, attachments))

    async def _stream(self, text: str, attachments: list[Attachment]) -> None:
        """Consume engine events and render them."""
        self._current_bubble = None
        self._reasoning_panel = None
        self._tool_panels.clear()
        self._set_status("thinking…")

        try:
            async for event in self.ctx.engine.send(
                self.conversation.id, text, attachments=attachments
            ):
                await self._handle_event(event)
        except asyncio.CancelledError:
            if self._current_bubble is not None:
                self._current_bubble.flush()
            await self._mount(NoticePanel("Stopped.", error=False))
            raise
        except Exception as exc:  # noqa: BLE001 - last line of defence
            await self._mount(NoticePanel("Unexpected error.", detail=str(exc)))
        finally:
            if self._current_bubble is not None:
                self._current_bubble.flush()
            if self._reasoning_panel is not None:
                self._reasoning_panel.flush()
            self._set_status("")
            self._scroll_end()

    async def _handle_event(self, event: Any) -> None:
        transcript = self.query_one("#transcript", VerticalScroll)

        if isinstance(event, TextDelta):
            if self._current_bubble is None:
                self._current_bubble = MessageBubble("assistant")
                await transcript.mount(self._current_bubble)
            self._current_bubble.append(event.text)
            self._scroll_end()

        elif isinstance(event, ReasoningDelta):
            if self._reasoning_panel is None:
                self._reasoning_panel = ReasoningPanel()
                await transcript.mount(self._reasoning_panel)
            self._reasoning_panel.append(event.text)

        elif isinstance(event, ToolCallStarted):
            panel = ToolCallPanel(event.call)
            self._tool_panels[event.call.id] = panel
            await transcript.mount(panel)
            self._set_status(f"running {event.call.name}…")
            self._scroll_end()

        elif isinstance(event, ToolCallFinished):
            panel = self._tool_panels.get(event.call.id)
            if panel is None:
                panel = ToolCallPanel(event.call)
                self._tool_panels[event.call.id] = panel
                await transcript.mount(panel)
            panel.finish(ok=event.ok, output=event.output, duration=event.duration)
            # The next model reply belongs in a fresh bubble.
            self._current_bubble = None
            self._scroll_end()

        elif isinstance(event, ToolCallDenied):
            panel = self._tool_panels.get(event.call.id)
            if panel is not None:
                panel.deny(event.reason)
            self._current_bubble = None

        elif isinstance(event, MessageCompleted):
            if self._current_bubble is not None:
                self._current_bubble.flush()
            self._current_bubble = None

        elif isinstance(event, UsageReported):
            self._last_usage = event.usage

        elif isinstance(event, TurnFinished):
            # The engine may have auto-titled the conversation; re-read it so the
            # sidebar and /export heading show the new name.
            if self.conversation is not None:
                refreshed = await self.ctx.storage.get_conversation(self.conversation.id)
                if refreshed is not None:
                    self.conversation = refreshed
            await self._refresh_conversations()
            self._update_status()

        elif isinstance(event, EngineError):
            await transcript.mount(NoticePanel(event.message, detail=event.detail))
            self._scroll_end()

    # ------------------------------------------------------------------
    # modals
    # ------------------------------------------------------------------
    async def _ask(self, screen: Any) -> Any:
        """Show a modal screen and return whatever it dismisses with.

        Textual's ``push_screen_wait`` is only legal inside a worker, but we
        open modals from several kinds of caller: slash commands, key actions
        and the streaming task (tool approval). Pushing with a callback that
        resolves a future gives the same "await the answer" ergonomics with no
        worker requirement.

        The one hard rule: **never await this from the message pump.** The pump
        is what delivers the keypress that dismisses the modal, so blocking it
        here deadlocks the app. Every caller therefore sits in a worker or a
        task -- see ``on_composer_submitted`` and the ``action_*`` wrappers.
        """

        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()

        def resolve(result: Any) -> None:
            if not future.done():
                future.set_result(result)

        self.push_screen(screen, resolve)
        return await future

    # ------------------------------------------------------------------
    # engine callbacks
    # ------------------------------------------------------------------
    async def _approve_tool(self, tool: Tool, call: ToolCall) -> bool:
        choice = await self._ask(ApprovalScreen(tool, call))
        if choice == "session":
            self.ctx.engine.approve_for_session(tool.name)
            return True

        return choice == "once"

    async def _notify(self, message: str) -> None:
        self._set_status(message)

    # ------------------------------------------------------------------
    # actions
    # ------------------------------------------------------------------
    async def action_new_chat(self) -> None:
        convo = await self.ctx.storage.create_conversation(model=self.ctx.config.model)
        self.conversation = convo
        await self._refresh_conversations()

        transcript = self.query_one("#transcript", VerticalScroll)
        await transcript.remove_children()
        await transcript.mount(
            WelcomePanel(
                self.ctx.config.model,
                len(self.ctx.tools.available(self.ctx.config)),
                self.ctx.config.is_configured,
            )
        )
        self._update_status()

    def action_toggle_sidebar(self) -> None:
        sidebar = self.query_one("#sidebar")
        sidebar.toggle_class("hidden")
        self.ctx.config.show_sidebar = not sidebar.has_class("hidden")

    # Modal-opening actions are deliberately *sync*: Textual awaits an async
    # action on the message pump, which would block the pump the modal needs in
    # order to be dismissed. They hand off to a worker and return immediately.
    def action_settings(self) -> None:
        self.run_worker(self._open_settings(), group="modal")

    def action_search(self) -> None:
        self.run_worker(self._open_search(), group="modal")

    def action_attach(self) -> None:
        self.run_worker(self._open_attach(), group="modal")

    def action_help(self) -> None:
        self.run_worker(self._open_help(), group="modal")

    async def _open_settings(self) -> None:
        changes = await self._ask(SettingsScreen(self.ctx.config))
        if not changes:
            return
        self.ctx.config.update(**changes)
        self.ctx.config.save()
        # Base URL / key live in the HTTP client's headers, so rebuild it.
        self.ctx.rebuild_llm()
        self.notify("Settings saved.")
        self._update_status()

    async def _open_search(self) -> None:
        conversation_id = await self._ask(SearchScreen(self.ctx.storage.search_messages))
        if conversation_id:
            await self._load_conversation(conversation_id)

    async def _open_attach(self) -> None:
        path = await self._ask(FilePickerScreen(self.ctx.workspace))
        if path:
            await self._attach(path)

    async def _open_help(self) -> None:
        await self._ask(HelpScreen(self._help_text()))

    async def action_regenerate(self) -> None:
        if self.conversation is None:
            return
        if self._stream_task and not self._stream_task.done():
            self.notify("Still responding.", severity="warning")
            return
        self._stream_task = asyncio.create_task(self._regenerate())

    async def _regenerate(self) -> None:
        self._current_bubble = None
        self._tool_panels.clear()
        self._set_status("regenerating…")
        try:
            history = await self.ctx.storage.get_messages(self.conversation.id)
            last_user = next((m for m in reversed(history) if m.role == "user"), None)
            if last_user is None:
                await self._mount(NoticePanel("Nothing to regenerate yet.", error=False))
                return
            await self.ctx.storage.delete_messages_after(
                self.conversation.id, last_user.created_at
            )
            await self._load_conversation(self.conversation.id)

            async for event in self.ctx.engine._run_turn(self.conversation.id, last_user):
                await self._handle_event(event)
        finally:
            await self._load_conversation(self.conversation.id)
            self._set_status("")

    def action_stop(self) -> None:
        if self._stream_task and not self._stream_task.done():
            self._stream_task.cancel()
            self._set_status("")

    # ------------------------------------------------------------------
    # slash commands
    # ------------------------------------------------------------------
    async def _run_command(self, raw: str) -> None:
        name, _, args = raw.partition(" ")
        name = name.lower()
        args = args.strip()

        handlers = {
            "/help": self._cmd_help,
            "/new": self._cmd_new,
            "/clear": self._cmd_clear,
            "/model": self._cmd_model,
            "/models": self._cmd_models,
            "/settings": self._cmd_settings,
            "/upload": self._cmd_upload,
            "/tools": self._cmd_tools,
            "/plugins": self._cmd_plugins,
            "/memory": self._cmd_memory,
            "/remember": self._cmd_remember,
            "/forget": self._cmd_forget,
            "/search": self._cmd_search,
            "/export": self._cmd_export,
            "/stats": self._cmd_stats,
            "/quit": self._cmd_quit,
            "/exit": self._cmd_quit,
        }

        if handler := handlers.get(name):
            await handler(args)
            return

        # Plugin-contributed commands.
        if command := self.ctx.plugins.commands.get(name):
            await self._run_plugin_command(command, args)
            return

        await self._mount(NoticePanel(f"Unknown command {name}. Try /help."))

    async def _run_plugin_command(self, command: SlashCommand, args: str) -> None:
        ctx = ToolContext(
            config=self.ctx.config,
            storage=self.ctx.storage,
            workspace=self.ctx.workspace,
            conversation_id=self.conversation.id if self.conversation else "",
            notify=self._notify,
        )
        try:
            output = await command.handler(args, ctx)
        except Exception as exc:  # noqa: BLE001 - a plugin must not crash the app
            await self._mount(
                NoticePanel(f"Command {command.name} failed.", detail=str(exc))
            )
            return
        await self._mount(MessageBubble("assistant", str(output)))

    # Slash commands already run in a worker (see ``on_composer_submitted``),
    # so they can await modals directly.
    async def _cmd_help(self, args: str) -> None:
        await self._open_help()

    async def _cmd_new(self, args: str) -> None:
        await self.action_new_chat()

    async def _cmd_clear(self, args: str) -> None:
        if self.conversation is None:
            return
        confirmed = await self._ask(
            ConfirmScreen("Delete this conversation?", confirm_label="Delete")
        )
        if confirmed:
            await self.ctx.storage.delete_conversation(self.conversation.id)
            await self.action_new_chat()

    async def _cmd_model(self, args: str) -> None:
        if not args:
            await self._mount(
                MessageBubble("assistant", f"Current model: `{self.ctx.config.model}`")
            )
            return
        self.ctx.config.model = args
        self.ctx.config.save()
        self._update_status()
        await self._mount(MessageBubble("assistant", f"Model switched to `{args}`."))

    async def _cmd_models(self, args: str) -> None:
        self._set_status("fetching models…")
        models = await self.ctx.llm.list_models()
        self._set_status("")
        if not models:
            await self._mount(
                NoticePanel("Could not list models.", detail="The server may not support /models.")
            )
            return
        listing = "\n".join(f"- `{m}`" for m in models[:60])
        await self._mount(MessageBubble("assistant", f"**Available models**\n\n{listing}"))

    async def _cmd_settings(self, args: str) -> None:
        await self._open_settings()

    async def _cmd_upload(self, args: str) -> None:
        if not args:
            await self._open_attach()
            return
        await self._attach(args)

    async def _cmd_tools(self, args: str) -> None:
        tools = self.ctx.tools.available(self.ctx.config)
        if not tools:
            await self._mount(NoticePanel("No tools are enabled.", error=False))
            return
        lines = ["**Available tools**", ""]
        for tool in tools:
            flag = " *(asks first)*" if tool.requires_approval else ""
            lines.append(f"- `{tool.name}`{flag} — {tool.description}")
        await self._mount(MessageBubble("assistant", "\n".join(lines)))

    async def _cmd_plugins(self, args: str) -> None:
        plugins = self.ctx.plugins.plugins
        if not plugins:
            await self._mount(
                MessageBubble(
                    "assistant",
                    "No plugins loaded.\n\nDrop a `.py` file in your plugin directory "
                    "or run `pyassist --write-example-plugin`.",
                )
            )
            return
        lines = ["**Plugins**", ""]
        for info in plugins:
            status = "✓" if info.ok else f"✗ {info.error}"
            lines.append(f"- `{info.name}` v{info.version} — {status}")
            if info.tools:
                lines.append(f"  - tools: {', '.join(f'`{t}`' for t in info.tools)}")
            if info.commands:
                lines.append(f"  - commands: {', '.join(f'`{c}`' for c in info.commands)}")
        await self._mount(MessageBubble("assistant", "\n".join(lines)))

    async def _cmd_memory(self, args: str) -> None:
        memories = await self.ctx.storage.list_memories()
        if not memories:
            await self._mount(
                MessageBubble("assistant", "Nothing remembered yet. Try `/remember key=value`.")
            )
            return
        lines = ["**Memories**", ""]
        lines += [f"- **{m.key}**: {m.value}" for m in memories]
        await self._mount(MessageBubble("assistant", "\n".join(lines)))

    async def _cmd_remember(self, args: str) -> None:
        key, sep, value = args.partition("=")
        if not sep or not key.strip() or not value.strip():
            await self._mount(NoticePanel("Usage: /remember key=value"))
            return
        await self.ctx.storage.upsert_memory(
            key.strip(),
            value.strip(),
            conversation_id=self.conversation.id if self.conversation else "",
        )
        await self._mount(MessageBubble("assistant", f"Remembered **{key.strip()}**."))

    async def _cmd_forget(self, args: str) -> None:
        if not args:
            await self._mount(NoticePanel("Usage: /forget <key>"))
            return
        removed = await self.ctx.storage.delete_memory(args)
        message = f"Forgot **{args}**." if removed else f"No memory named **{args}**."
        await self._mount(MessageBubble("assistant", message))

    async def _cmd_search(self, args: str) -> None:
        if not args:
            await self._open_search()
            return
        results = await self.ctx.storage.search_messages(args)
        if not results:
            await self._mount(MessageBubble("assistant", f"No matches for “{args}”."))
            return
        lines = [f"**{len(results)} matches for “{args}”**", ""]
        for convo, message in results[:20]:
            preview = " ".join(message.content.split())[:80]
            lines.append(f"- *{convo.title}* — {preview}")
        await self._mount(MessageBubble("assistant", "\n".join(lines)))

    async def _cmd_export(self, args: str) -> None:
        if self.conversation is None:
            return
        from .. import paths

        messages = await self.ctx.storage.get_messages(self.conversation.id)
        lines = [f"# {self.conversation.title}", ""]
        for message in messages:
            if message.role == "user":
                lines += ["## You", "", message.content, ""]
            elif message.role == "assistant" and message.content:
                lines += ["## pyassist", "", message.content, ""]

        target = paths.export_dir() / f"{self.conversation.id}.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("\n".join(lines), encoding="utf-8")
        await self._mount(MessageBubble("assistant", f"Exported to `{target}`."))

    async def _cmd_stats(self, args: str) -> None:
        stats = await self.ctx.storage.stats()
        size_kb = stats.pop("size_bytes", 0) / 1024
        lines = ["**Database**", ""]
        lines += [f"- {k}: {v}" for k, v in stats.items()]
        lines.append(f"- size: {size_kb:.1f} KB")
        lines.append(f"- path: `{self.ctx.storage.path}`")
        await self._mount(MessageBubble("assistant", "\n".join(lines)))

    async def _cmd_quit(self, args: str) -> None:
        self.exit()

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    async def _attach(self, path: str) -> None:
        try:
            attachment = attachments_mod.build_attachment(
                path,
                max_bytes=self.ctx.config.max_attachment_bytes,
                vision_enabled=self.ctx.config.vision_enabled,
            )
        except OSError as exc:
            await self._mount(NoticePanel(f"Could not attach {path}", detail=str(exc)))
            return

        if attachment.truncated and not attachment.content:
            await self._mount(
                NoticePanel(
                    f"{attachment.name} is too large to inline.",
                    detail="Ask the assistant to use the read_file tool instead.",
                    error=False,
                )
            )
            return

        self._pending.append(attachment)
        self._update_attachment_bar()

        # Text attachments are inlined into the prompt as a fenced block.
        if attachment.kind == "text" and attachment.content:
            composer = self.query_one(Composer)
            existing = composer.value
            block = f"\n\n```{_suffix(attachment.name)}\n{attachment.content}\n```"
            sep = "\n\n" if existing.strip() else ""
            composer.text = f"{existing}{sep}Contents of {attachment.name}:{block}"

    def _update_attachment_bar(self) -> None:
        bar = self.query_one("#attachment-bar", Static)
        if not self._pending:
            bar.add_class("hidden")
            bar.update("")
            return
        names = ", ".join(a.name for a in self._pending)
        bar.remove_class("hidden")
        bar.update(f"📎 {names}")

    async def _clear_welcome(self) -> None:
        for widget in self.query(WelcomePanel):
            await widget.remove()

    async def _mount(self, widget: Any) -> None:
        transcript = self.query_one("#transcript", VerticalScroll)
        await self._clear_welcome()
        await transcript.mount(widget)
        self._scroll_end()

    def _scroll_end(self) -> None:
        self.query_one("#transcript", VerticalScroll).scroll_end(animate=False)

    def _set_status(self, message: str) -> None:
        self.query_one("#status-bar", Static).update(message)

    def _update_status(self) -> None:
        cfg = self.ctx.config
        tools = len(self.ctx.tools.available(cfg))
        key = mask_secret(cfg.api_key) or "no key"
        self.sub_title = f"{cfg.model} · {tools} tools · {key}"

    def _help_text(self) -> str:
        text = HELP_TEXT
        commands = self.ctx.plugins.commands
        if commands:
            rows = "\n".join(
                f"| `{c.name}` | {c.description or '—'} (from {c.plugin}) |"
                for c in commands.values()
            )
            text += f"\n### Plugin commands\n\n| Command | Action |\n| --- | --- |\n{rows}\n"
        return text

    async def on_unmount(self) -> None:
        await self.ctx.aclose()


def _suffix(name: str) -> str:
    """Language hint for a fenced code block."""
    suffix = Path(name).suffix.lstrip(".").lower()
    return {"py": "python", "js": "javascript", "ts": "typescript", "md": "markdown"}.get(
        suffix, suffix
    )
