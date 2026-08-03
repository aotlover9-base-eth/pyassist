"""Tool registry, sandboxing and built-in behaviour."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pyassist.config import Config
from pyassist.tools.base import ToolContext, ToolRegistry, ToolResult
from pyassist.tools.builtin import (
    CurrentTimeTool,
    ListDirectoryTool,
    ReadFileTool,
    RecallTool,
    RememberTool,
    WorkspaceError,
    WriteFileTool,
    _resolve_in_workspace,
    _validate_public_url,
    build_default_registry,
)


@pytest.fixture
def ctx(config: Config, storage, workspace: Path) -> ToolContext:
    return ToolContext(
        config=config, storage=storage, workspace=workspace, conversation_id="c1"
    )


# ----------------------------------------------------------------------
# sandbox
# ----------------------------------------------------------------------
def test_resolve_allows_paths_inside_workspace(workspace: Path) -> None:
    resolved = _resolve_in_workspace("hello.txt", workspace)
    assert resolved == (workspace / "hello.txt").resolve()


@pytest.mark.parametrize(
    "escape",
    ["../outside.txt", "../../etc/passwd", "sub/../../outside.txt"],
)
def test_resolve_blocks_traversal(workspace: Path, escape: str) -> None:
    with pytest.raises(WorkspaceError):
        _resolve_in_workspace(escape, workspace)


def test_resolve_blocks_absolute_paths_outside(workspace: Path, tmp_path: Path) -> None:
    outsider = tmp_path / "elsewhere.txt"
    outsider.write_text("secret", encoding="utf-8")
    with pytest.raises(WorkspaceError):
        _resolve_in_workspace(str(outsider), workspace)


def test_resolve_rejects_empty() -> None:
    with pytest.raises(WorkspaceError):
        _resolve_in_workspace("", Path.cwd())


# ----------------------------------------------------------------------
# filesystem tools
# ----------------------------------------------------------------------
async def test_read_file(ctx: ToolContext) -> None:
    result = await ReadFileTool().run({"path": "hello.txt"}, ctx)
    assert result.ok
    assert "hello world" in result.content


async def test_read_file_missing(ctx: ToolContext) -> None:
    result = await ReadFileTool().run({"path": "nope.txt"}, ctx)
    assert not result.ok


async def test_read_file_outside_workspace_is_refused(ctx: ToolContext, tmp_path: Path) -> None:
    result = await ReadFileTool().run({"path": str(tmp_path / "x.txt")}, ctx)
    assert not result.ok
    assert "outside the workspace" in result.content


async def test_write_file(ctx: ToolContext, workspace: Path) -> None:
    result = await WriteFileTool().run({"path": "out/new.txt", "content": "data"}, ctx)
    assert result.ok
    assert (workspace / "out" / "new.txt").read_text(encoding="utf-8") == "data"


async def test_write_file_requires_approval() -> None:
    assert WriteFileTool().requires_approval is True


async def test_list_directory(ctx: ToolContext) -> None:
    result = await ListDirectoryTool().run({}, ctx)
    assert result.ok
    assert "hello.txt" in result.content


# ----------------------------------------------------------------------
# memory tools
# ----------------------------------------------------------------------
async def test_remember_then_recall(ctx: ToolContext) -> None:
    stored = await RememberTool().run({"key": "stack", "value": "python"}, ctx)
    assert stored.ok

    recalled = await RecallTool().run({"query": "stack"}, ctx)
    assert "python" in recalled.content


async def test_remember_rejects_empty(ctx: ToolContext) -> None:
    assert not (await RememberTool().run({"key": "", "value": ""}, ctx)).ok


async def test_current_time_returns_json(ctx: ToolContext) -> None:
    result = await CurrentTimeTool().run({}, ctx)
    assert "utc" in json.loads(result.content)


# ----------------------------------------------------------------------
# SSRF guard
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.com",
        "http://localhost:8080/admin",
        "http://127.0.0.1/",
        "not-a-url",
        "",
    ],
)
def test_url_validator_rejects_unsafe(url: str) -> None:
    assert _validate_public_url(url) is not None


# ----------------------------------------------------------------------
# registry
# ----------------------------------------------------------------------
def test_registry_respects_availability_flags() -> None:
    registry = build_default_registry()

    permissive = Config(allow_shell_tool=True, allow_network_tool=True, memory_enabled=True)
    assert "run_shell" in {t.name for t in registry.available(permissive)}

    locked = Config(allow_shell_tool=False, allow_network_tool=False, memory_enabled=False)
    names = {t.name for t in registry.available(locked)}
    assert "run_shell" not in names
    assert "http_request" not in names
    assert "remember" not in names
    assert "read_file" in names  # always available


def test_registry_schemas_are_openai_shaped() -> None:
    schemas = build_default_registry().schemas(Config())
    assert schemas
    for schema in schemas:
        assert schema["type"] == "function"
        function = schema["function"]
        assert function["name"] and function["description"]
        assert function["parameters"]["type"] == "object"


def test_registry_returns_no_schemas_when_tools_disabled() -> None:
    assert build_default_registry().schemas(Config(tools_enabled=False)) == []


def test_registry_rejects_duplicate_names() -> None:
    registry = ToolRegistry()
    registry.register(ReadFileTool())
    with pytest.raises(ValueError):
        registry.register(ReadFileTool())
    registry.register(ReadFileTool(), replace=True)  # explicit replace is fine


def test_tool_result_error_helper() -> None:
    result = ToolResult.error("boom")
    assert not result.ok
    assert "boom" in result.content
