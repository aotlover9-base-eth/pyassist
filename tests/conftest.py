"""Shared fixtures.

Every test runs against a temporary config/data directory so the developer's
real ``~/.config/pyassist`` is never touched. The env vars are set at import
time (before :mod:`pyassist.paths` is used) via an autouse fixture.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pyassist.config import Config
from pyassist.core.storage import Storage


@pytest.fixture(autouse=True)
def isolated_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point every pyassist directory at a temp folder."""
    for var, sub in (
        ("PYASSIST_CONFIG_DIR", "config"),
        ("PYASSIST_DATA_DIR", "data"),
        ("PYASSIST_PLUGIN_DIR", "plugins"),
    ):
        target = tmp_path / sub
        target.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv(var, str(target))

    # Stop a developer's real key leaking into tests.
    for var in (
        "OPENAI_API_KEY",
        "PYASSIST_API_KEY",
        "OPENAI_BASE_URL",
        "PYASSIST_BASE_URL",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_BASE_URL",
        "PYASSIST_PROVIDER",
    ):
        monkeypatch.delenv(var, raising=False)

    return tmp_path


@pytest.fixture
def config() -> Config:
    """A configured, offline-safe config."""
    return Config(
        api_key="test-key",
        base_url="https://example.invalid/v1",
        model="test-model",
        memory_enabled=True,
    )


@pytest.fixture
async def storage(tmp_path: Path):
    """A connected Storage backed by a temp database."""
    store = Storage(tmp_path / "test.sqlite3")
    await store.connect()
    try:
        yield store
    finally:
        await store.close()


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    root.mkdir(exist_ok=True)
    (root / "hello.txt").write_text("hello world", encoding="utf-8")
    return root
