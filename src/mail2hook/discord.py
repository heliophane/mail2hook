"""Discord payload building and webhook delivery."""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from email.utils import parseaddr
from typing import Any

from mail2hook.config import Config
from mail2hook.parse import ParsedMessage, truncate_text

logger = logging.getLogger(__name__)

# Discord embed limits
MAX_TITLE_CHARS = 256
MAX_DESCRIPTION_CHARS = 4096
MAX_FIELD_NAME_CHARS = 256
MAX_FIELD_VALUE_CHARS = 1024

# Colors (decimal)
COLOR_SUCCESS = 0x57F287  # Green
COLOR_FAILURE = 0xED4245  # Red
COLOR_INFO = 0x5865F2  # Blurple


class DiscordError(Exception):
    """Base error for Discord posting."""


class DiscordTemporaryError(DiscordError):
    """Temporary failure (e.g. rate limit, 5xx, network timeout) -> exit 75."""


class DiscordPermanentError(DiscordError):
    """Permanent failure (e.g. invalid payload 400, deleted webhook 404) -> exit 65/78."""


def redact_webhook_url(url: str) -> str:
    """Mask the secret token part of a Discord webhook URL for safe logging."""
    parts = url.rstrip("/").split("/")
    if len(parts) >= 2 and parts[-2].isdigit():
        return f"{'/'.join(parts[:-1])}/[REDACTED]"
    return "[REDACTED_WEBHOOK_URL]"


def format_sender(sender: str, hostname: str | None) -> str:
    """Format sender cleanly for Discord without duplicate names or broken angles."""
    name, addr = parseaddr(sender)
    name = name.strip()
    addr = addr.strip()

    # If address has no @ or is localhost, associate with hostname if known
    if addr:
        if "@" not in addr and hostname:
            addr = f"{addr}@{hostname}"
        elif (addr.endswith("@localhost") or addr.endswith("@localdomain")) and hostname:
            user = addr.split("@")[0]
            addr = f"{user}@{hostname}"

    if not addr:
        return sender.strip()

    # If name is redundant with the localpart (e.g. name='root', addr='root@ubuntu24')
    localpart = addr.split("@")[0]
    if not name or name.lower() == localpart.lower():
        return addr

    return f"{name} ({addr})"


def get_log_reminder(subject: str, body: str, hostname: str | None, status: str) -> str:
    """Generate log inspection instructions and shell commands."""
    sub_lower = subject.lower()
    body_lower = body.lower()
    host_display = f" on `{hostname}`" if hostname else ""

    if "unattended" in sub_lower or "unattended" in body_lower:
        if status == "failure":
            return (
                f"Check upgrade failure logs{host_display}:\n"
                "```bash\n"
                "sudo tail -n 50 /var/log/unattended-upgrades/unattended-upgrades.log\n"
                "sudo tail -n 50 /var/log/unattended-upgrades/unattended-upgrades-dpkg.log\n"
                "```"
            )
        return (
            f"Check upgrade logs{host_display}:\n"
            "```bash\n"
            "sudo tail -n 50 /var/log/unattended-upgrades/unattended-upgrades.log\n"
            "```"
        )

    return f"Check system logs{host_display}:\n```bash\nsudo journalctl -xe\n```"


