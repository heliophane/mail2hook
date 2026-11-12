"""Configuration loading and validation for mail2hook."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values

DEFAULT_SYSTEM_ENV_PATH = Path("/etc/mail2hook/env")
DEFAULT_MAX_BODY_CHARS = 1500
DEFAULT_DISCORD_USERNAME = "mail2hook"

# Match standard Discord webhook URLs (including ptb, canary, and discordapp domains)
DISCORD_WEBHOOK_REGEX = re.compile(
    r"^https://(?:(?:ptb|canary)\.)?discord(?:app)?\.com/api/webhooks/\d+/[A-Za-z0-9_-]+/?$"
)


class ConfigError(Exception):
    """Raised when configuration is missing or invalid."""


@dataclass(frozen=True)
class Config:
    """Application configuration."""

    discord_webhook_url: str
    filter_senders: list[str] = field(default_factory=list)
    filter_subject_keywords: list[str] = field(default_factory=list)
    max_body_chars: int = DEFAULT_MAX_BODY_CHARS
    discord_username: str = DEFAULT_DISCORD_USERNAME


def _parse_comma_list(raw_value: str | None) -> list[str]:
    """Parse a comma-separated string into a list of stripped, lowercase items."""
    if not raw_value:
        return []
    items = [item.strip().lower() for item in raw_value.split(",")]
    return [item for item in items if item]


def validate_webhook_url(url: str | None) -> str:
    """Validate that the webhook URL is present and matches Discord's URL format."""
    if not url or not url.strip():
        raise ConfigError("Missing required environment variable: DISCORD_WEBHOOK_URL")

    cleaned = url.strip()
    if not DISCORD_WEBHOOK_REGEX.match(cleaned):
        raise ConfigError(
            "Malformed DISCORD_WEBHOOK_URL: must be a valid Discord webhook URL "
            "(e.g. https://discord.com/api/webhooks/<id>/<token>)"
        )
    return cleaned


def _safe_load_dotenv(path: Path) -> dict[str, str]:
    """Safely load variables from a dotenv file, giving a clear message on permission errors."""
    try:
        loaded = dotenv_values(path)
        return {k: v for k, v in loaded.items() if v is not None}
    except PermissionError as err:
        raise ConfigError(
            f"Permission denied reading configuration file '{path}'. "
            f"Ensure the running user has read access "
            f"(e.g. sudo chown root:<group> {path} && sudo chmod 0640 {path})"
        ) from err


def load_config(
    env_file: Path | str | None = None,
    environ: Mapping[str, str] | None = None,
) -> Config:
    """Load and validate configuration from files and environment variables.

    Loading order:
    1. If `env_file` is provided, load variables from it.
    2. Otherwise, if `/etc/mail2hook/env` exists, load from it.
    3. Load `.env` from the current working directory if present.
    4. Override with explicit environment variables (environ or os.environ).
    """
    file_vars: dict[str, str] = {}
    if env_file is not None:
        custom_path = Path(env_file)
        if custom_path.is_file():
            file_vars.update(_safe_load_dotenv(custom_path))
    else:
        if DEFAULT_SYSTEM_ENV_PATH.is_file():
            file_vars.update(_safe_load_dotenv(DEFAULT_SYSTEM_ENV_PATH))
        local_env = Path(".env")
        if local_env.is_file():
            file_vars.update(_safe_load_dotenv(local_env))

    active_environ = os.environ if environ is None else environ
    env_map: dict[str, str] = {**file_vars, **active_environ}

    webhook_url = validate_webhook_url(env_map.get("DISCORD_WEBHOOK_URL"))

    max_body_chars_raw = env_map.get("MAX_BODY_CHARS")
    if max_body_chars_raw is not None and max_body_chars_raw.strip():
        try:
            max_body_chars = int(max_body_chars_raw.strip())
            if max_body_chars <= 0:
                raise ValueError("must be positive")
        except ValueError as err:
            raise ConfigError(
                f"Invalid MAX_BODY_CHARS '{max_body_chars_raw}': must be a positive integer"
            ) from err
    else:
        max_body_chars = DEFAULT_MAX_BODY_CHARS

    discord_username = env_map.get("DISCORD_USERNAME", DEFAULT_DISCORD_USERNAME).strip()
    if not discord_username:
        discord_username = DEFAULT_DISCORD_USERNAME

    filter_senders = _parse_comma_list(env_map.get("FILTER_SENDERS"))
    filter_subject_keywords = _parse_comma_list(env_map.get("FILTER_SUBJECT_KEYWORDS"))

    return Config(
        discord_webhook_url=webhook_url,
        filter_senders=filter_senders,
        filter_subject_keywords=filter_subject_keywords,
        max_body_chars=max_body_chars,
        discord_username=discord_username,
    )
