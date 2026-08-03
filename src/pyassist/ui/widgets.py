"""Custom widgets for the chat transcript and composer.

Rendering notes
---------------
* Assistant text is rendered with Rich's :class:`~rich.markdown.Markdown`,
  which gives us headings, lists, tables and syntax-highlighted code fences
  for free (Rich delegates fenced blocks to Pygments).
* Streaming repaints are **throttled**. Refreshing on every SSE token pegs the
  CPU on fast providers, so deltas are buffered and flushed on a timer, which
  keeps the UI smooth without making the text feel laggy.
* User text is rendered as plain text, not Markdown: echoing a user's stray
  backticks or underscores as formatting is confusing, and it avoids any
  markup-injection surprises.
"""

from __future__ import annotations

import time

from rich.console import Group, RenderableType
from rich.markdown import Markdown
from rich.text import Text
from textual import events
from textual.app import ComposeResult

# Aliased: ``core.models.Message`` (a chat message) would otherwise shadow
# Textual's ``Message`` (an event) further down this module.
from textual.message import Message as TextualMessage
from textual.widget import Widget
from textual.widgets import Static, TextArea

from ..core.models import Message, ToolCall

#: Minimum seconds between repaints while streaming (~20 fps).
STREAM_REFRESH_INTERVAL = 0.05


class WelcomePanel(Static):
    """Shown in an empty conversation."""

    def __init__(self, model: str, tool_count: int, configured: bool) -> None:
        super().__init__(self._build(model, tool_count, configured))

    @staticmethod
    def _build(model: str, tool_count: int, configured: bool) -> RenderableType:

        lines = Text()
        lines.append("pyassist\n", style="bold #dfe3e8")
        lines.append(f"model: {model}   tools: {tool_count}\n\n", style="#565d67")

        if not configured:
            lines.append("⚠ No API key configured. ", style="#d29922")
            lines.append("Press F2 to open Settings.\n\n", style="#7d8590")

        lines.append("Type a message to begin, or ", style="#7d8590")
        lines.append("/help", style="#4c8dff")
        lines.append(" for commands.", style="#7d8590")
        return lines


class MessageBubble(Widget):
    """One user or assistant message.

    Holds its own text buffer so the app can append streaming deltas without
    rebuilding the widget tree.
    """

    DEFAULT_CSS = ""  # styled from theme.CSS

    def __init__(self, role: str, content: str = "", *, markdown: bool = True) -> None:
        super().__init__(classes=role)
        self.role = role
        self._buffer = content
        self._markdown = markdown
        self._body: Static | None = None
        self._last_refresh = 0.0
        self._dirty = False

    # -- construction ------------------------------------------------------
    def compose(self) -> ComposeResult:
        yield Static(self._role_label(), classes="role")
        self._body = Static(self._render_body(), classes="content", markup=False)
        yield self._body

    def _role_label(self) -> Text:
        label = "You" if self.role == "user" else "pyassist"
        return Text(label)

    def _render_body(self) -> RenderableType:
        if not self._buffer:
            # A zero-width space keeps the widget from collapsing while empty.
            return Text("​")
        if self._markdown and self.role == "assistant":
            return Markdown(self._buffer, code_theme="monokai", hyperlinks=False)
        return Text(self._buffer)

    # -- streaming ---------------------------------------------------------
    @property
    def text(self) -> str:
        return self._buffer

    def append(self, delta: str) -> None:
        """Buffer a streamed delta, repainting at most every 50 ms."""
        self._buffer += delta
        self._dirty = True

        now = time.monotonic()
        if now - self._last_refresh >= STREAM_REFRESH_INTERVAL:
            self.flush()

    def flush(self) -> None:
        """Force a repaint of any buffered text."""
        if not self._dirty or self._body is None:
            return
        self._body.update(self._render_body())
        self._last_refresh = time.monotonic()
        self._dirty = False

    def set_text(self, text: str) -> None:
        self._buffer = text
        self._dirty = True
        self.flush()

    @classmethod
    def from_message(cls, message: Message) -> MessageBubble:
        return cls(message.role, message.display_content)


