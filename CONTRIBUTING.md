# Contributing to pyassist

Thank you for your interest in contributing to **pyassist**!

`pyassist` is a terminal AI assistant built with [Textual](https://github.com/Textualize/textual). It aims to be fast, reliable, beautiful, and completely local-first.

---

## Development Setup

1. **Clone the repository**:
   ```bash
   git clone https://github.com/aotlover9-base-eth/pyassist.git
   cd pyassist
   ```

2. **Create and activate a virtual environment**:
   - **Linux / macOS**:
     ```bash
     python3 -m venv .venv
     source .venv/bin/activate
     ```
   - **Windows**:
     ```cmd
     python -m venv .venv
     .venv\Scripts\activate
     ```

3. **Install editable package and dev dependencies**:
   ```bash
   pip install -e ".[dev]"
   ```

---

## Running Tests and Linter

Before submitting a pull request, make sure tests pass and the code adheres to formatting standards:

```bash
# Run the test suite
pytest -v

# Run the linter
ruff check .

# Fix auto-fixable lint issues
ruff check --fix .
```

All 90+ unit and integration tests must pass cleanly.

---

## Architecture Overview

```
src/pyassist/
├── config.py            # Dataclass settings, JSON persistence, env overrides
├── paths.py             # Cross-platform platformdirs config/data/plugin paths
├── cli.py               # Command-line entry point, headless mode (-p)
├── core/
│   ├── models.py        # Message, Conversation, ToolCall, Attachment, Usage
│   ├── storage.py       # Async SQLite persistence: WAL, migrations, FTS5 search
│   ├── memory.py        # Long-term facts injected into system prompt
│   ├── llm.py           # OpenAI-compatible SSE streaming client
│   ├── engine.py        # Agent loop & tool execution orchestration
│   ├── events.py        # UI-agnostic engine events
│   ├── attachments.py   # File upload: text inlining, image data URLs
│   └── bootstrap.py     # Application wiring & dependency graph
├── tools/
│   ├── base.py          # Tool ABC, ToolRegistry, ToolResult
│   └── builtin.py       # Filesystem, shell, HTTP, memory, datetime tools
├── plugins/
│   ├── api.py           # Public plugin author API (re-exported)
│   └── loader.py        # Entry-point and directory plugin discovery
└── ui/
    ├── app.py           # Textual App, keybindings, event handling
    ├── widgets.py       # Message bubbles, tool panels, composer
    ├── screens.py       # Settings, search, file picker, approval modals
    └── theme.py         # Minimalist dark theme CSS
```

---

## Writing a Plugin

You can extend `pyassist` with custom tools and slash commands without modifying core code. Drop a Python script in your plugin directory (`pyassist --plugin-dir`):

```python
"""Reverse text plugin."""
from pyassist.plugins.api import PluginRegistry, ToolContext, ToolResult

__version__ = "1.0.0"

async def reverse_text(args: dict, ctx: ToolContext) -> ToolResult:
    text = str(args.get("text", ""))
    return ToolResult(content=text[::-1], display=f"Reversed {len(text)} chars")

def register(registry: PluginRegistry) -> None:
    registry.add_function_tool(
        reverse_text,
        name="reverse_text",
        description="Reverse a string of text.",
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string", "description": "Text to reverse."}},
            "required": ["text"],
        },
    )
    registry.add_command("/flip", lambda args, ctx: text[::-1], "Flip text")
```

---

## Pull Request Guidelines

1. **Focused PRs**: Keep changes concise and focused on a single feature or fix.
2. **Add Tests**: Add tests covering any bug fixes or new functionality in `tests/`.
3. **Docs**: Update `README.md` or `GETTING_STARTED.md` if changing user-facing behavior.
