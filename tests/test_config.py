"""Tests for configuration loading and validation."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from mail2hook.config import (
    DEFAULT_DISCORD_USERNAME,
    DEFAULT_MAX_BODY_CHARS,
    ConfigError,
    load_config,
    validate_discord_username,
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
    assert validate_webhook_url(f"  {VALID_WEBHOOK_URL}/  ") == VALID_WEBHOOK_URL
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
        "SERVER_NAME": "server01-prod",
    }
    cfg = load_config(environ=environ)
    assert cfg.discord_webhook_url == VALID_WEBHOOK_URL
    assert cfg.filter_senders == ["root@example.com", "alerts@example.com"]
    assert cfg.filter_subject_keywords == ["cron", "unattended-upgrades"]
    assert cfg.max_body_chars == 2000
    assert cfg.discord_username == "ServerAlerts"
    assert cfg.server_name == "server01-prod"


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


def test_load_config_quotes_comments_and_export(tmp_path: Path) -> None:
    env_file = tmp_path / "quoted_env"
    env_file.write_text(
        "# Leading comment\n"
        f'export DISCORD_WEBHOOK_URL="{VALID_WEBHOOK_URL}"\n'
        "DISCORD_USERNAME='QuotedBot'\n"
        "SERVER_NAME=web01 # inline comment\n"
    )
    cfg = load_config(env_file=env_file, environ={})
    assert cfg.discord_webhook_url == VALID_WEBHOOK_URL
    assert cfg.discord_username == "QuotedBot"
    assert cfg.server_name == "web01"


def test_load_config_directory_eacces_surfaces_permission_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("root bypasses DAC directory permissions")

    locked_dir = tmp_path / "locked_dir"
    locked_dir.mkdir(mode=0o755)
    env_file = locked_dir / "env"
    env_file.write_text(f"DISCORD_WEBHOOK_URL={VALID_WEBHOOK_URL}\n")
    env_file.chmod(0o640)
    locked_dir.chmod(0o000)

    try:
        with pytest.raises(ConfigError, match="Permission denied reading configuration file"):
            load_config(env_file=env_file, environ={})

        monkeypatch.setattr("mail2hook.config.DEFAULT_SYSTEM_ENV_PATH", env_file)
        with pytest.raises(ConfigError, match="Permission denied reading configuration file"):
            load_config(load_default_files=True, environ={})

        cfg = load_config(
            load_default_files=True,
            environ={"DISCORD_WEBHOOK_URL": VALID_WEBHOOK_URL},
        )
        assert cfg.discord_webhook_url == VALID_WEBHOOK_URL
    finally:
        locked_dir.chmod(0o755)
        env_file.chmod(0o644)


def test_load_config_explicit_env_file_missing_and_directory_errors(tmp_path: Path) -> None:
    missing_env = tmp_path / "does-not-exist.env"
    with pytest.raises(ConfigError, match="Configuration file not found or unreadable"):
        load_config(env_file=missing_env, environ={"DISCORD_WEBHOOK_URL": VALID_WEBHOOK_URL})

    dir_env = tmp_path / "env_dir"
    dir_env.mkdir()
    with pytest.raises(ConfigError, match="Configuration file not found or unreadable"):
        load_config(env_file=dir_env, environ={"DISCORD_WEBHOOK_URL": VALID_WEBHOOK_URL})


def test_load_config_blocks_cwd_dotenv_when_system_env_present_or_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cwd_dir = tmp_path / "cwd"
    cwd_dir.mkdir()
    monkeypatch.chdir(cwd_dir)
    cwd_dotenv = cwd_dir / ".env"
    cwd_dotenv.write_text(
        f"DISCORD_WEBHOOK_URL={VALID_CANARY_WEBHOOK_URL}\n"
        "DISCORD_USERNAME=InjectedBot\n"
        "SERVER_NAME=injected.example.com\n",
        encoding="utf-8",
    )

    # 1. System env exists & readable -> .env must NOT be loaded or inject defaults
    sys_env = tmp_path / "system_env"
    sys_env.write_text(f"DISCORD_WEBHOOK_URL={VALID_WEBHOOK_URL}\n", encoding="utf-8")
    monkeypatch.setattr("mail2hook.config.DEFAULT_SYSTEM_ENV_PATH", sys_env)
    cfg = load_config(load_default_files=True, environ={})
    assert cfg.discord_webhook_url == VALID_WEBHOOK_URL
    assert cfg.discord_username == DEFAULT_DISCORD_USERNAME
    assert cfg.server_name is None

    # 1b. System env exists without webhook -> .env must STILL NOT supply webhook
    sys_env.write_text("SERVER_NAME=system-only.example.com\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="Missing required environment variable"):
        load_config(load_default_files=True, environ={})

    # 2. System env unreadable (EACCES) -> .env must NOT be loaded; permission error surfaces
    if not (hasattr(os, "geteuid") and os.geteuid() == 0):
        locked_dir = tmp_path / "locked_sys"
        locked_dir.mkdir(mode=0o755)
        locked_env = locked_dir / "env"
        locked_env.write_text(f"DISCORD_WEBHOOK_URL={VALID_WEBHOOK_URL}\n", encoding="utf-8")
        locked_dir.chmod(0o000)
        try:
            monkeypatch.setattr("mail2hook.config.DEFAULT_SYSTEM_ENV_PATH", locked_env)
            with pytest.raises(ConfigError, match="Permission denied reading configuration file"):
                load_config(load_default_files=True, environ={})
        finally:
            locked_dir.chmod(0o755)

    # 3. System env absent + running as root -> .env must NOT be loaded
    monkeypatch.setattr("mail2hook.config.DEFAULT_SYSTEM_ENV_PATH", tmp_path / "absent_env")
    monkeypatch.setattr("mail2hook.config._is_running_as_root", lambda: True)
    with pytest.raises(ConfigError, match="Missing required environment variable"):
        load_config(load_default_files=True, environ={})

    # 4. System env absent + non-root -> .env loads for local development
    monkeypatch.setattr("mail2hook.config._is_running_as_root", lambda: False)
    local_cfg = load_config(load_default_files=True, environ={})
    assert local_cfg.discord_webhook_url == VALID_CANARY_WEBHOOK_URL
    assert local_cfg.discord_username == "InjectedBot"


def test_load_config_quoted_values_with_inline_comments(tmp_path: Path) -> None:
    env_file = tmp_path / "comments_env"
    env_file.write_text(
        f'DISCORD_WEBHOOK_URL="{VALID_WEBHOOK_URL}" # comment with "quotes"\n'
        "DISCORD_USERNAME='MyBot#1'\t# tab comment with 'quote'\n"
        'SERVER_NAME="web#01" # keep hash inside quotes\n'
        "FILTER_SENDERS=root@example.com\t# tab inline comment\n"
        'FILTER_SUBJECT_KEYWORDS="" # empty quoted string\n',
        encoding="utf-8",
    )
    cfg = load_config(env_file=env_file, environ={})
    assert cfg.discord_webhook_url == VALID_WEBHOOK_URL
    assert cfg.discord_username == "MyBot#1"
    assert cfg.server_name == "web#01"
    assert cfg.filter_senders == ["root@example.com"]
    assert cfg.filter_subject_keywords == []

    bare_env = tmp_path / "bare_comment_env"
    bare_env.write_text(f"{VALID_WEBHOOK_URL} # pasted webhook with note\n", encoding="utf-8")
    bare_cfg = load_config(env_file=bare_env, environ={})
    assert bare_cfg.discord_webhook_url == VALID_WEBHOOK_URL


def test_validate_discord_username() -> None:
    assert validate_discord_username(None) == DEFAULT_DISCORD_USERNAME
    assert validate_discord_username("   ") == DEFAULT_DISCORD_USERNAME
    assert validate_discord_username("there") == "there"
    assert validate_discord_username("everyone-bot") == "everyone-bot"
    assert validate_discord_username("A" * 80) == "A" * 80

    for invalid in (
        "A" * 81,
        "My Discord Hook",
        "Clyde-Notifier",
        "bad```name",
        "everyone",
        "EVERYONE",
        "here",
        "Here",
        "@everyone",
        "@here",
    ):
        with pytest.raises(ConfigError, match="Invalid DISCORD_USERNAME"):
            validate_discord_username(invalid)
