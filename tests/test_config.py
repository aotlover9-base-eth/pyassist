"""Config loading, saving, coercion and masking."""

from __future__ import annotations

import json
from pathlib import Path

from pyassist import paths
from pyassist.config import Config, mask_secret


def test_defaults_are_sane() -> None:
    config = Config()
    assert config.base_url.startswith("https://")
    assert config.stream is True
    assert config.max_tool_iterations >= 1
    # Destructive capabilities must be opt-in.
    assert config.allow_shell_tool is False
    assert config.auto_approve_tools is False


def test_save_and_load_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    original = Config(api_key="sk-secret", model="gpt-4o", temperature=0.2)
    original.save(path)

    loaded = Config.load(path, use_env=False)
    assert loaded.api_key == "sk-secret"
    assert loaded.model == "gpt-4o"
    assert loaded.temperature == 0.2


def test_load_ignores_unknown_keys(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"model": "custom", "not_a_setting": 42}), encoding="utf-8")

    config = Config.load(path, use_env=False)
    assert config.model == "custom"
    assert not hasattr(config, "not_a_setting")


def test_corrupt_config_falls_back_to_defaults(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text("{ this is not json", encoding="utf-8")

    config = Config.load(path, use_env=False)
    assert config.model == Config().model


def test_from_dict_coerces_types() -> None:
    config = Config.from_dict({"temperature": "0.5", "max_tokens": "100", "stream": 0})
    assert config.temperature == 0.5
    assert config.max_tokens == 100
    assert config.stream is False


def test_from_dict_keeps_default_on_bad_value() -> None:
    config = Config.from_dict({"temperature": "not-a-number"})
    assert config.temperature == Config().temperature


def test_env_overrides_file() -> None:
    config = Config(api_key="from-file", base_url="https://file.example/v1")
    config.apply_env({"OPENAI_API_KEY": "from-env", "PYASSIST_MODEL": "env-model"})

    assert config.api_key == "from-env"
    assert config.model == "env-model"
    assert config.base_url == "https://file.example/v1"  # untouched


def test_is_configured_logic() -> None:
    assert not Config().is_configured  # OpenAI default, no key
    assert Config(api_key="sk-x").is_configured
    # Local servers need no key.
    assert Config(base_url="http://localhost:11434/v1").is_configured


def test_update_validates() -> None:
    config = Config()
    config.update(model="new-model", temperature="0.9")
    assert config.model == "new-model"
    assert config.temperature == 0.9


def test_redacted_hides_key() -> None:
    data = Config(api_key="sk-abcdefghijklmnop").redacted()
    assert "sk-abcdefghijklmnop" not in json.dumps(data)


def test_mask_secret() -> None:
    assert mask_secret("") == ""
    assert mask_secret("short") == "*****"  # too short to reveal anything
    masked = mask_secret("sk-abcdefghijklmnop")
    assert masked.startswith("sk-a")
    assert masked.endswith("mnop")


def test_urls_are_built_correctly() -> None:
    config = Config(base_url="https://api.example.com/v1/")
    assert config.chat_completions_url() == "https://api.example.com/v1/chat/completions"
    assert config.models_url() == "https://api.example.com/v1/models"


def test_anthropic_urls_and_provider_resolution() -> None:
    # Auto-detection from Anthropic URL
    cfg_auto = Config(base_url="https://api.anthropic.com/v1/")
    assert cfg_auto.resolved_provider == "anthropic"
    assert cfg_auto.anthropic_messages_url() == "https://api.anthropic.com/v1/messages"
    assert cfg_auto.models_url() == "https://api.anthropic.com/v1/models"

    # Auto-detection from root Anthropic URL
    cfg_root = Config(base_url="https://api.anthropic.com")
    assert cfg_root.resolved_provider == "anthropic"
    assert cfg_root.anthropic_messages_url() == "https://api.anthropic.com/v1/messages"
    assert cfg_root.anthropic_models_url() == "https://api.anthropic.com/v1/models"

    # Explicit provider overrides
    cfg_anthropic = Config(provider="anthropic")
    assert cfg_anthropic.resolved_provider == "anthropic"
    # When default OpenAI base_url is unchanged, points to Anthropic's official endpoint
    assert cfg_anthropic.anthropic_messages_url() == "https://api.anthropic.com/v1/messages"

    cfg_openai = Config(provider="openai", base_url="https://api.anthropic.com/v1/")
    assert cfg_openai.resolved_provider == "openai"


def test_anthropic_is_configured_requires_api_key() -> None:
    assert not Config(provider="anthropic", api_key="").is_configured
    assert not Config(base_url="https://api.anthropic.com/v1/", api_key="").is_configured
    assert Config(provider="anthropic", api_key="sk-ant-test").is_configured


def test_anthropic_env_overrides() -> None:
    cfg = Config()
    cfg.apply_env({
        "PYASSIST_PROVIDER": "anthropic",
        "ANTHROPIC_API_KEY": "sk-ant-env",
        "ANTHROPIC_MODEL": "claude-haiku-5-5",
    })
    assert cfg.provider == "anthropic"
    assert cfg.resolved_provider == "anthropic"
    assert cfg.api_key == "sk-ant-env"
    assert cfg.model == "claude-haiku-5-5"


def test_paths_respect_env_overrides() -> None:
    # isolated_dirs sets these; just confirm they're honoured.
    assert "config" in str(paths.config_dir())
    assert paths.config_file().name == "config.json"
    assert paths.database_file().suffix == ".sqlite3"
