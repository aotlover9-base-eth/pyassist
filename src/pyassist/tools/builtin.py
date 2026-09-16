"""Built-in tools shipped with pyassist.

Each tool is deliberately small and defensive:

* ``read_file`` / ``write_file`` / ``list_directory`` are confined to the
  workspace by :func:`_resolve_in_workspace`, which blocks ``..`` traversal and
  symlink escapes.
* ``run_shell`` is disabled by default (``allow_shell_tool``) and always
  requires approval.
* ``http_request`` is gated on ``allow_network_tool`` and refuses non-HTTP(S)
  schemes and private-network hosts.
* ``remember`` / ``recall`` expose long-term memory to the model.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import socket
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from ..config import Config
from .base import Tool, ToolContext, ToolRegistry, ToolResult

MAX_OUTPUT_CHARS = 20_000


# ----------------------------------------------------------------------
# path sandboxing
# ----------------------------------------------------------------------
class WorkspaceError(Exception):
    """Raised when a path escapes the workspace."""


def _resolve_in_workspace(raw: str, workspace: Path) -> Path:
    """Resolve ``raw`` and guarantee it stays inside ``workspace``.

    ``Path.resolve()`` collapses ``..`` *and* follows symlinks, so comparing
    the resolved path against the resolved workspace defeats both traversal
    and symlink-escape attempts.
    """
    if not raw or not raw.strip():
        raise WorkspaceError("path must not be empty")

    root = workspace.resolve()
    candidate = Path(raw).expanduser()
    target = (candidate if candidate.is_absolute() else root / candidate).resolve()

    if target != root and root not in target.parents:
        raise WorkspaceError(
            f"path {raw!r} is outside the workspace ({root}). "
            "Only files under the workspace may be accessed."
        )
    return target


def _truncate(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n… [truncated, {len(text) - limit} more characters]"


# ----------------------------------------------------------------------
# filesystem
# ----------------------------------------------------------------------
class ReadFileTool(Tool):
    name = "read_file"
    description = (
        "Read a UTF-8 text file from the workspace. Use this to inspect source "
        "code, configuration or data before answering questions about it."
    )
    category = "filesystem"
    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Path relative to the workspace root.",
            },
            "max_lines": {
                "type": "integer",
                "description": "Optional cap on the number of lines returned.",
            },
        },
        "required": ["path"],
    }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        try:
            path = _resolve_in_workspace(str(args.get("path", "")), ctx.workspace)
        except WorkspaceError as exc:
            return ToolResult.error(str(exc))

        if not path.is_file():
            return ToolResult.error(f"{path.name} is not a file or does not exist")

        try:
            text = await asyncio.to_thread(path.read_text, encoding="utf-8", errors="replace")
        except OSError as exc:
            return ToolResult.error(f"could not read {path.name}: {exc}")

        max_lines = args.get("max_lines")
        if isinstance(max_lines, int) and max_lines > 0:
            lines = text.splitlines()
            if len(lines) > max_lines:
                text = "\n".join(lines[:max_lines]) + f"\n… [{len(lines) - max_lines} more lines]"

        await ctx.say(f"read {path.name}")
        return ToolResult(content=_truncate(text), display=f"Read `{path.name}`")


class WriteFileTool(Tool):
    name = "write_file"
    description = (
        "Create or overwrite a text file in the workspace. Parent directories "
        "are created automatically."
    )
    category = "filesystem"
    requires_approval = True
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path relative to the workspace root."},
            "content": {"type": "string", "description": "Full text content to write."},
        },
        "required": ["path", "content"],
    }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        try:
            path = _resolve_in_workspace(str(args.get("path", "")), ctx.workspace)
        except WorkspaceError as exc:
            return ToolResult.error(str(exc))

        content = str(args.get("content", ""))
        try:
            await asyncio.to_thread(path.parent.mkdir, parents=True, exist_ok=True)
            await asyncio.to_thread(path.write_text, content, encoding="utf-8")
        except OSError as exc:
            return ToolResult.error(f"could not write {path.name}: {exc}")

        await ctx.say(f"wrote {path.name}")
        return ToolResult(
            content=f"Wrote {len(content)} characters to {path}",
            display=f"Wrote `{path.name}` ({len(content)} chars)",
        )


class ListDirectoryTool(Tool):
    name = "list_directory"
    description = "List files and folders at a path inside the workspace."
    category = "filesystem"
    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Directory relative to the workspace root. Defaults to root.",
            }
        },
    }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        raw = str(args.get("path") or ".")
        try:
            path = _resolve_in_workspace(raw, ctx.workspace)
        except WorkspaceError as exc:
            return ToolResult.error(str(exc))

        if not path.is_dir():
            return ToolResult.error(f"{raw} is not a directory")

        entries: list[str] = []
        try:
            for item in sorted(path.iterdir(), key=lambda p: (p.is_file(), p.name.lower())):
                if item.name.startswith("."):
                    continue
                if item.is_dir():
                    entries.append(f"{item.name}/")
                else:
                    entries.append(f"{item.name}  ({item.stat().st_size} bytes)")
        except OSError as exc:
            return ToolResult.error(f"could not list {raw}: {exc}")

        listing = "\n".join(entries) or "(empty directory)"
        return ToolResult(content=_truncate(listing), display=f"Listed `{raw}` ({len(entries)})")


# ----------------------------------------------------------------------
# shell
# ----------------------------------------------------------------------
class ShellTool(Tool):
    name = "run_shell"
    description = (
        "Run a shell command in the workspace and return its stdout/stderr. "
        "Use only when necessary; prefer dedicated tools."
    )
    category = "system"
    requires_approval = True
    parameters = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "The command line to execute."},
            "timeout": {
                "type": "integer",
                "description": "Seconds before the command is killed (default 30).",
            },
        },
        "required": ["command"],
    }

    def is_available(self, config: Config) -> bool:
        return config.allow_shell_tool

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        command = str(args.get("command", "")).strip()
        if not command:
            return ToolResult.error("command must not be empty")

        timeout = args.get("timeout")
        timeout = float(timeout) if isinstance(timeout, (int, float)) and timeout > 0 else 30.0

        await ctx.say(f"running: {command[:60]}")
        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(ctx.workspace),
            )
        except OSError as exc:
            return ToolResult.error(f"could not start command: {exc}")

        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return ToolResult.error(f"command timed out after {timeout:.0f}s")

        out = stdout.decode("utf-8", "replace").strip()
        err = stderr.decode("utf-8", "replace").strip()

        parts = [f"exit code: {proc.returncode}"]
        if out:
            parts.append(f"stdout:\n{out}")
        if err:
            parts.append(f"stderr:\n{err}")

        return ToolResult(
            content=_truncate("\n\n".join(parts)),
            ok=proc.returncode == 0,
            display=f"`{command}` → exit {proc.returncode}",
        )


# ----------------------------------------------------------------------
# network
# ----------------------------------------------------------------------
class HttpRequestTool(Tool):
    name = "http_request"
    description = (
        "Fetch a public HTTP(S) URL and return the response body. Useful for "
        "reading documentation pages or calling JSON APIs."
    )
    category = "network"
    parameters = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Absolute http:// or https:// URL."},
            "method": {
                "type": "string",
                "enum": ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"],
                "description": "HTTP method (default GET).",
            },
            "headers": {"type": "object", "description": "Optional request headers."},
            "body": {"type": "string", "description": "Optional request body."},
        },
        "required": ["url"],
    }

    def is_available(self, config: Config) -> bool:
        return config.allow_network_tool

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        url = str(args.get("url", "")).strip()
        error = _validate_public_url(url)
        if error:
            return ToolResult.error(error)

        method = str(args.get("method") or "GET").upper()
        headers = args.get("headers")
        headers = {str(k): str(v) for k, v in headers.items()} if isinstance(headers, dict) else {}
        body = args.get("body")

        await ctx.say(f"{method} {url[:60]}")
        try:
            async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
                response = await client.request(
                    method, url, headers=headers, content=body if body else None
                )
        except httpx.HTTPError as exc:
            return ToolResult.error(f"request failed: {exc}")

        text = response.text
        summary = f"HTTP {response.status_code} {response.reason_phrase}\n\n{text}"
        return ToolResult(
            content=_truncate(summary),
            ok=response.status_code < 400,
            display=f"{method} {url} → {response.status_code}",
        )


def _validate_public_url(url: str) -> str | None:
    """Reject non-HTTP schemes and private/loopback destinations (SSRF guard)."""
    if not url:
        return "url must not be empty"

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return "only http:// and https:// URLs are allowed"
    if not parsed.hostname:
        return "url has no host"

    host = parsed.hostname
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return f"could not resolve host {host!r}"

    for info in infos:
        address = info[4][0]
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            continue
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            return f"refusing to reach private address {address} ({host})"
    return None


# ----------------------------------------------------------------------
# memory
# ----------------------------------------------------------------------
class RememberTool(Tool):
    name = "remember"
    description = (
        "Store a durable fact about the user so it can be recalled in future "
        "conversations, e.g. their name, stack, timezone or preferences."
    )
    category = "memory"
    parameters = {
        "type": "object",
        "properties": {
            "key": {
                "type": "string",
                "description": "Short identifier, e.g. 'timezone' or 'preferred_language'.",
            },
            "value": {"type": "string", "description": "The fact to remember."},
        },
        "required": ["key", "value"],
    }

    def is_available(self, config: Config) -> bool:
        return config.memory_enabled

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        key = str(args.get("key", "")).strip()
        value = str(args.get("value", "")).strip()
        if not key or not value:
            return ToolResult.error("both key and value are required")

        await ctx.storage.upsert_memory(key, value, conversation_id=ctx.conversation_id)
        await ctx.say(f"remembered {key}")
        return ToolResult(content=f"Stored memory {key!r}.", display=f"Remembered **{key}**")


class RecallTool(Tool):
    name = "recall"
    description = (
        "Search previously stored facts about the user. Call this when the "
        "user refers to something they told you earlier."
    )
    category = "memory"
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search term. Omit to list everything remembered.",
            }
        },
    }

    def is_available(self, config: Config) -> bool:
        return config.memory_enabled

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        query = str(args.get("query") or "").strip()
        memories = (
            await ctx.storage.search_memories(query)
            if query
            else await ctx.storage.list_memories(limit=50)
        )
        if not memories:
            return ToolResult(content="No matching memories are stored.")

        payload = {mem.key: mem.value for mem in memories}
        return ToolResult(
            content=json.dumps(payload, indent=2),
            display=f"Recalled {len(memories)} memor{'y' if len(memories) == 1 else 'ies'}",
        )


# ----------------------------------------------------------------------
# misc
# ----------------------------------------------------------------------
class CurrentTimeTool(Tool):
    name = "current_time"
    description = (
        "Get the current local and UTC date/time. Call this whenever the "
        "answer depends on today's date - never guess it."
    )
    category = "general"
    parameters = {"type": "object", "properties": {}}

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        local = datetime.now().astimezone()
        payload = {
            "local": local.strftime("%Y-%m-%d %H:%M:%S %Z"),
            "utc": datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC"),
            "timestamp": int(time.time()),
        }
        return ToolResult(
            content=json.dumps(payload, indent=2),
            display=f"Current time: {payload['local']}",
        )


#: Instantiated once; tools are stateless so sharing them is safe.
BUILTIN_TOOLS: tuple[Tool, ...] = (
    ReadFileTool(),
    WriteFileTool(),
    ListDirectoryTool(),
    ShellTool(),
    HttpRequestTool(),
    RememberTool(),
    RecallTool(),
    CurrentTimeTool(),
)


def build_default_registry() -> ToolRegistry:
    """A registry pre-loaded with every built-in tool."""
    registry = ToolRegistry()
    for tool in BUILTIN_TOOLS:
        registry.register(tool)
    return registry
