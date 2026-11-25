"""Discord payload building and webhook delivery."""

from __future__ import annotations

import http.client
import json
import logging
import math
import platform
import random
import re
import socket
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from datetime import UTC
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from mail2hook import __version__
from mail2hook.config import Config
from mail2hook.parse import ParsedMessage, truncate_text

logger = logging.getLogger(__name__)

# Discord webhook and embed limits
MAX_CONTENT_CHARS = 2000
MAX_TITLE_CHARS = 256
MAX_DESCRIPTION_CHARS = 4096
MAX_FIELD_NAME_CHARS = 256
MAX_FIELD_VALUE_CHARS = 1024
MAX_AUTHOR_NAME_CHARS = 256
MAX_FOOTER_TEXT_CHARS = 2048
MAX_EMBED_TOTAL_CHARS = 6000

# Colors (decimal)
COLOR_SUCCESS = 0x57F287  # Green
COLOR_WARNING = 0xFEE75C  # Yellow
COLOR_FAILURE = 0xED4245  # Red
COLOR_INFO = 0x5865F2  # Blurple


class DiscordError(Exception):
    """Base error for Discord posting."""


class DiscordTemporaryError(DiscordError):
    """Temporary failure (e.g. rate limit, 5xx, network timeout) -> exit 75."""


