# 🤖 pyassist

<p align="center">
  <a href="https://github.com/aotlover9-base-eth/pyassist/actions"><img src="https://github.com/aotlover9-base-eth/pyassist/actions/workflows/ci.yml/badge.svg" alt="CI Status" /></a>
  <img src="https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13%20%7C%203.14-3776AB.svg?logo=python&logoColor=white" alt="Python Versions" />
  <a href="https://opensource.org/licenses/MIT"><img src="https://img.shields.io/badge/License-MIT-yellow.svg" alt="License: MIT" /></a>
  <a href="https://github.com/astral-sh/ruff"><img src="https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json" alt="Ruff Code Style" /></a>
  <a href="https://github.com/Textualize/textual"><img src="https://img.shields.io/badge/built%20with-Textual-00d4aa.svg" alt="Built with Textual" /></a>
</p>

> **Say goodbye to 50 open browser tabs.** Meet `pyassist`, your smart, cozy terminal companion that streams answers in real-time, remembers who you are, and helps you get stuff done without ever leaving your keyboard. 🛋️✨

```text
┌ pyassist ────────────────────────────────────────── claude-haiku-5-5 ─┐
│ Chats            │  You                                               │
│ ▸ Fix my regex   │  summarise pyproject.toml                          │
│   Movie ideas    │                                                    │
│   Rust vs Go     │  pyassist                                          │
│                  │  Here's the quick scoop:                           │
│                  │  ```toml                                           │
│                  │  [project]                                         │
│                  │  name = "pyassist"                                 │
│                  │  ```                                               │
│                  │  ⚙ read_file  ✓ 0.01s                              │
├──────────────────┴────────────────────────────────────────────────────┤
│ > ask anything… (Ctrl+U to attach, F2 for settings)                   │
└───────────────────────────────────────────────────────────────────────┘
```

---

## 🌟 Why You'll Love It

- 🎨 **Gorgeous & Cozy**: A split-screen terminal interface that looks like it belongs in a sci-fi movie. Smooth streaming, syntax-highlighted code, and zero clutter.
- 🧠 **Bring Your Favorite Brain**: Speaks fluent **Anthropic Claude** (Haiku, Sonnet, Opus) and **OpenAI** (GPT-4o, o3-mini), plus lightning-fast **Groq**, **OpenRouter**, or 100% free offline models like **Ollama** and **LM Studio**.
- 🦾 **Hands-On Helper Tools**: Need to peek at a file, edit code, or check the system clock? It runs tools safely on your machine and *always* asks your permission before doing anything risky.
- 🐘 **Memory Like an Elephant**: Teach it your favorite tech stack, preferences, or nicknames with `/remember`. It brings them up naturally when you need them.
- 🔍 **Instant Search**: Can't remember what you solved last Tuesday? Hit `Ctrl+F` and search your entire conversation history in a blink.
- 📸 **Look at This Picture**: Drag in screenshots, diagrams, or code files (`Ctrl+U`), and vision models will break them down for you.
- ⚡ **Lightning Fast CLI**: Too busy to launch the app? Just ask from your command line: `pyassist -p "explain recursion like I'm five"`.

---

## 🚀 Quickstart (Under 60 Seconds!)

Requires Python **3.11+**.

```bash
# 1. Grab the repository
git clone https://github.com/aotlover9-base-eth/pyassist.git
cd pyassist

# 2. Setup your virtual environment
python3 -m venv .venv
source .venv/bin/activate       # On Windows: .venv\Scripts\activate

# 3. Install in one go
pip install -e ".[dev]"

# 4. Jump right in!
pyassist
```

---

## 🤖 Pick Your AI Brain

Setting up your assistant takes just one command. Choose your favorite provider:

### Option A: Anthropic Claude (Recommended) 🧡

Talk to Claude natively using your Anthropic key:

```bash
pyassist --set provider=anthropic
pyassist --set api_key=sk-ant-api03-...
pyassist --set model=claude-haiku-5-5
```

*(You can also simply `export ANTHROPIC_API_KEY=sk-ant-...` in your terminal!)*

### Option B: OpenAI / ChatGPT 💚

```bash
pyassist --set provider=openai
pyassist --set api_key=sk-...
pyassist --set model=gpt-4o-mini
```

### Option C: 100% Free & Offline with Ollama 🦙

No API keys? No problem. Run models locally on your own hardware:

```bash
# Make sure Ollama is running, then:
pyassist --set provider=openai
pyassist --set base_url=http://localhost:11434/v1
pyassist --set model=llama3.2
```

> 🪄 **Smart Auto-Detection**: If your `base_url` points to `api.anthropic.com`, `pyassist` automatically knows to use Claude. If it points anywhere else, it smoothly speaks OpenAI!

---

## 🎮 Cheat Sheet

### Essential Hotkeys

| Key | What it does |
| :--- | :--- |
| `Enter` | Send message 🚀 |
| `Ctrl + J` | Add a new line in the text box |
| `F2` | Open Settings (tweak models, temperature & keys) |
| `Ctrl + N` | Start a fresh chat |
| `Ctrl + F` | Search through old messages |
| `Ctrl + U` | Attach a file or photo |
| `Ctrl + B` | Hide / show the conversation sidebar |
| `Ctrl + R` | Re-roll / regenerate the last answer |
| `Esc` | Stop generating right now / close modal |
| `Ctrl + Q` | Quit and go back to your shell |

### Fun Slash Commands

Just type these right into the chat bar:

- `/models` — See all the cool models available from your provider
- `/model claude-sonnet-5-5` — Switch models on the fly
- `/remember I love dark mode and TypeScript` — Teach pyassist a permanent fact
- `/memory` — Inspect what pyassist remembers about you
- `/forget <key>` — Erase a remembered fact
- `/export` — Save your chat as a beautiful Markdown file
- `/tools` — See the tools pyassist can use to help you
- `/clear` — Wipe the current chat canvas clean

---

## ⚡ Quick One-Liners (Headless Mode)

Don't want to open the full UI? Use `pyassist` as a pipeable superpower in your terminal:

```bash
# Quick question
pyassist -p "What's the command to find large files on Linux?"

# Inspect code with automated tool approval
pyassist -p "Read pyproject.toml and tell me what dependencies we use" -y

# Check your current config
pyassist --config
```

---

## 🧩 Build Your Own Plugins in 5 Minutes!

Want to teach `pyassist` custom tricks? Just drop a python file into your plugins folder (`pyassist --plugin-dir`):

```python
from pyassist.plugins.api import PluginRegistry, ToolContext, ToolResult

async def roll_dice(args: dict, ctx: ToolContext) -> ToolResult:
    import random
    num = random.randint(1, 6)
    return ToolResult(content=f"You rolled a 🎲 {num}!")

def register(registry: PluginRegistry) -> None:
    registry.add_function_tool(
        roll_dice,
        name="roll_dice",
        description="Rolls a 6-sided dice.",
    )
    registry.add_command("/roll", lambda args, ctx: "Rolling the dice... 🎲", "Roll dice")
```

Restart `pyassist`, type `/roll`, and watch the magic happen!

---

## 🧪 Testing & Hacking

We keep things rock-solid. Run the test suite anytime:

```bash
# Run 115+ automated tests
pytest

# Fast linting with Ruff
ruff check .
```

---

## 📄 License

Built with love and released under the [MIT License](LICENSE). Feel free to fork, hack, and make it your own!
