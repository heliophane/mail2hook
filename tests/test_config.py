"""Tests for configuration loading and validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from mail2hook.config import (
    DEFAULT_DISCORD_USERNAME,
    DEFAULT_MAX_BODY_CHARS,
    ConfigError,
    load_config,
    validate_webhook_url,
)

VALID_WEBHOOK_URL = (
    "https://discord.com/api/webhooks/123456789012345678/abcdefghijklmnopqrstuvwxyz0123456789"
)
VALID_CANARY_WEBHOOK_URL = (
    "https://canary.discord.com/api/webhooks/123456789012345678/token-with-hyphens_and_underscore"
)


def test_validate_webhook_url_success() -> None:
    assert validate_webhook_url(VALID_WEBHOOK_URL) == VALID_WEBHOOK_URL
    assert validate_webhook_url(VALID_CANARY_WEBHOOK_URL) == VALID_CANARY_WEBHOOK_URL


def test_validate_webhook_url_missing() -> None:
    with pytest.raises(ConfigError, match="Missing required environment variable"):
        validate_webhook_url(None)
    with pytest.raises(ConfigError, match="Missing required environment variable"):
        validate_webhook_url("   ")


def test_validate_webhook_url_malformed() -> None:
    invalid_urls = [
        "https://example.com/api/webhooks/123/abc",
        "http://discord.com/api/webhooks/123/abc",
        "https://discord.com/not-webhooks/123/abc",
        "not-a-url",
    ]
    for url in invalid_urls:
        with pytest.raises(ConfigError, match="Malformed DISCORD_WEBHOOK_URL"):
            validate_webhook_url(url)


def test_load_config_minimal_defaults() -> None:
    cfg = load_config(environ={"DISCORD_WEBHOOK_URL": VALID_WEBHOOK_URL})
    assert cfg.discord_webhook_url == VALID_WEBHOOK_URL
    assert cfg.filter_senders == []
    assert cfg.filter_subject_keywords == []
    assert cfg.max_body_chars == DEFAULT_MAX_BODY_CHARS
    assert cfg.discord_username == DEFAULT_DISCORD_USERNAME


def test_load_config_all_fields() -> None:
    environ = {
        "DISCORD_WEBHOOK_URL": VALID_WEBHOOK_URL,
        "FILTER_SENDERS": "root@example.com, alerts@example.com, ",
        "FILTER_SUBJECT_KEYWORDS": "CRON, Unattended-Upgrades",
        "MAX_BODY_CHARS": "2000",
        "DISCORD_USERNAME": "ServerAlerts",
    }
    cfg = load_config(environ=environ)
    assert cfg.discord_webhook_url == VALID_WEBHOOK_URL
    assert cfg.filter_senders == ["root@example.com", "alerts@example.com"]
    assert cfg.filter_subject_keywords == ["cron", "unattended-upgrades"]
    assert cfg.max_body_chars == 2000
    assert cfg.discord_username == "ServerAlerts"


def test_load_config_invalid_max_body_chars() -> None:
    with pytest.raises(ConfigError, match="Invalid MAX_BODY_CHARS 'foo'"):
        load_config(environ={"DISCORD_WEBHOOK_URL": VALID_WEBHOOK_URL, "MAX_BODY_CHARS": "foo"})

    with pytest.raises(ConfigError, match="Invalid MAX_BODY_CHARS '-5'"):
        load_config(environ={"DISCORD_WEBHOOK_URL": VALID_WEBHOOK_URL, "MAX_BODY_CHARS": "-5"})

    with pytest.raises(ConfigError, match="Invalid MAX_BODY_CHARS '0'"):
        load_config(environ={"DISCORD_WEBHOOK_URL": VALID_WEBHOOK_URL, "MAX_BODY_CHARS": "0"})


def test_load_config_from_file(tmp_path: Path) -> None:
    env_file = tmp_path / "custom_env"
    env_file.write_text(
        f"DISCORD_WEBHOOK_URL={VALID_WEBHOOK_URL}\n"
        "DISCORD_USERNAME=FileBot\n"
        "FILTER_SENDERS=cron@example.com\n"
    )
    # Using load_config with custom file path, empty environ so loaded vars take effect
    cfg = load_config(env_file=env_file, environ={})
    assert cfg.discord_username == "FileBot"
    assert cfg.filter_senders == ["cron@example.com"]


def test_load_config_permission_error(tmp_path: Path) -> None:
    env_file = tmp_path / "inaccessible_env"
    env_file.write_text(f"DISCORD_WEBHOOK_URL={VALID_WEBHOOK_URL}\n")
    env_file.chmod(0o000)

    try:
        with pytest.raises(ConfigError, match="Permission denied reading configuration file"):
            load_config(env_file=env_file, environ={})
    finally:
        # Restore permissions so pytest cleanup can delete temp dir
        env_file.chmod(0o644)


def test_load_config_bare_webhook_url(tmp_path: Path) -> None:
    env_file = tmp_path / "bare_env"
    env_file.write_text(f"{VALID_WEBHOOK_URL}\n")
    cfg = load_config(env_file=env_file, environ={})
    assert cfg.discord_webhook_url == VALID_WEBHOOK_URL


def test_load_config_webhook_alias() -> None:
    cfg1 = load_config(environ={"WEBHOOK_URL": VALID_WEBHOOK_URL})
    assert cfg1.discord_webhook_url == VALID_WEBHOOK_URL

    cfg2 = load_config(environ={"DISCORD_WEBHOOK": VALID_WEBHOOK_URL})
    assert cfg2.discord_webhook_url == VALID_WEBHOOK_URL
