"""Configuration loading and validation for mail2hook."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_SYSTEM_ENV_PATH = Path("/etc/mail2hook/env")
DEFAULT_MAX_BODY_CHARS = 1500
DEFAULT_DISCORD_USERNAME = "mail2hook"
MAX_DISCORD_USERNAME_LEN = 80
_FORBIDDEN_USERNAME_SUBSTRINGS = ("discord", "clyde", "```")
_FORBIDDEN_EXACT_USERNAMES = frozenset({"everyone", "here", "@everyone", "@here"})

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
    server_name: str | None = None


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

    cleaned = url.strip().rstrip("/")
    if not DISCORD_WEBHOOK_REGEX.match(cleaned):
        raise ConfigError(
            "Malformed DISCORD_WEBHOOK_URL: must be a valid Discord webhook URL "
            "(e.g. https://discord.com/api/webhooks/<id>/<token>)"
        )
    return cleaned


def validate_discord_username(raw_username: str | None) -> str:
    """Validate and normalize DISCORD_USERNAME per Discord webhook restrictions."""
    username = (raw_username or "").strip()
    if not username:
        return DEFAULT_DISCORD_USERNAME
    if len(username) > MAX_DISCORD_USERNAME_LEN:
        raise ConfigError(
            f"Invalid DISCORD_USERNAME: length {len(username)} exceeds maximum "
            f"{MAX_DISCORD_USERNAME_LEN} characters"
        )
    lowered = username.lower()
    for forbidden in _FORBIDDEN_USERNAME_SUBSTRINGS:
        if forbidden in lowered:
            raise ConfigError(f"Invalid DISCORD_USERNAME: must not contain '{forbidden}'")
    if lowered in _FORBIDDEN_EXACT_USERNAMES:
        raise ConfigError(f"Invalid DISCORD_USERNAME: '{username}' is reserved by Discord")
    return username


def _is_running_as_root() -> bool:
    """Return True if the effective user ID is root (uid 0)."""
    return hasattr(os, "geteuid") and os.geteuid() == 0


def _strip_inline_comment(value: str) -> str:
    """Strip unquoted inline comments and surrounding matching quotes from an env value."""
    val = value.strip()
    if not val or val.startswith("#"):
        return ""

    if val[0] in ('"', "'"):
        quote = val[0]
        for idx in range(1, len(val)):
            if val[idx] == quote:
                remainder = val[idx + 1 :].lstrip()
                if not remainder or remainder.startswith("#"):
                    return val[1:idx]

    match = re.search(r"\s+#", val)
    if match:
        return val[: match.start()].strip()
    return val


def _safe_load_dotenv(path: Path, *, ignore_missing: bool = False) -> dict[str, str]:
    """Safely load variables from a KEY=VALUE env file without external dependencies."""
    try:
        raw_text = path.read_text(encoding="utf-8", errors="replace")
    except PermissionError as err:
        raise ConfigError(
            f"Permission denied reading configuration file '{path}'. "
            f"Ensure the running user has traverse access to '{path.parent}' (0750) "
            f"and read access to '{path}' (0640), e.g. via the 'mail' group "
            f"(sudo chown root:mail {path.parent} {path} && "
            f"sudo chmod 0750 {path.parent} && sudo chmod 0640 {path})"
        ) from err
    except (FileNotFoundError, IsADirectoryError, NotADirectoryError) as err:
        if ignore_missing:
            return {}
        raise ConfigError(f"Configuration file not found or unreadable: '{path}'") from err

    result: dict[str, str] = {}
    for raw_line in raw_text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" in line:
            key, value = line.split("=", 1)
            key = key.strip()
            if key:
                result[key] = _strip_inline_comment(value)
        elif "discord.com/api/webhooks/" in line or "discordapp.com/api/webhooks/" in line:
            # The user pasted the bare webhook URL directly without key= prefix
            result["DISCORD_WEBHOOK_URL"] = _strip_inline_comment(line)
    return result


def _try_load_dotenv(path: Path) -> tuple[dict[str, str], bool, ConfigError | None]:
    """Load an optional default env file, returning (vars, file_present, load_error)."""
    try:
        return _safe_load_dotenv(path, ignore_missing=False), True, None
    except ConfigError as err:
        if isinstance(err.__cause__, FileNotFoundError):
            return {}, False, None
        return {}, True, err


def load_config(
    env_file: Path | str | None = None,
    environ: Mapping[str, str] | None = None,
    *,
    load_default_files: bool | None = None,
) -> Config:
    """Load and validate configuration from files and environment variables.

    Loading order:
    1. If `env_file` is provided, load variables from it (fails if missing/unreadable).
    2. Otherwise, if `load_default_files` is True (default when `environ` is None),
       load from `/etc/mail2hook/env` if present, or `.env` only when not running as root
       and `/etc/mail2hook/env` does not exist.
    3. Override with explicit environment variables (`environ` or `os.environ`).
    """
    file_vars: dict[str, str] = {}
    system_env_perm_error: ConfigError | None = None
    if env_file is not None:
        custom_path = Path(env_file)
        file_vars.update(_safe_load_dotenv(custom_path, ignore_missing=False))
    else:
        should_load_defaults = (
            (environ is None) if load_default_files is None else load_default_files
        )
        if should_load_defaults:
            sys_vars, system_env_present, system_env_perm_error = _try_load_dotenv(
                DEFAULT_SYSTEM_ENV_PATH
            )
            file_vars.update(sys_vars)
            if not system_env_present and not _is_running_as_root():
                file_vars.update(_safe_load_dotenv(Path(".env"), ignore_missing=True))

    active_environ = os.environ if environ is None else environ
    env_map: dict[str, str] = {**file_vars, **active_environ}

    # Accept DISCORD_WEBHOOK_URL, or common aliases DISCORD_WEBHOOK / WEBHOOK_URL
    raw_webhook = (
        env_map.get("DISCORD_WEBHOOK_URL")
        or env_map.get("DISCORD_WEBHOOK")
        or env_map.get("WEBHOOK_URL")
    )
    if (not raw_webhook or not raw_webhook.strip()) and system_env_perm_error is not None:
        raise system_env_perm_error
    webhook_url = validate_webhook_url(raw_webhook)

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

    discord_username = validate_discord_username(env_map.get("DISCORD_USERNAME"))

    filter_senders = _parse_comma_list(env_map.get("FILTER_SENDERS"))
    filter_subject_keywords = _parse_comma_list(env_map.get("FILTER_SUBJECT_KEYWORDS"))

    server_name_raw = env_map.get("SERVER_NAME") or env_map.get("HOSTNAME_OVERRIDE")
    server_name = server_name_raw.strip() if server_name_raw and server_name_raw.strip() else None

    return Config(
        discord_webhook_url=webhook_url,
        filter_senders=filter_senders,
        filter_subject_keywords=filter_subject_keywords,
        max_body_chars=max_body_chars,
        discord_username=discord_username,
        server_name=server_name,
    )
