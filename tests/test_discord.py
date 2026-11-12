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
    assert "From" in field_names
    assert "Date" in field_names

    # Footer
    assert "box01.example.com" in embed["footer"]["text"]


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