def build_payload(msg: ParsedMessage, config: Config) -> dict[str, Any]:
    """Construct the Discord webhook payload dictionary."""
    color = COLOR_INFO
    if msg.status == "success":
        color = COLOR_SUCCESS
    elif msg.status == "failure":
        color = COLOR_FAILURE

    max_desc = min(config.max_body_chars, MAX_DESCRIPTION_CHARS)
    desc = truncate_text(msg.body, max_desc) if msg.body else "(no content)"

    fields: list[dict[str, Any]] = []

    if msg.hostname:
        fields.append(
            {
                "name": "Server",
                "value": f"`{msg.hostname}`",
                "inline": True,
            }
        )

    sender_formatted = format_sender(msg.sender, msg.hostname)
    fields.append(
        {
            "name": "From",
            "value": truncate_text(sender_formatted, MAX_FIELD_VALUE_CHARS),
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

    log_reminder = get_log_reminder(msg.subject, msg.body, msg.hostname, msg.status)
    fields.append(
        {
            "name": "Server Logs",
            "value": log_reminder,
            "inline": False,
        }
    )

    footer_text = f"mail2hook • {msg.hostname}" if msg.hostname else "mail2hook"

    embed: dict[str, Any] = {
        "title": truncate_text(msg.subject, MAX_TITLE_CHARS),
        "description": desc,
        "color": color,
        "fields": fields,
        "footer": {"text": truncate_text(footer_text, 2048)},
    }

    if msg.hostname:
        embed["author"] = {"name": f"Server: {msg.hostname}"}

    return {
        "username": config.discord_username,
        "allowed_mentions": {"parse": []},
        "embeds": [embed],
    }


def post_to_discord(
    webhook_url: str,
    payload: dict[str, Any],
    max_retries: int = 3,
    initial_backoff: float = 1.0,
    max_backoff: float = 10.0,
) -> None:
    """Send payload to the Discord webhook with retry logic for 429 and 5xx errors."""
    data = json.dumps(payload).encode("utf-8")
    masked_url = redact_webhook_url(webhook_url)

    req = urllib.request.Request(
        webhook_url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "mail2hook (https://github.com/example/mail2hook, 0.1.0)",
        },
        method="POST",
    )

    attempts = 0
    backoff = initial_backoff

    while True:
        attempts += 1
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                # 204 No Content is standard for Discord webhooks
                if resp.status in (200, 204):
                    return
                # Unexpected status code
                raise DiscordTemporaryError(
                    f"Unexpected status code {resp.status} from Discord webhook {masked_url}"
                )

        except urllib.error.HTTPError as http_err:
            status_code = http_err.code

            # Handle Rate Limiting (429)
            if status_code == 429:
                if attempts > max_retries:
                    raise DiscordTemporaryError(
                        f"Rate limit retry attempts exhausted for Discord webhook {masked_url}"
                    ) from http_err

                retry_after_sec = 2.0
                try:
                    retry_header = http_err.headers.get("Retry-After")
                    if retry_header:
                        retry_after_sec = float(retry_header)
                    else:
                        error_body = http_err.read().decode("utf-8", errors="replace")
                        body_data = json.loads(error_body)
                        retry_after_sec = float(body_data.get("retry_after", 2.0))
                except Exception:
                    pass

                sleep_time = min(retry_after_sec, max_backoff)
                time.sleep(sleep_time)
                continue

            # Transient 5xx server errors
            if 500 <= status_code < 600:
                if attempts > max_retries:
                    raise DiscordTemporaryError(
                        f"Discord server error HTTP {status_code} on {masked_url} "
                        f"after {attempts} attempts"
                    ) from http_err

                sleep_time = min(backoff, max_backoff)
                time.sleep(sleep_time)
                backoff *= 2.0
                continue

            # Permanent 4xx errors (e.g. 400 Bad Request, 401 Unauthorized, 404 Not Found)
            err_msg = f"Discord permanent error HTTP {status_code} on {masked_url}"
            try:
                details = http_err.read().decode("utf-8", errors="replace")
                if details:
                    # Avoid logging secrets or excessive data
                    err_msg += f": {details[:200]}"
            except Exception:
                pass
            raise DiscordPermanentError(err_msg) from http_err

        except (urllib.error.URLError, TimeoutError, OSError) as net_err:
            # Network or connection errors
            if attempts > max_retries:
                raise DiscordTemporaryError(
                    f"Network error communicating with Discord: {net_err}"
                ) from net_err

            sleep_time = min(backoff, max_backoff)
            time.sleep(sleep_time)
            backoff *= 2.0
            continue
