"""Tests for Discord payload creation and HTTP delivery."""

from __future__ import annotations

import io
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from mail2hook.config import Config
from mail2hook.discord import (
    COLOR_FAILURE,
    COLOR_INFO,
    COLOR_SUCCESS,
    DiscordPermanentError,
    DiscordTemporaryError,
    build_payload,
    post_to_discord,
    redact_webhook_url,
)
from mail2hook.parse import ParsedMessage

FAKE_WEBHOOK_URL = "https://discord.com/api/webhooks/123456789012345678/secret-token-abcdef123456"


def _make_msg(
    status: str = "info",
    subject: str = "Test Subject",
    body: str = "Test body",
    hostname: str | None = "server01.example.com",
) -> ParsedMessage:
    return ParsedMessage(
        sender="root@server01.example.com",
        subject=subject,
        date="Wed, 11 Nov 2026 12:00:00 +0000",
        body=body,
        status=status,
        hostname=hostname,
    )


def test_redact_webhook_url() -> None:
    redacted = redact_webhook_url(FAKE_WEBHOOK_URL)
    assert "secret-token" not in redacted
    assert "[REDACTED]" in redacted
    assert "123456789012345678" in redacted


def test_build_payload_structure_and_limits() -> None:
    config = Config(
        discord_webhook_url=FAKE_WEBHOOK_URL,
        max_body_chars=500,
        discord_username="CustomBot",
    )
    long_subject = "A" * 300
    long_body = "B" * 1000

    msg = _make_msg(
        status="success",
        subject=long_subject,
        body=long_body,
        hostname="box01.example.com",
    )
    payload = build_payload(msg, config)

    assert payload["username"] == "CustomBot"
    # Strict security rule: allowed_mentions parse must be empty
    assert payload["allowed_mentions"] == {"parse": []}

    embed = payload["embeds"][0]
    # Title truncated to 256
    assert len(embed["title"]) <= 256
    assert embed["title"].endswith("…")

    # Description truncated to max_body_chars (500)
    assert len(embed["description"]) <= 500
    assert embed["description"].endswith("…")

    # Color for success
    assert embed["color"] == COLOR_SUCCESS

    # Fields
    field_names = [f["name"] for f in embed["fields"]]
    assert "Server" in field_names
    assert "From" in field_names
    assert "Date" in field_names
    assert "Server Logs" in field_names

    # Check author
    assert "Server: box01.example.com" in embed["author"]["name"]

    # Check log command in Server Logs field
    log_field = next(f for f in embed["fields"] if f["name"] == "Server Logs")
    assert "journalctl" in log_field["value"]

    # Footer
    assert "box01.example.com" in embed["footer"]["text"]


def test_get_effective_hostname() -> None:
    from mail2hook.discord import get_effective_hostname

    # Config server_name takes highest precedence
    cfg = Config(discord_webhook_url=FAKE_WEBHOOK_URL, server_name="custom-host")
    msg = _make_msg(hostname="parsed-host")
    assert get_effective_hostname(msg, cfg) == "custom-host"

    # Parsed msg hostname is second
    cfg_none = Config(discord_webhook_url=FAKE_WEBHOOK_URL, server_name=None)
    assert get_effective_hostname(msg, cfg_none) == "parsed-host"


def test_get_os_info() -> None:
    from mail2hook.discord import get_os_info

    # Should run safely without error on any OS (returns str or None)
    os_info = get_os_info()
    assert os_info is None or isinstance(os_info, str)


def test_format_sender() -> None:
    from mail2hook.discord import format_sender

    # Duplicate name and localpart
    assert format_sender("root <root@ubuntu24>", "ubuntu24") == "root@ubuntu24"
    # Bare local name without @
    assert format_sender("root", "bookstack") == "root@bookstack"
    # @localhost
    assert format_sender("root@localhost", "bookstack") == "root@bookstack"
    # Distinct name and address
    assert (
        format_sender("Alerts System <alerts@monitor.example.com>", "server01")
        == "Alerts System (alerts@monitor.example.com)"
    )


def test_get_log_reminder() -> None:
    from mail2hook.discord import get_log_reminder

    # Unattended upgrades success
    succ = get_log_reminder(
        "unattended-upgrades result: SUCCESS", "All upgrades", "ubuntu24", "success"
    )
    assert "sudo tail -n 50 /var/log/unattended-upgrades/unattended-upgrades.log" in succ
    assert "`ubuntu24`" in succ

    # Unattended upgrades failure
    fail = get_log_reminder(
        "unattended-upgrades result: FAILURE", "dpkg error", "ubuntu24", "failure"
    )
    assert "unattended-upgrades-dpkg.log" in fail

    # Generic mail
    gen = get_log_reminder("Cron output", "Backup ran", "ubuntu24", "info")
    assert "journalctl -xe" in gen

    # Reboot required adds sudo reboot
    reboot_log = get_log_reminder(
        "unattended-upgrades result", "linux-image", "ubuntu24", "success", reboot_required=True
    )
    assert "sudo reboot" in reboot_log


