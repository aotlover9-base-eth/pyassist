"""Configuration model and persistence.

Design notes
------------
* A plain ``dataclass`` is the single source of truth for defaults, so adding a
  setting means touching exactly one place.
* Loading is *forgiving*: unknown keys are ignored and a corrupt config file
  falls back to defaults instead of crashing the app on startup.
* Environment variables win over the file, which is the behaviour people expect
  from CLI tools (``OPENAI_API_KEY`` just works).
* The API key is never returned by :meth:`Config.redacted`, which is what the
  UI and logs use.
"""

from __future__ import annotations

import contextlib
import json
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

from . import paths

DEFAULT_SYSTEM_PROMPT = (
    "You are pyassist, a concise and capable assistant running inside a terminal.\n"
    "Format answers in GitHub-flavoured Markdown and always tag fenced code blocks "
    "with their language.\n"
    "Prefer short, direct answers. Use the provided tools when they let you give a "
    "factual answer instead of guessing, and explain what you did afterwards."
)

#: Environment overrides applied on top of the config file, in ``(env var, field)`` form.
_ENV_OVERRIDES: tuple[tuple[str, str], ...] = (
    ("PYASSIST_PROVIDER", "provider"),
    ("PYASSIST_API_KEY", "api_key"),
    ("OPENAI_API_KEY", "api_key"),
    ("ANTHROPIC_API_KEY", "api_key"),
    ("PYASSIST_BASE_URL", "base_url"),
    ("OPENAI_BASE_URL", "base_url"),
    ("ANTHROPIC_BASE_URL", "base_url"),
    ("PYASSIST_MODEL", "model"),
    ("ANTHROPIC_MODEL", "model"),
)


