# Getting Started with pyassist

A friendly, no-assumptions guide. Copy each command, press Enter, read what it says.

Everything here is for **Windows + Command Prompt (cmd)**, since that's what you're on.

---

## What is this thing?

`pyassist` is a chat assistant that lives in your terminal instead of a browser tab.
You type a question, it streams an answer back. It also remembers your chats, can read
your files, and can run small tools when you allow it.

It talks to any **OpenAI-compatible** API. That means it works with OpenAI, but also
with local models (Ollama, LM Studio, llama.cpp) or other providers — you just point it
at a different address. More on that in Step 5.

---

## Step 1 — Open a terminal in the project folder

Press `Win + R`, type `cmd`, press Enter. Then:

```cmd
cd c:\Users\nikhi\Downloads\pyassistent
```

Your prompt should now show that folder. You'll run everything from here.

---

## Step 2 — Install it

The project already has a virtual environment (a private box for its Python packages,
so it can't break anything else on your computer). Activate it:

```cmd
.venv\Scripts\activate
```

You'll see `(.venv)` appear at the start of your prompt. **That's how you know it worked.**
You need to do this every time you open a new terminal.

Now install the app itself:

```cmd
pip install -e .
```

The `-e` means "editable" — if you change the code, the change takes effect immediately,
no reinstall needed.

Check it worked:

```cmd
pyassist --version
```

You should see `pyassist 0.1.0`. If you get "not recognized", see Troubleshooting below.

---

## Step 3 — Give it an API key

This is the one thing it can't work without. An API key is a password that proves to the
AI provider that it's you, so they can bill you.

Get one from <https://platform.openai.com/api-keys> — it looks like `sk-proj-abc123...`

Then save it:

```cmd
pyassist --set api_key=sk-paste-your-real-key-here
```

This writes it to a config file on your machine so you only do it once.

> **Keep this key private.** Anyone who has it can spend your money. Don't paste it into
> chats, screenshots, or commit it to GitHub. If it leaks, delete it on the same page you
> made it and generate a new one.

Confirm it saved (the key is deliberately shown masked, like `sk-p…3f9`):

```cmd
pyassist --config
```

---

## Step 4 — Actually use it

```cmd
pyassist
```

The app opens. Type a message, press **Enter**, and watch the reply stream in.

### The keys you'll actually use

| Key | What it does |
| --- | --- |
| `Enter` | Send your message |
| `Ctrl+J` | New line *inside* your message (instead of sending) |
| `Esc` | Stop a reply that's still generating |
| `F1` | Help |
| `F2` | Settings |
| `Ctrl+N` | New chat |
| `Ctrl+F` | Search all your past chats |
| `Ctrl+U` | Attach a file |
| `Ctrl+B` | Show/hide the chat list on the left |
| `Ctrl+R` | Regenerate the last reply |
| `Ctrl+Q` | Quit |

### Slash commands

Type these into the message box, like `/help`:

| Command | What it does |
| --- | --- |
| `/help` | The full list |
| `/new` | Start a fresh chat |
| `/clear` | Delete the current chat |
| `/model` | Show which model you're using |
| `/model gpt-4o` | Switch to a different model |
| `/models` | List every model your provider offers |
| `/settings` | Open settings |
| `/upload notes.txt` | Attach a file by name |
| `/tools` | Show what tools are turned on |
| `/remember name=Nikhil` | Save a fact it should recall later |
| `/memory` | Show everything it's remembered |
| `/forget name` | Delete one saved fact |
| `/search docker` | Find past messages mentioning "docker" |
| `/export` | Save this chat as a Markdown file |
| `/quit` | Exit |

---

## Step 5 — Using a local / free model (optional)

Don't want to pay per message? Run a model on your own machine with
[Ollama](https://ollama.com), then point `pyassist` at it:

```cmd
pyassist --set base_url=http://localhost:11434/v1
pyassist --set api_key=ollama
pyassist --set model=llama3
```

The `api_key` there is a dummy value — local servers don't check it, but the app expects
*something* to be set.

To go back to OpenAI later:

```cmd
pyassist --set base_url=https://api.openai.com/v1
pyassist --set api_key=sk-your-real-key
pyassist --set model=gpt-4o-mini
```

---

## Step 6 — Asking questions without opening the app

Handy for quick one-offs and for piping into other commands:

```cmd
pyassist -p "explain what a virtual environment is, in two sentences"
```

The answer prints straight to your terminal and exits.

---

## Tools (the part that touches your computer)

The assistant can read files, list folders, do math, and so on. **It asks permission
before every single tool run**, and you can say no.

Two tools are **off by default** because they're the risky ones:

```cmd
pyassist --set allow_shell_tool=true      :: lets it run terminal commands
pyassist --set allow_network_tool=true    :: lets it fetch web pages
```

Only turn those on if you understand what you're agreeing to. The shell tool in
particular means an AI can run commands on your machine. Leave them off while you're
getting comfortable — everything else works fine without them.

See what's currently enabled:

```cmd
pyassist --list-tools
```

---

## Common settings

All of these use the same `pyassist --set key=value` pattern:

| Setting | Does what | Default |
| --- | --- | --- |
| `model` | Which AI model | `gpt-4o-mini` |
| `temperature` | 0 = focused and repetitive, 2 = wild | `0.7` |
| `max_tokens` | Cap on reply length (0 = provider decides) | `0` |
| `system_prompt` | Standing instructions for its personality | *(built-in)* |
| `stream` | Show text as it types vs. all at once | `true` |
| `tools_enabled` | Master switch for all tools | `true` |
| `auto_approve_tools` | Skip the permission prompt (careful) | `false` |
| `memory_enabled` | Let it remember facts between chats | `true` |
| `vision_enabled` | Allow sending images | `true` |

---

## Where your data lives

Everything is local — your chats never go anywhere except to the AI provider you chose.

```cmd
pyassist --config
```

That prints the paths. Chats live in a SQLite database file; you can back it up by
copying it. Deleting it wipes your history.

---

## Troubleshooting

**`'pyassist' is not recognized`**
The virtual environment isn't active. Run `.venv\Scripts\activate` — look for `(.venv)`
in your prompt.

**`'python' is not recognized`**
Python isn't installed or isn't on your PATH. Install from <https://python.org/downloads>
and tick **"Add Python to PATH"** during setup.

**"No API key configured"**
Step 3 didn't take. Run `pyassist --config` to check, then set it again.

**`401 Unauthorized` / `invalid_api_key`**
The key is wrong, expired, or has a stray space. Generate a fresh one and re-set it.

**`429` / "quota exceeded"**
Your provider account is out of credit. Check your billing page. Note that a free
signup and a paid API balance are separate things on OpenAI.

**It's slow or hangs on the first message**
Usually just the provider being slow. Press `Esc` to cancel and try again.

**Something else**
Run with `--debug` for verbose logs:
```cmd
pyassist --debug
```

---

## Running the tests

If you change the code and want to check you didn't break anything:

```cmd
pytest -q
```

All 91 should pass.

---

## Quick reference

```cmd
cd c:\Users\nikhi\Downloads\pyassistent   :: go to the project
.venv\Scripts\activate                    :: turn on the environment
pyassist                                  :: launch it
pyassist --config                         :: see your settings
pyassist --set model=gpt-4o               :: change a setting
pyassist -p "a quick question"            :: one-off question
pyassist --list-tools                     :: what tools exist
deactivate                                :: turn the environment off
```
