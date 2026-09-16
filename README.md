# pyassist

<p align="center">
  <a href="https://github.com/aotlover9-base-eth/pyassist/actions"><img src="https://github.com/aotlover9-base-eth/pyassist/actions/workflows/ci.yml/badge.svg" alt="CI Status" /></a>
  <img src="https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13%20%7C%203.14-3776AB.svg?logo=python&logoColor=white" alt="Python Versions" />
  <a href="https://opensource.org/licenses/MIT"><img src="https://img.shields.io/badge/License-MIT-yellow.svg" alt="License: MIT" /></a>
  <a href="https://github.com/astral-sh/ruff"><img src="https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json" alt="Ruff Code Style" /></a>
  <a href="https://github.com/Textualize/textual"><img src="https://img.shields.io/badge/built%20with-Textual-00d4aa.svg" alt="Built with Textual" /></a>
</p>

A terminal AI assistant built with **Textual**. Streams responses in real-time, speaks the standard OpenAI API wire protocol (so it works with OpenAI, Groq, OpenRouter, Ollama, LM Studio, vLLM, and more), renders GitHub-flavored Markdown with syntax highlighting, executes sandboxed tools with approval gates, persists chats and durable long-term memory locally in SQLite (WAL + FTS5 search), and supports hot-reloaded plugins.

```
┌ pyassist ─────────────────────────────────────────── gpt-4o-mini ─┐
│ Chats            │  You                                           │
│ ▸ Refactor auth  │  summarise pyproject.toml                      │
│   SQL indexes    │                                                │
│   Rust vs Go     │  pyassist                                      │
│                  │  Here's the gist:                              │
│                  │  ```toml                                       │
│                  │  [project]                                     │
│                  │  name = "pyassist"                             │
│                  │  ```                                           │
│                  │  ⚙ read_file  ✓ 0.01s                          │
├──────────────────┴────────────────────────────────────────────────┤
│ > ask anything…                                                   │
└───────────────────────────────────────────────────────────────────┘
```

---

## ✨ Features