def test_build_payload_reboot_field() -> None:
    config = Config(discord_webhook_url=FAKE_WEBHOOK_URL)

    # 1. Upgrade message with reboot required
    msg_reboot = ParsedMessage(
        sender="root@bookstack",
        subject="unattended-upgrades result for bookstack: SUCCESS",
        date="Wed, 11 Nov 2026 12:00:00 +0000",
        body="All upgrades installed",
        status="success",
        hostname="bookstack",
        reboot_required=True,
        reboot_packages=["linux-image-6.8.0-generic"],
    )
    payload = build_payload(msg_reboot, config)
    reboot_field = next(f for f in payload["embeds"][0]["fields"] if f["name"] == "Reboot")
    assert "Required" in reboot_field["value"]
    assert "linux-image-6.8.0-generic" in reboot_field["value"]

    # 2. Upgrade message without reboot required
    msg_no_reboot = ParsedMessage(
        sender="root@bookstack",
        subject="unattended-upgrades result for bookstack: SUCCESS",
        date="Wed, 11 Nov 2026 12:00:00 +0000",
        body="All upgrades installed",
        status="success",
        hostname="bookstack",
        reboot_required=False,
    )
    payload_no = build_payload(msg_no_reboot, config)
    reboot_field_no = next(f for f in payload_no["embeds"][0]["fields"] if f["name"] == "Reboot")
    assert "Not needed" in reboot_field_no["value"]


def test_build_payload_colors() -> None:
    config = Config(discord_webhook_url=FAKE_WEBHOOK_URL)

    msg_fail = _make_msg(status="failure")
    payload_fail = build_payload(msg_fail, config)
    assert payload_fail["embeds"][0]["color"] == COLOR_FAILURE

    msg_info = _make_msg(status="info")
    payload_info = build_payload(msg_info, config)
    assert payload_info["embeds"][0]["color"] == COLOR_INFO


@patch("urllib.request.urlopen")
def test_post_to_discord_success(mock_urlopen: MagicMock) -> None:
    mock_response = MagicMock()
    mock_response.status = 204
    mock_response.__enter__.return_value = mock_response
    mock_urlopen.return_value = mock_response

    payload = {"username": "test", "embeds": []}
    post_to_discord(FAKE_WEBHOOK_URL, payload)

    assert mock_urlopen.call_count == 1
    req = mock_urlopen.call_args[0][0]
    assert req.full_url == FAKE_WEBHOOK_URL
    assert req.headers["Content-type"] == "application/json"


@patch("time.sleep")
@patch("urllib.request.urlopen")
def test_post_to_discord_retry_429_then_succeed(
    mock_urlopen: MagicMock, mock_sleep: MagicMock
) -> None:
    # First call: 429 with Retry-After header
    resp_429 = urllib.error.HTTPError(
        url=FAKE_WEBHOOK_URL,
        code=429,
        msg="Too Many Requests",
        hdrs={"Retry-After": "0.1"},  # type: ignore[arg-type]
        fp=io.BytesIO(b'{"retry_after": 0.1}'),
    )
    # Second call: 204 No Content
    resp_204 = MagicMock()
    resp_204.status = 204
    resp_204.__enter__.return_value = resp_204

    mock_urlopen.side_effect = [resp_429, resp_204]

    post_to_discord(FAKE_WEBHOOK_URL, {"test": True}, max_retries=2)
    assert mock_urlopen.call_count == 2
    mock_sleep.assert_called_once_with(0.1)


@patch("time.sleep")
@patch("urllib.request.urlopen")
def test_post_to_discord_retry_exhausted_500(
    mock_urlopen: MagicMock, mock_sleep: MagicMock
) -> None:
    resp_500 = urllib.error.HTTPError(
        url=FAKE_WEBHOOK_URL,
        code=500,
        msg="Internal Server Error",
        hdrs={},  # type: ignore[arg-type]
        fp=io.BytesIO(b"server down"),
    )
    mock_urlopen.side_effect = resp_500

    with pytest.raises(DiscordTemporaryError, match="Discord server error HTTP 500"):
        post_to_discord(FAKE_WEBHOOK_URL, {"test": True}, max_retries=2, initial_backoff=0.01)

    assert mock_urlopen.call_count == 3


@patch("time.sleep")
@patch("urllib.request.urlopen")
def test_post_to_discord_network_error(mock_urlopen: MagicMock, mock_sleep: MagicMock) -> None:
    mock_urlopen.side_effect = urllib.error.URLError("Connection refused")

    with pytest.raises(DiscordTemporaryError, match="Network error communicating with Discord"):
        post_to_discord(FAKE_WEBHOOK_URL, {"test": True}, max_retries=2, initial_backoff=0.01)

    assert mock_urlopen.call_count == 3


@patch("urllib.request.urlopen")
def test_post_to_discord_permanent_error_404(mock_urlopen: MagicMock) -> None:
    resp_404 = urllib.error.HTTPError(
        url=FAKE_WEBHOOK_URL,
        code=404,
        msg="Not Found",
        hdrs={},  # type: ignore[arg-type]
        fp=io.BytesIO(b'{"message": "Unknown Webhook"}'),
    )
    mock_urlopen.side_effect = resp_404

    with pytest.raises(DiscordPermanentError, match="Discord permanent error HTTP 404"):
        post_to_discord(FAKE_WEBHOOK_URL, {"test": True})

    # Permanent error should not be retried
    assert mock_urlopen.call_count == 1
