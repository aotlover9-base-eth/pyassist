"""Filesystem locations used by pyassist.

All paths are resolved through :mod:`platformdirs` so the app behaves natively
on Windows (``%APPDATA%``), macOS (``~/Library``) and Linux (XDG).

Every path can be overridden with an environment variable, which makes tests
and throwaway sandboxes trivial:

``PYASSIST_CONFIG_DIR``, ``PYASSIST_DATA_DIR``, ``PYASSIST_PLUGIN_DIR``
"""

from __future__ import annotations

import os
from pathlib import Path

from platformdirs import PlatformDirs

APP_NAME = "pyassist"

# appauthor=False matters on Windows: platformdirs otherwise nests
# <author>/<appname>, giving a redundant "pyassist\pyassist" path.
_dirs = PlatformDirs(appname=APP_NAME, appauthor=False, roaming=True)


def _env_path(var: str) -> Path | None:
    raw = os.environ.get(var)
    if not raw:
        return None
    # Stray surrounding whitespace is easy to introduce on Windows
    # (`set VAR=value && ...` keeps the trailing space) and would otherwise
    # create a subtly different directory.
    raw = raw.strip().strip('"')
    if not raw:
        return None
    return Path(raw).expanduser()


def config_dir() -> Path:
    """Directory holding ``config.json``."""
    return _env_path("PYASSIST_CONFIG_DIR") or Path(_dirs.user_config_dir)


def data_dir() -> Path:
    """Directory holding the SQLite database and exports."""
    return _env_path("PYASSIST_DATA_DIR") or Path(_dirs.user_data_dir)


def config_file() -> Path:
    return config_dir() / "config.json"


def database_file() -> Path:
    return data_dir() / "pyassist.sqlite3"


def plugin_dir() -> Path:
    """Directory scanned for single-file user plugins."""
    return _env_path("PYASSIST_PLUGIN_DIR") or (config_dir() / "plugins")


def log_file() -> Path:
    return data_dir() / "pyassist.log"


def export_dir() -> Path:
    return data_dir() / "exports"


def ensure_dirs() -> None:
    """Create every directory pyassist writes to. Safe to call repeatedly."""
    for path in (config_dir(), data_dir(), plugin_dir(), export_dir()):
        path.mkdir(parents=True, exist_ok=True)
