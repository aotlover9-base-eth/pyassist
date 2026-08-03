# pyassist

A beautiful terminal AI assistant. Textual TUI, works with any OpenAI-compatible
API, streams responses, renders Markdown with syntax highlighting, calls tools,
remembers things locally in SQLite, and takes plugins.

```
┌ pyassist ─────────────────────────────── gpt-4o-mini ─┐
│ Chats            │  You                               │
│ ▸ Refactor auth  │  summarise pyproject.toml          │
│   SQL indexes    │                                    │
│   Rust vs Go     │  pyassist                          │
│                  │  Here's the gist:                  │
│                  │  ```toml                           │
│                  │  [project]                         │
│                  │  name = "pyassist"                 │
│                  │  ```                               │
│                  │  ⚙ read_file  ✓ 0.01s              │
├──────────────────┴────────────────────────────────────┤
│ > ask anything…                                       │
└───────────────────────────────────────────────────────┘
```

## Install

```bash
python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # macOS / Linux
pip install -e ".[dev]"
```

## Configure

Either press `F2` inside the app, or use the CLI:

```bash
pyassist --set api_key=sk-...
pyassist --set base_url=https://api.openai.com/v1
pyassist --set model=gpt-4o-mini
```

Environment variables take precedence over the config file, so this works too:

```bash
set OPENAI_API_KEY=sk-...          # Windows
export OPENAI_API_KEY=sk-...       # macOS / Linux
```

Any OpenAI-compatible server works. Point `base_url` at it:

| Provider   | base_url                          |
| ---------- | --------------------------------- |
| OpenAI     | `https://api.openai.com/v1`       |
| Groq       | `https://api.groq.com/openai/v1`  |
| OpenRouter | `https://openrouter.ai/api/v1`    |
| Ollama     | `http://localhost:11434/v1`       |
| LM Studio  | `http://localhost:1234/v1`        |
| vLLM       | `http://localhost:8000/v1`        |

> **Note on secrets.** The API key is stored in plain text in `config.json`
> (`chmod 600` where the OS supports it). If you would rather not persist it,
> use the `OPENAI_API_KEY` environment variable instead.

## Run

```bash
pyassist                       # launch the TUI
pyassist -p "explain WAL mode" # headless, prints to stdout
pyassist --list-tools          # show registered tools
pyassist --config              # show current settings (key masked)
```

## Keys

| Key            | Action                       |
| -------------- | ---------------------------- |
| `Enter`        | Send                         |
| `Ctrl+J`       | Newline in the composer      |
| `F2`           | Settings                     |
| `Ctrl+N`       | New chat                     |
| `Ctrl+B`       | Toggle sidebar               |
| `Ctrl+F`       | Search history               |
| `Ctrl+U`       | Attach a file                |
| `Ctrl+R`       | Regenerate last reply        |
| `Esc`          | Stop streaming               |
| `Ctrl+Q`       | Quit                         |

## Slash commands

`/help` `/new` `/clear` `/model [name]` `/models` `/settings` `/upload <path>`
`/tools` `/plugins` `/memory` `/remember key=value` `/forget key` `/search <q>`
`/export` `/stats` `/quit`

Plugins can add their own.

## Architecture

```
src/pyassist/
├── config.py            # dataclass settings, JSON persistence, env overrides
├── paths.py             # platform-correct config/data/plugin dirs
├── cli.py               # argparse entry point, headless mode
├── core/
│   ├── models.py        # Message, Conversation, ToolCall, Attachment, Usage
│   ├── storage.py       # async SQLite: WAL, migrations, FTS5 search
│   ├── memory.py        # long-term facts injected into the system prompt
│   ├── llm.py           # OpenAI-compatible SSE streaming client
│   ├── engine.py        # the agent loop
│   ├── events.py        # UI-agnostic engine events
│   ├── attachments.py   # file upload: text inlining, image data URLs
│   └── bootstrap.py     # dependency wiring
├── tools/
│   ├── base.py          # Tool ABC, ToolRegistry, ToolResult
│   └── builtin.py       # fs, shell, http, memory, time
├── plugins/
│   ├── api.py           # what plugin authors import
│   └── loader.py        # entry-point + directory discovery
└── ui/
    ├── app.py           # the Textual App, slash commands, key bindings
    ├── widgets.py       # message bubbles, tool panels, composer
    ├── screens.py       # settings, search, file picker, approval modals
    └── theme.py         # dark minimal CSS
```

### How the pieces fit

**`core/llm.py`** speaks the OpenAI wire protocol and nothing else. It parses SSE
into `StreamChunk` objects and tolerates the quirks real providers ship with
(bare JSON lines, usage-only chunks, `reasoning_content` from DeepSeek-style
models).

**`core/engine.py`** is the agent loop and the heart of the app:

1. persist the user message,
2. assemble `system prompt + memory + trimmed history`,
3. stream the reply, emitting `TextDelta` events as bytes arrive,
4. if the model asked for tools: check approval, run them, persist each result
   as a `tool` message, then loop back to step 2 so the model sees the output,
5. stop on a tool-free reply or after `max_tool_iterations`.

It emits plain dataclass events and never imports Textual, which is why the same
loop powers both the TUI and `-p` mode, and why it is straightforward to test
against a fake HTTP transport.

**`core/storage.py`** keeps everything in one SQLite file with WAL enabled so
writes don't block the UI. Conversations, messages, attachments and memories all
live there. An FTS5 virtual table backs `/search`, degrading to `LIKE` if the
local SQLite build lacks FTS5.

**`tools/`** defines a `Tool` ABC with a JSON Schema for its parameters, which
the registry turns into the `tools[]` request payload. Tools are sandboxed by
default: filesystem tools refuse paths outside the workspace, `run_shell` is
off until you enable it, and `http_request` rejects private-network addresses.
Anything marked `requires_approval` prompts you before it runs.

**`plugins/`** discovers both installed packages (via the
`pyassist.plugins` entry point group) and loose `.py` files in your plugin
directory. Plugins register tools, slash commands and startup hooks. A plugin
that raises on import is reported and skipped rather than taking down the app.

## Writing a plugin

Drop this in your plugin directory (`pyassist --plugin-dir` prints the path):

```python
"""Reverse text."""
from pyassist.plugins.api import PluginRegistry, ToolContext, ToolResult

__version__ = "1.0.0"

async def reverse(args: dict, ctx: ToolContext) -> ToolResult:
    return ToolResult(content=str(args.get("text", ""))[::-1])

def register(registry: PluginRegistry) -> None:
    registry.add_function_tool(
        reverse,
        name="reverse_text",
        description="Reverse a string.",
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    )
```

Run `pyassist --write-example-plugin` to generate a fuller starting point.

## Tests

```bash
.venv\Scripts\python.exe -m pytest
```

The suite covers config round-tripping, SQLite persistence and search, SSE
parsing (against a mocked transport, no network), the tool sandbox, history
trimming, the tool-calling loop end to end, and the TUI itself via Textual's
`run_test()` pilot.

## License

MIT