class ReasoningPanel(Static):
    """Collapsed 'thinking' text from reasoning models."""

    def __init__(self, text: str = "") -> None:
        super().__init__("", markup=False)
        self._buffer = text
        self._last_refresh = 0.0
        self._dirty = bool(text)
        if text:
            self.flush()

    def append(self, delta: str) -> None:
        self._buffer += delta
        self._dirty = True
        if time.monotonic() - self._last_refresh >= STREAM_REFRESH_INTERVAL:
            self.flush()

    def flush(self) -> None:
        if not self._dirty:
            return
        self.update(Text(f"thinking… {self._buffer}"))
        self._last_refresh = time.monotonic()
        self._dirty = False


class ToolCallPanel(Static):
    """A single tool invocation: arguments, then result."""

    def __init__(self, call: ToolCall) -> None:
        super().__init__("", markup=False)
        self._call = call
        self._status = "running"
        self._output = ""
        self._duration = 0.0
        self._redraw()

    def finish(self, *, ok: bool, output: str, duration: float) -> None:
        self._status = "ok" if ok else "failed"
        self._output = output
        self._duration = duration
        self.set_classes(["ok" if ok else "failed"])
        self._redraw()

    def deny(self, reason: str) -> None:
        self._status = "denied"
        self._output = reason
        self.set_classes(["failed"])
        self._redraw()

    def _redraw(self) -> None:
        icon = {"running": "⟳", "ok": "✓", "failed": "✗", "denied": "⊘"}[self._status]
        header = Text()
        header.append(f"{icon} ", style="bold")
        header.append(self._call.name, style="bold #dfe3e8")

        args = self._call.parsed_arguments()
        if args:
            preview = ", ".join(f"{k}={_short(v)}" for k, v in args.items())
            header.append(f"  {preview}", style="#565d67")
        if self._duration:
            header.append(f"  {self._duration:.2f}s", style="#565d67")

        if not self._output:
            self.update(header)
            return

        body = Text(_clip(self._output, 500), style="#7d8590")
        self.update(Group(header, body))


class NoticePanel(Static):
    """A warning or error line in the transcript."""

    def __init__(self, message: str, *, detail: str = "", error: bool = True) -> None:
        super().__init__("", markup=False)
        if error:
            self.add_class("error")
        text = Text()
        text.append("✗ " if error else "⚠ ", style="bold")
        text.append(message)
        if detail:
            text.append(f"\n{_clip(detail, 400)}", style="#7d8590")
        self.update(text)


class Composer(TextArea):
    """Multi-line input.

    ``Enter`` submits and ``Ctrl+J`` inserts a newline, matching the muscle
    memory of most chat UIs.

    The swap has to happen *here* rather than in the app: ``TextArea`` consumes
    ``Enter`` in its own key handler to insert a newline and stops the event, so
    an ``on_key`` on the App would never see it. We intercept the key and post a
    message instead, which keeps the app decoupled from the widget internals.
    """

    class Submitted(TextualMessage):
        """Posted when the user presses Enter."""

    def __init__(self) -> None:
        super().__init__(id="composer", soft_wrap=True, show_line_numbers=False)

    async def _on_key(self, event: events.Key) -> None:
        if event.key == "enter":
            event.prevent_default()
            event.stop()
            self.post_message(self.Submitted())
            return
        if event.key == "ctrl+j":
            event.prevent_default()
            event.stop()
            self.insert("\n")
            return
        await super()._on_key(event)

    @property
    def value(self) -> str:
        return self.text

    def clear_input(self) -> None:
        self.text = ""


def _short(value: object, limit: int = 40) -> str:
    text = str(value).replace("\n", " ")
    return text if len(text) <= limit else text[:limit] + "…"


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[:limit].rstrip() + " …"