@dataclass(slots=True)
class Config:
    """User-facing settings for pyassist."""

    # --- connection -----------------------------------------------------
    provider: str = "auto"
    base_url: str = "https://api.openai.com/v1"
    api_key: str = ""
    model: str = "gpt-4o-mini"
    request_timeout: float = 120.0
    extra_headers: dict[str, str] = field(default_factory=dict)
    anthropic_version: str = "2023-06-01"

    # --- generation -----------------------------------------------------
    system_prompt: str = DEFAULT_SYSTEM_PROMPT
    temperature: float = 0.7
    max_tokens: int = 0  # 0 => let the server decide
    stream: bool = True

    # --- agent behaviour ------------------------------------------------
    tools_enabled: bool = True
    auto_approve_tools: bool = False
    max_tool_iterations: int = 6
    allow_shell_tool: bool = False
    allow_network_tool: bool = True

    # --- memory / history -----------------------------------------------
    memory_enabled: bool = True
    memory_context_limit: int = 12
    history_char_budget: int = 24_000

    # --- attachments ----------------------------------------------------
    max_attachment_bytes: int = 256 * 1024
    vision_enabled: bool = True

    # --- ui ---------------------------------------------------------------
    theme: str = "pyassist-dark"
    show_sidebar: bool = True

    # ------------------------------------------------------------------
    # (de)serialisation
    # ------------------------------------------------------------------
    @classmethod
    def _field_names(cls) -> set[str]:
        return {f.name for f in fields(cls)}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Config:
        """Build a config from a dict, ignoring unknown/ill-typed values."""
        known = cls._field_names()
        clean: dict[str, Any] = {}
        defaults = cls()
        for key, value in data.items():
            if key not in known or value is None:
                continue
            expected = type(getattr(defaults, key))
            try:
                if expected is bool:
                    clean[key] = bool(value)
                elif expected is int:
                    clean[key] = int(value)
                elif expected is float:
                    clean[key] = float(value)
                elif expected is dict:
                    clean[key] = dict(value)
                else:
                    clean[key] = str(value)
            except (TypeError, ValueError):
                continue  # keep the default rather than exploding
        return cls(**clean)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def redacted(self) -> dict[str, Any]:
        """Config as a dict with the API key masked - safe for logs and the UI."""
        data = self.to_dict()
        data["api_key"] = mask_secret(self.api_key)
        return data

    # ------------------------------------------------------------------
    # persistence
    # ------------------------------------------------------------------
    @classmethod
    def load(cls, path: Path | None = None, *, use_env: bool = True) -> Config:
        """Read config from disk, then layer environment overrides on top."""
        path = path or paths.config_file()
        config = cls()
        if path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    config = cls.from_dict(raw)
            except (OSError, json.JSONDecodeError):
                # A broken config must never prevent the app from starting.
                config = cls()
        if use_env:
            config.apply_env()
        return config

    def apply_env(self, environ: dict[str, str] | None = None) -> None:
        """Apply ``PYASSIST_*`` / ``OPENAI_*`` / ``ANTHROPIC_*`` overrides."""
        env = environ if environ is not None else dict(os.environ)

        if prov := env.get("PYASSIST_PROVIDER"):
            self.provider = prov

        effective = self.resolved_provider

        # Base URL
        base = env.get("PYASSIST_BASE_URL")
        if not base:
            if effective == "anthropic":
                base = env.get("ANTHROPIC_BASE_URL") or env.get("OPENAI_BASE_URL")
            else:
                base = env.get("OPENAI_BASE_URL") or env.get("ANTHROPIC_BASE_URL")
        if base:
            self.base_url = base

        # API key
        key = env.get("PYASSIST_API_KEY")
        if not key:
            if effective == "anthropic":
                key = env.get("ANTHROPIC_API_KEY") or env.get("OPENAI_API_KEY")
            else:
                key = env.get("OPENAI_API_KEY") or env.get("ANTHROPIC_API_KEY")
        if key:
            self.api_key = key

        # Model
        m = env.get("PYASSIST_MODEL")
        if not m and effective == "anthropic":
            m = env.get("ANTHROPIC_MODEL")
        if m:
            self.model = m

    def save(self, path: Path | None = None) -> Path:
        """Atomically persist the config with owner-only permissions."""
        path = path or paths.config_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self.to_dict(), indent=2, ensure_ascii=False)

        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, path)  # atomic on POSIX and Windows
        _restrict_permissions(path)
        return path

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def update(self, **changes: Any) -> None:
        """Apply validated changes in place (used by the settings screen)."""
        merged = self.to_dict() | changes
        fresh = Config.from_dict(merged)
        for f in fields(Config):
            setattr(self, f.name, getattr(fresh, f.name))

    @property
    def resolved_provider(self) -> str:
        """Return 'anthropic' or 'openai' based on explicit setting or URL."""
        prov = (self.provider or "auto").strip().lower()
        if prov in ("anthropic", "claude"):
            return "anthropic"
        if prov in ("openai", "openai-compatible"):
            return "openai"
        if "anthropic.com" in self.base_url.lower():
            return "anthropic"
        return "openai"

    @property
    def is_configured(self) -> bool:
        """True when we have enough information to attempt a request.

        Local servers (Ollama, llama.cpp, LM Studio, vLLM) usually need no key,
        so a non-default base URL alone counts as configured. Anthropic and OpenAI
        endpoints require an API key.
        """
        if self.api_key.strip():
            return True
        if self.resolved_provider == "anthropic":
            return False
        return "api.openai.com" not in self.base_url

    def chat_completions_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/chat/completions"

    def models_url(self) -> str:
        if self.resolved_provider == "anthropic":
            return self.anthropic_models_url()
        return f"{self.base_url.rstrip('/')}/models"

    def anthropic_messages_url(self) -> str:
        base = self.base_url.rstrip("/")
        if "api.openai.com" in base:
            return "https://api.anthropic.com/v1/messages"
        if base.endswith("/v1"):
            return f"{base}/messages"
        return f"{base}/v1/messages"

    def anthropic_models_url(self) -> str:
        base = self.base_url.rstrip("/")
        if "api.openai.com" in base:
            return "https://api.anthropic.com/v1/models"
        if base.endswith("/v1"):
            return f"{base}/models"
        return f"{base}/v1/models"


def mask_secret(secret: str, *, visible: int = 4) -> str:
    """``sk-ABCD...WXYZ`` style masking; never reveals a short key."""
    if not secret:
        return ""
    if len(secret) <= visible * 2:
        return "*" * len(secret)
    return f"{secret[:visible]}{'*' * 6}{secret[-visible:]}"


def _restrict_permissions(path: Path) -> None:
    """Best-effort ``chmod 600``. A no-op on Windows, which is fine."""
    with contextlib.suppress(OSError):
        path.chmod(0o600)