- 🖥️ **Beautiful Terminal TUI**: Full-screen split-pane interface powered by [Textual](https://github.com/Textualize/textual) with live streaming, dark theme, and smooth throttled re-rendering.
- 🌐 **Universal Provider Support**: Connects to OpenAI, OpenRouter, Groq, Anthropic proxies, Ollama, LM Studio, vLLM, and any custom OpenAI-compatible endpoint.
- 🛠️ **Sandboxed Tool Calling**: Native function calling with strict directory containment (`read_file`, `write_file`, `list_directory`), SSRF-protected `http_request`, system time, and optional `run_shell`.
- 🛡️ **Defensive Approval Gates**: Actions requiring permissions prompt you first (once or for the session). Auto-approve can be toggled on demand.
- 🧠 **Long-Term Memory**: Durable facts stored locally in SQLite and dynamically injected into future conversation contexts.
- 🔍 **Full-Text History Search**: Instant conversation search powered by SQLite FTS5 (with graceful fallback to `LIKE`).
- 📎 **Multimodal File Attachments**: Attach code files (automatically inlined into context) or images (converted to base64 data URLs for vision models).
- 🔌 **Extensible Plugins**: Drop a single `.py` file into your plugins folder or install a Python package via entry points.
- ⚡ **Scriptable CLI Mode**: Run one-off prompts directly in your shell (`pyassist -p "..."`) with streaming stdout.

---

## 🚀 Installation

`pyassist` requires Python **3.11+**.

### Linux / macOS

```bash
git clone https://github.com/aotlover9-base-eth/pyassist.git
cd pyassist
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

### Windows (cmd / PowerShell)

```cmd
git clone https://github.com/aotlover9-base-eth/pyassist.git
cd pyassist
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"
```

---

## ⚙️ Configuration

Configure through the interactive Settings modal (`F2` inside the app) or directly from your terminal:

```bash
pyassist --set api_key=sk-...
pyassist --set base_url=https://api.openai.com/v1
pyassist --set model=gpt-4o-mini
```

### Environment Variables

Environment variables take precedence over saved settings:

```bash
# Linux / macOS
export OPENAI_API_KEY=sk-...
export OPENAI_BASE_URL=https://api.openai.com/v1

# Windows
set OPENAI_API_KEY=sk-...
set OPENAI_BASE_URL=https://api.openai.com/v1
```

### Supported Providers

Point `base_url` to your preferred server:

| Provider | `base_url` | API Key Required? |
| :--- | :--- | :--- |
| **OpenAI** | `https://api.openai.com/v1` | Yes (`sk-...`) |
| **Groq** | `https://api.groq.com/openai/v1` | Yes (`gsk_...`) |
| **OpenRouter** | `https://openrouter.ai/api/v1` | Yes (`sk-or-...`) |
| **Ollama** | `http://localhost:11434/v1` | No (or dummy `ollama`) |
| **LM Studio** | `http://localhost:1234/v1` | No |
| **vLLM** | `http://localhost:8000/v1` | No |

> 🔒 **Security Notice**: Your API key is stored locally in `config.json` with restricted permissions (`chmod 600` on POSIX systems). If you prefer not to persist secrets to disk, use the `OPENAI_API_KEY` environment variable.

---

## 💻 Usage

```bash
# Launch interactive TUI
pyassist

# Headless one-liner (pipeable, prints to stdout)
pyassist -p "Explain SQLite WAL mode in two sentences"

# Headless with auto-approved tools
pyassist -p "Inspect pyproject.toml and list dependencies" -y

# Inspect settings & masked credentials
pyassist --config

# List available tools
pyassist --list-tools

# List loaded plugins
pyassist --list-plugins
```

---

## ⌨️ Keyboard Shortcuts

| Key | Action |
| :--- | :--- |
| `Enter` | Send message |
| `Ctrl + J` | Insert newline in message composer |
| `Esc` | Stop streaming response / Cancel dialog |
| `F1` | Show Help screen |
| `F2` | Open Settings modal |
| `Ctrl + N` | Start a new chat |
| `Ctrl + B` | Toggle conversation sidebar |
| `Ctrl + F` | Search conversation history |
| `Ctrl + U` | Attach a file |
| `Ctrl + R` | Regenerate last assistant reply |
| `Ctrl + Q` | Quit application |

---

## 💬 Slash Commands

Type these directly into the message box:

| Command | Action |
| :--- | :--- |
| `/help` | Display shortcuts and commands reference |
| `/new` | Start a fresh chat conversation |
| `/clear` | Delete the current conversation |
| `/model [name]` | Show or switch the active model |
| `/models` | Fetch available model IDs from provider |
| `/settings` | Open interactive settings dialog |
| `/upload <path>` | Attach a file from disk |
| `/tools` | List all active and disabled tools |
| `/plugins` | List all loaded plugins |
| `/memory` | Inspect remembered facts |
| `/remember key=val` | Save a persistent fact about yourself |
| `/forget <key>` | Delete a stored fact |
| `/search <query>` | Search all message history |
| `/export` | Export current chat to a clean Markdown file |
| `/stats` | View database record counts and file size |
| `/quit` | Exit application |

---

## 🏛️ Architecture

```
src/pyassist/
├── config.py            # Dataclass settings, JSON persistence, env overrides
├── paths.py             # Native platformdirs: config, data, plugins, exports
├── cli.py               # Argparse entry point & headless -p streaming mode
├── core/
│   ├── models.py        # Message, Conversation, ToolCall, Attachment, Usage
│   ├── storage.py       # Async SQLite (WAL, migrations, FTS5 full-text search)
│   ├── memory.py        # Long-term facts injected into system prompt
│   ├── llm.py           # OpenAI-compatible SSE streaming client
│   ├── engine.py        # Core agent loop: orchestrates streaming & tool loops
│   ├── events.py        # Decoupled dataclass events consumed by frontends
│   ├── attachments.py   # Inlines text files & encodes vision images to data URLs
│   └── bootstrap.py     # Clean dependency graph & application wiring
├── tools/
│   ├── base.py          # Tool ABC, ToolRegistry, ToolResult, JSON Schema
│   └── builtin.py       # Sandboxed FS, shell, HTTP, memory, datetime tools
├── plugins/
│   ├── api.py           # Public plugin API (registry, tools, slash commands)
│   └── loader.py        # Entry points & loose-file plugin discovery
└── ui/
    ├── app.py           # Textual App, event handlers, screen router
    ├── widgets.py       # Message bubbles, tool panels, composer
    ├── screens.py       # Settings, search, file picker, approval modals
    └── theme.py         # Minimalist dark theme CSS
```

---

## 🔌 Writing Plugins

Drop a `.py` file in your plugin folder (`pyassist --plugin-dir` displays the location):

```python
"""Word count and reverse plugin."""
from pyassist.plugins.api import PluginRegistry, ToolContext, ToolResult

__version__ = "1.0.0"

async def word_count(args: dict, ctx: ToolContext) -> ToolResult:
    text = str(args.get("text", ""))
    words = len(text.split())
    return ToolResult(
        content=f"{words} words, {len(text)} characters",
        display=f"Counted {words} words",
    )

def register(registry: PluginRegistry) -> None:
    registry.add_function_tool(
        word_count,
        name="word_count",
        description="Count words and characters in text.",
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string", "description": "Input text"}},
            "required": ["text"],
        },
    )
    registry.add_command("/hello", lambda args, ctx: f"Hello, {args or 'world'}!", "Say hello")
```

Run `pyassist --write-example-plugin` to generate an editable template in your plugin folder.

---

## 🧪 Testing

Run the automated test suite and linter:

```bash
# Run all tests
pytest -v

# Run the Ruff linter
ruff check .
```

---

## 📄 License

Distributed under the [MIT License](LICENSE).