class DiscordPermanentError(DiscordError):
    """Permanent failure (e.g. invalid payload 400, deleted webhook 404) -> exit 65/78."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def _sanitize_inline_code(value: str) -> str:
    """Remove backticks and newlines so untrusted values cannot break Discord code spans."""
    return re.sub(r"[`\r\n]+", "", value).strip()


_WEBHOOK_ID_RE = re.compile(r"/api/(?:v\d+/)?webhooks/(\d+)(?:/|$)")
_WEBHOOK_SECRET_RE = re.compile(
    r"(https?://(?:[^/\s@]+@)?[^\s/]+/api/(?:v\d+/)?webhooks/\d+/)([^\s/?#\"'>]+)"
)
_URL_USERINFO_RE = re.compile(r"(https?://)([^/\s@]+)@")


def redact_webhook_url(url: str) -> str:
    """Mask the secret token part and any credentials of a Discord webhook URL."""
    try:
        parsed = urlsplit(url.strip())
    except ValueError:
        return "[REDACTED_WEBHOOK_URL]"

    if not parsed.scheme or not parsed.netloc:
        return "[REDACTED_WEBHOOK_URL]"

    host_part = parsed.netloc.rsplit("@", 1)[-1]
    prefix = f"{parsed.scheme}://[REDACTED]@" if "@" in parsed.netloc else f"{parsed.scheme}://"

    path_parts = [p for p in parsed.path.split("/") if p]
    if len(path_parts) >= 2 and path_parts[-2].isdigit():
        redacted_path = "/" + "/".join([*path_parts[:-1], "[REDACTED]"])
        return f"{prefix}{host_part}{redacted_path}"
    return "[REDACTED_WEBHOOK_URL]"


def extract_webhook_id(url: str | None) -> str | None:
    """Return the numeric Discord webhook ID without exposing its token."""
    if not url:
        return None
    try:
        path = urlsplit(url.strip()).path
    except ValueError:
        return None
    match = _WEBHOOK_ID_RE.search(path)
    return match.group(1) if match else None


def redact_secrets(text: str, webhook_url: str | None = None) -> str:
    """Redact webhook URLs, tokens, and URL credentials from text."""
    if not text:
        return text
    result = text
    if webhook_url:
        stripped = webhook_url.strip()
        if stripped:
            masked = redact_webhook_url(stripped)
            base_only = stripped.split("?", 1)[0].split("#", 1)[0]
            for candidate in (stripped, stripped.rstrip("/"), base_only, base_only.rstrip("/")):
                if candidate:
                    result = result.replace(candidate, masked)
            try:
                path_parts = [p for p in urlsplit(stripped).path.split("/") if p]
            except ValueError:
                path_parts = []
            if len(path_parts) >= 2 and path_parts[-2].isdigit() and len(path_parts[-1]) >= 16:
                result = result.replace(path_parts[-1], "[REDACTED]")
    result = _WEBHOOK_SECRET_RE.sub(r"\1[REDACTED]", result)
    result = _URL_USERINFO_RE.sub(r"\1[REDACTED]@", result)
    return result


def get_log_reminder(
    subject: str,
    body: str,
    hostname: str | None,
    status: str,
    reboot_required: bool = False,
) -> str:
    """Generate log inspection instructions and shell commands."""
    sub_lower = subject.lower()
    body_lower = body.lower()
    safe_host = _sanitize_inline_code(hostname) if hostname else ""
    host_display = f" on `{safe_host}`" if safe_host else ""

    reboot_hint = "\n\nReboot server:\n```bash\nsudo reboot\n```" if reboot_required else ""

    if "unattended" in sub_lower or "unattended" in body_lower:
        if status == "failure":
            return (
                f"Check upgrade failure logs{host_display}:\n"
                "```bash\n"
                "sudo tail -n 50 /var/log/unattended-upgrades/unattended-upgrades.log\n"
                "sudo tail -n 50 /var/log/unattended-upgrades/unattended-upgrades-dpkg.log\n"
                "```"
                f"{reboot_hint}"
            )
        return (
            f"Check upgrade logs{host_display}:\n"
            "```bash\n"
            "sudo tail -n 50 /var/log/unattended-upgrades/unattended-upgrades.log\n"
            "```"
            f"{reboot_hint}"
        )

    return f"Check system logs{host_display}:\n```bash\nsudo journalctl -xe\n```{reboot_hint}"


def get_os_info() -> str | None:
    """Detect local OS name and major/minor version (e.g. 'Ubuntu 24.04', 'Debian 12')."""
    # 1. Try platform.freedesktop_os_release (Python 3.10+)
    try:
        if hasattr(platform, "freedesktop_os_release"):
            info = platform.freedesktop_os_release()
            name = info.get("NAME") or info.get("ID", "").capitalize()
            version = info.get("VERSION_ID")
            if name and version:
                return f"{name} {version}"
            if info.get("PRETTY_NAME"):
                return info["PRETTY_NAME"]
    except Exception:
        pass

    # 2. Try parsing /etc/os-release directly
    for os_file in (Path("/etc/os-release"), Path("/usr/lib/os-release")):
        try:
            if os_file.is_file():
                data: dict[str, str] = {}
                for line in os_file.read_text(encoding="utf-8", errors="replace").splitlines():
                    line = line.strip()
                    if "=" in line and not line.startswith("#"):
                        k, v = line.split("=", 1)
                        data[k.strip()] = v.strip().strip('"').strip("'")
                name = data.get("NAME") or data.get("ID", "").capitalize()
                version = data.get("VERSION_ID")
                if name and version:
                    return f"{name} {version}"
                if data.get("PRETTY_NAME"):
                    return data["PRETTY_NAME"]
        except Exception:
            pass

    return None


def get_effective_hostname(msg: ParsedMessage, config: Config) -> str | None:
    """Determine the most accurate hostname: config override > parsed message > local socket."""
    if config.server_name:
        return _sanitize_inline_code(config.server_name) or None
    if msg.hostname:
        return _sanitize_inline_code(msg.hostname) or None
    try:
        local_host = _sanitize_inline_code(socket.gethostname())
        if local_host and local_host.lower() not in ("localhost", "localhost.localdomain"):
            return local_host
    except Exception:
        pass
    return None


def build_payload(msg: ParsedMessage, config: Config) -> dict[str, Any]:
    """Construct the Discord webhook payload dictionary."""
    if msg.status == "failure":
        color = COLOR_FAILURE
    elif msg.reboot_required or msg.status == "warning":
        color = COLOR_WARNING
    elif msg.status == "success":
        color = COLOR_SUCCESS
    else:
        color = COLOR_INFO

    max_desc = min(config.max_body_chars, MAX_DESCRIPTION_CHARS)
    desc = truncate_text(msg.body, max_desc) if msg.body else "(no content)"

    hostname = get_effective_hostname(msg, config)
    os_info_raw = get_os_info()
    os_info = _sanitize_inline_code(os_info_raw) if os_info_raw else None

    fields: list[dict[str, Any]] = []

    if hostname:
        fields.append(
            {
                "name": "Server",
                "value": f"`{hostname}`",
                "inline": True,
            }
        )

    if os_info:
        fields.append(
            {
                "name": "OS",
                "value": f"`{os_info}`",
                "inline": True,
            }
        )

    # Show Reboot field for upgrade mails or whenever a reboot is detected
    is_upgrade = "unattended" in msg.subject.lower() or "unattended" in msg.body.lower()
    if msg.reboot_required or is_upgrade:
        if msg.reboot_required:
            safe_pkgs = [
                _sanitize_inline_code(p) for p in msg.reboot_packages if _sanitize_inline_code(p)
            ]
            if safe_pkgs:
                pkgs_preview = ", ".join(safe_pkgs[:2])
                if len(safe_pkgs) > 2:
                    pkgs_preview += f" (+{len(safe_pkgs) - 2} more)"
                reboot_value = f"⚠️ **Required** (`{pkgs_preview}`)"
            else:
                reboot_value = "⚠️ **Required**"
        else:
            reboot_value = "🟢 Not needed"

        fields.append(
            {
                "name": "Reboot",
                "value": truncate_text(reboot_value, MAX_FIELD_VALUE_CHARS),
                "inline": True,
            }
        )

    if msg.date:
        fields.append(
            {
                "name": "Date",
                "value": truncate_text(msg.date, MAX_FIELD_VALUE_CHARS),
                "inline": True,
            }
        )

    log_reminder = get_log_reminder(
        msg.subject,
        msg.body,
        hostname,
        msg.status,
        reboot_required=msg.reboot_required,
    )
    fields.append(
        {
            "name": "Server Logs",
            "value": truncate_text(log_reminder, MAX_FIELD_VALUE_CHARS),
            "inline": False,
        }
    )

    footer_text = f"mail2hook • {hostname}" if hostname else "mail2hook"
    if os_info:
        footer_text = f"{footer_text} ({os_info})"
    footer_text = truncate_text(footer_text, MAX_FOOTER_TEXT_CHARS)

    title_text = truncate_text(msg.subject, MAX_TITLE_CHARS)

    author_name: str | None = None
    if hostname:
        author_raw = f"Server: {hostname}"
        if os_info:
            author_raw += f" • {os_info}"
        author_name = truncate_text(author_raw, MAX_AUTHOR_NAME_CHARS)

    # Enforce Discord's 6000-character total limit across all embed text fields
    overhead = (
        len(title_text)
        + len(footer_text)
        + (len(author_name) if author_name else 0)
        + sum(len(f["name"]) + len(f["value"]) for f in fields)
    )
    available_desc = max(1, MAX_EMBED_TOTAL_CHARS - overhead)
    desc = truncate_text(desc, available_desc)

    embed: dict[str, Any] = {
        "title": title_text,
        "description": desc,
        "color": color,
        "fields": fields,
        "footer": {"text": footer_text},
    }

    if author_name:
        embed["author"] = {"name": author_name}

    return {
        "username": config.discord_username,
        "allowed_mentions": {"parse": []},
        "embeds": [embed],
    }


MAX_ERROR_BODY_BYTES = 4096
DEFAULT_TOTAL_TIMEOUT = 60.0


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Reject HTTP 3xx redirects rather than converting POST into GET."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        return None


_NO_REDIRECT_OPENER = urllib.request.build_opener(_NoRedirectHandler())


def _open_request(req: urllib.request.Request, timeout: float = 15.0) -> Any:
    """Execute an HTTP request without following redirects."""
    return _NO_REDIRECT_OPENER.open(req, timeout=timeout)


def _header_get(headers: Mapping[str, str] | None, name: str) -> str | None:
    """Lookup an HTTP header value case-insensitively."""
    if headers is None:
        return None
    val = headers.get(name)
    if val is not None:
        return val
    target = name.lower()
    for key in headers:
        if str(key).lower() == target:
            return headers[key]
    return None


def _parse_non_negative_finite(raw: Any) -> float | None:
    """Parse raw numeric value as a finite float >= 0, else return None."""
    if isinstance(raw, bool):
        return None
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(val) or val < 0:
        return None
    return val


def _parse_retry_after(
    headers: Mapping[str, str] | None,
    body_bytes: bytes,
    *,
    default: float = 2.0,
) -> float:
    """Determine wait seconds from JSON retry_after, X-RateLimit-Reset-After, or Retry-After."""
    if body_bytes:
        try:
            body_data = json.loads(body_bytes.decode("utf-8", errors="replace"))
            if isinstance(body_data, dict) and "retry_after" in body_data:
                parsed = _parse_non_negative_finite(body_data["retry_after"])
                if parsed is not None:
                    return parsed
        except Exception:
            pass

    reset_after = _header_get(headers, "X-RateLimit-Reset-After")
    if reset_after is not None:
        parsed = _parse_non_negative_finite(reset_after.strip())
        if parsed is not None:
            return parsed

    retry_header = _header_get(headers, "Retry-After")
    if retry_header is not None:
        stripped = retry_header.strip()
        parsed = _parse_non_negative_finite(stripped)
        if parsed is not None:
            return parsed
        try:
            dt = parsedate_to_datetime(stripped)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            delta = dt.timestamp() - time.time()
            if math.isfinite(delta) and delta >= 0:
                return delta
        except Exception:
            pass

    return default


def _is_discord_json_error(headers: Mapping[str, str] | None, body_bytes: bytes) -> bool:
    """Return True if an HTTP error response looks like a JSON API response from Discord."""
    content_type = (_header_get(headers, "Content-Type") or "").lower()
    if "application/json" in content_type:
        return True
    if not body_bytes:
        return False
    try:
        parsed = json.loads(body_bytes.decode("utf-8", errors="replace"))
        return isinstance(parsed, dict)
    except Exception:
        return False


def _sleep_within_deadline(
    wait_time: float,
    deadline: float,
    exhaust_msg: str,
    *,
    strict_wait: bool = False,
) -> None:
    """Sleep before retrying without crossing the total deadline."""
    remaining = deadline - time.monotonic()
    if remaining <= 0 or (strict_wait and wait_time > remaining):
        raise DiscordTemporaryError(exhaust_msg) from None
    sleep_for = wait_time if strict_wait else min(wait_time, remaining)
    if sleep_for > 0:
        time.sleep(sleep_for)


def post_to_discord(
    webhook_url: str,
    payload: dict[str, Any],
    max_retries: int = 3,
    initial_backoff: float = 1.0,
    max_backoff: float = 10.0,
    total_timeout: float = DEFAULT_TOTAL_TIMEOUT,
) -> None:
    """Send payload to the Discord webhook with retry, deadline, and secret redaction."""
    data = json.dumps(payload).encode("utf-8")
    masked_url = redact_webhook_url(webhook_url)
    headers = {
        "Content-Type": "application/json",
        "User-Agent": f"DiscordBot (https://github.com/heliophane/mail2hook, {__version__})",
    }

    attempts = 0
    backoff = initial_backoff
    deadline = time.monotonic() + total_timeout

    while True:
        attempts += 1
        req = urllib.request.Request(webhook_url, data=data, headers=headers, method="POST")
        remaining = deadline - time.monotonic()
        req_timeout = min(15.0, max(1.0, remaining)) if total_timeout > 0 else 1.0

        try:
            with _open_request(req, timeout=req_timeout) as resp:
                # 204 No Content is standard for Discord webhooks
                if resp.status in (200, 204):
                    return
                if 300 <= resp.status < 400:
                    raise DiscordTemporaryError(
                        f"Unexpected redirect HTTP {resp.status} (redirects disabled) "
                        f"from Discord webhook {masked_url}"
                    )
                raise DiscordTemporaryError(
                    f"Unexpected status code {resp.status} from Discord webhook {masked_url}"
                )

        except urllib.error.HTTPError as http_err:
            try:
                status_code = http_err.code
                resp_headers = http_err.headers
                try:
                    raw_body = http_err.read(MAX_ERROR_BODY_BYTES)
                except Exception:
                    raw_body = b""
            finally:
                http_err.close()

            if 300 <= status_code < 400:
                raise DiscordTemporaryError(
                    f"Unexpected redirect HTTP {status_code} (redirects disabled) "
                    f"from Discord webhook {masked_url}"
                ) from None

            if status_code == 429:
                if attempts > max_retries:
                    raise DiscordTemporaryError(
                        f"Rate limit retry attempts exhausted for Discord webhook {masked_url}"
                    ) from None

                retry_after_sec = _parse_retry_after(resp_headers, raw_body)
                jitter = random.uniform(0.0, min(0.5, retry_after_sec * 0.25 + 0.05))
                _sleep_within_deadline(
                    retry_after_sec + jitter,
                    deadline,
                    f"Rate limit wait exceeds deadline for Discord webhook {masked_url}",
                    strict_wait=True,
                )
                continue

            is_cloudflare_403 = status_code == 403 and not _is_discord_json_error(
                resp_headers, raw_body
            )
            if status_code == 408 or 500 <= status_code < 600 or is_cloudflare_403:
                if attempts > max_retries:
                    raise DiscordTemporaryError(
                        f"Discord server error HTTP {status_code} on {masked_url} "
                        f"after {attempts} attempts"
                    ) from None

                jitter = random.uniform(0.0, min(0.5, backoff * 0.25))
                _sleep_within_deadline(
                    min(backoff + jitter, max_backoff + 0.5),
                    deadline,
                    f"Retry deadline exceeded after HTTP {status_code} on {masked_url}",
                )
                backoff *= 2.0
                continue

            err_msg = f"Discord permanent error HTTP {status_code} on {masked_url}"
            if raw_body:
                details = redact_secrets(
                    raw_body.decode("utf-8", errors="replace"),
                    webhook_url,
                )
                if details:
                    err_msg += f": {details[:200]}"
            raise DiscordPermanentError(err_msg, status_code=status_code) from None

        except (http.client.HTTPException, urllib.error.URLError, TimeoutError, OSError) as net_err:
            safe_net_err = redact_secrets(str(net_err), webhook_url)
            if attempts > max_retries:
                raise DiscordTemporaryError(
                    f"Network error communicating with Discord: {safe_net_err}"
                ) from None

            jitter = random.uniform(0.0, min(0.5, backoff * 0.25))
            _sleep_within_deadline(
                min(backoff + jitter, max_backoff + 0.5),
                deadline,
                f"Network retry deadline exceeded: {safe_net_err}",
            )
            backoff *= 2.0
            continue
