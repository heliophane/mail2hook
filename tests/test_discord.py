"""Tests for Discord payload creation and HTTP delivery."""

from __future__ import annotations

import http.client
import io
import urllib.error
import urllib.request
from email.message import Message
from email.utils import formatdate
from unittest.mock import MagicMock, patch

import pytest

from mail2hook.config import Config
from mail2hook.discord import (
    _NO_REDIRECT_OPENER,
    COLOR_FAILURE,
    COLOR_INFO,
    COLOR_SUCCESS,
    MAX_ERROR_BODY_BYTES,
    DiscordPermanentError,
    DiscordTemporaryError,
    _NoRedirectHandler,
    _parse_retry_after,
    build_payload,
    post_to_discord,
    redact_secrets,
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
    assert format_sender("root", "server01") == "root@server01"
    # @localhost
    assert format_sender("root@localhost", "server01") == "root@server01"
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
        sender="root@server01",
        subject="unattended-upgrades result for server01: SUCCESS",
        date="Wed, 11 Nov 2026 12:00:00 +0000",
        body="All upgrades installed",
        status="success",
        hostname="server01",
        reboot_required=True,
        reboot_packages=["linux-image-6.8.0-generic"],
    )
    payload = build_payload(msg_reboot, config)
    reboot_field = next(f for f in payload["embeds"][0]["fields"] if f["name"] == "Reboot")
    assert "Required" in reboot_field["value"]
    assert "linux-image-6.8.0-generic" in reboot_field["value"]

    # 2. Upgrade message without reboot required
    msg_no_reboot = ParsedMessage(
        sender="root@server01",
        subject="unattended-upgrades result for server01: SUCCESS",
        date="Wed, 11 Nov 2026 12:00:00 +0000",
        body="All upgrades installed",
        status="success",
        hostname="server01",
        reboot_required=False,
    )
    payload_no = build_payload(msg_no_reboot, config)
    reboot_field_no = next(f for f in payload_no["embeds"][0]["fields"] if f["name"] == "Reboot")
    assert "Not needed" in reboot_field_no["value"]


def test_build_payload_sanitizes_backticks() -> None:
    config = Config(discord_webhook_url=FAKE_WEBHOOK_URL, server_name="host`injected`\nname")
    msg = _make_msg()
    payload = build_payload(msg, config)
    server_field = next(f for f in payload["embeds"][0]["fields"] if f["name"] == "Server")
    assert server_field["value"] == "`hostinjectedname`"


def test_build_payload_colors() -> None:
    config = Config(discord_webhook_url=FAKE_WEBHOOK_URL)

    msg_fail = _make_msg(status="failure")
    payload_fail = build_payload(msg_fail, config)
    assert payload_fail["embeds"][0]["color"] == COLOR_FAILURE

    msg_info = _make_msg(status="info")
    payload_info = build_payload(msg_info, config)
    assert payload_info["embeds"][0]["color"] == COLOR_INFO


def _make_headers(mapping: dict[str, str] | None = None) -> Message:
    msg = Message()
    for key, val in (mapping or {}).items():
        msg[key] = val
    return msg


def _make_http_error(
    status_code: int,
    *,
    headers: dict[str, str] | None = None,
    body: bytes = b"",
    url: str = FAKE_WEBHOOK_URL,
) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        url=url,
        code=status_code,
        msg=f"HTTP {status_code}",
        hdrs=_make_headers(headers),
        fp=io.BytesIO(body),
    )


@patch("mail2hook.discord._open_request")
def test_post_to_discord_success(mock_open: MagicMock) -> None:
    mock_response = MagicMock()
    mock_response.status = 204
    mock_response.__enter__.return_value = mock_response
    mock_open.return_value = mock_response

    payload = {"username": "test", "embeds": []}
    post_to_discord(FAKE_WEBHOOK_URL, payload)

    assert mock_open.call_count == 1
    req = mock_open.call_args[0][0]
    assert req.full_url == FAKE_WEBHOOK_URL
    assert req.headers["Content-type"] == "application/json"
    assert req.headers["User-agent"].startswith(
        "DiscordBot (https://github.com/heliophane/mail2hook, "
    )


@patch("random.uniform", return_value=0.0)
@patch("time.sleep")
@patch("mail2hook.discord._open_request")
def test_post_to_discord_retry_429_then_succeed(
    mock_open: MagicMock, mock_sleep: MagicMock, _mock_rng: MagicMock
) -> None:
    resp_429 = _make_http_error(
        429,
        headers={"Retry-After": "99", "Content-Type": "application/json"},
        body=b'{"retry_after": 0.1}',
    )
    resp_204 = MagicMock()
    resp_204.status = 204
    resp_204.__enter__.return_value = resp_204

    mock_open.side_effect = [resp_429, resp_204]

    post_to_discord(FAKE_WEBHOOK_URL, {"test": True}, max_retries=2)
    assert mock_open.call_count == 2
    mock_sleep.assert_called_once_with(0.1)


@patch("random.uniform", return_value=0.0)
@patch("time.sleep")
@patch("mail2hook.discord._open_request")
def test_post_to_discord_retry_exhausted_500(
    mock_open: MagicMock, mock_sleep: MagicMock, _mock_rng: MagicMock
) -> None:
    mock_open.side_effect = [
        _make_http_error(500, body=b"server down"),
        _make_http_error(500, body=b"server down"),
        _make_http_error(500, body=b"server down"),
    ]

    with pytest.raises(DiscordTemporaryError, match="Discord server error HTTP 500") as exc_info:
        post_to_discord(FAKE_WEBHOOK_URL, {"test": True}, max_retries=2, initial_backoff=0.01)

    assert exc_info.value.__cause__ is None
    assert mock_open.call_count == 3


@patch("random.uniform", return_value=0.0)
@patch("time.sleep")
@patch("mail2hook.discord._open_request")
def test_post_to_discord_network_error(
    mock_open: MagicMock, mock_sleep: MagicMock, _mock_rng: MagicMock
) -> None:
    mock_open.side_effect = urllib.error.URLError("Connection refused")

    with pytest.raises(DiscordTemporaryError, match="Network error communicating with Discord"):
        post_to_discord(FAKE_WEBHOOK_URL, {"test": True}, max_retries=2, initial_backoff=0.01)

    assert mock_open.call_count == 3


@patch("mail2hook.discord._open_request")
def test_post_to_discord_permanent_error_404(mock_open: MagicMock) -> None:
    mock_open.side_effect = _make_http_error(
        404,
        headers={"Content-Type": "application/json"},
        body=b'{"message": "Unknown Webhook"}',
    )

    with pytest.raises(DiscordPermanentError, match="Discord permanent error HTTP 404") as exc_info:
        post_to_discord(FAKE_WEBHOOK_URL, {"test": True})

    assert exc_info.value.__cause__ is None
    assert mock_open.call_count == 1


def test_redact_webhook_url_and_secrets_variants() -> None:
    url_q = "https://user:pass@discord.com/api/v10/webhooks/123456789/very_secret_token_value/?wait=true"
    redacted = redact_webhook_url(url_q)
    assert redacted == "https://[REDACTED]@discord.com/api/v10/webhooks/123456789/[REDACTED]"
    assert "pass" not in redacted
    assert "very_secret_token_value" not in redacted

    err = f"Failed POST to {url_q} with token very_secret_token_value"
    safe = redact_secrets(err, url_q)
    assert "pass" not in safe
    assert "very_secret_token_value" not in safe


def test_no_redirect_handler_blocks_3xx_and_raises_temporary_error() -> None:
    handler = _NoRedirectHandler()
    req = urllib.request.Request(FAKE_WEBHOOK_URL, method="POST")
    assert (
        handler.redirect_request(req, io.BytesIO(), 302, "Found", _make_headers(), "https://x")
        is None
    )
    assert any(isinstance(h, _NoRedirectHandler) for h in _NO_REDIRECT_OPENER.handlers)

    with patch("mail2hook.discord._open_request", side_effect=_make_http_error(307)) as mock_open:
        with pytest.raises(DiscordTemporaryError, match="Unexpected redirect HTTP 307") as exc_info:
            post_to_discord(FAKE_WEBHOOK_URL, {"test": True}, max_retries=2)
    assert exc_info.value.__cause__ is None
    assert mock_open.call_count == 1


def test_parse_retry_after_precedence_and_invalid_values() -> None:
    assert _parse_retry_after(
        _make_headers({"X-RateLimit-Reset-After": "4.0", "Retry-After": "9.0"}),
        b'{"retry_after": 1.25}',
    ) == pytest.approx(1.25)

    assert _parse_retry_after(
        _make_headers({"X-RateLimit-Reset-After": "3.5", "Retry-After": "9.0"}),
        b'{"retry_after": "NaN"}',
    ) == pytest.approx(3.5)

    assert _parse_retry_after(
        _make_headers({"X-RateLimit-Reset-After": "-2", "Retry-After": "6.0"}),
        b"{invalid-json",
    ) == pytest.approx(6.0)

    # Use deterministic future timestamp 10s ahead
    with patch("time.time", return_value=1_700_000_000.0):
        future_http_date = formatdate(1_700_000_010.0, usegmt=True)
        parsed_date = _parse_retry_after(_make_headers({"Retry-After": future_http_date}), b"")
        assert parsed_date == pytest.approx(10.0)

        past_http_date = formatdate(1_699_999_990.0, usegmt=True)
        assert _parse_retry_after(_make_headers({"Retry-After": past_http_date}), b"") == 2.0

    assert _parse_retry_after(_make_headers({"Retry-After": "nan"}), b'{"retry_after": -1}') == 2.0
    assert _parse_retry_after(_make_headers({"Retry-After": "inf"}), b"") == 2.0


@patch("random.uniform", return_value=0.25)
@patch("time.sleep")
@patch("mail2hook.discord._open_request")
def test_post_to_discord_429_jitter_and_deadline_replaces_10s_cap(
    mock_open: MagicMock, mock_sleep: MagicMock, _mock_rng: MagicMock
) -> None:
    resp_204 = MagicMock()
    resp_204.status = 204
    resp_204.__enter__.return_value = resp_204

    mock_open.side_effect = [
        _make_http_error(429, body=b'{"retry_after": 12.0}'),
        resp_204,
    ]
    post_to_discord(FAKE_WEBHOOK_URL, {"test": True}, max_retries=1, total_timeout=60.0)
    mock_sleep.assert_called_once_with(12.25)

    mock_sleep.reset_mock()
    mock_open.side_effect = [_make_http_error(429, body=b'{"retry_after": 30.0}')]
    with pytest.raises(DiscordTemporaryError, match="exceeds deadline"):
        post_to_discord(FAKE_WEBHOOK_URL, {"test": True}, max_retries=1, total_timeout=5.0)
    mock_sleep.assert_not_called()


@patch("random.uniform", return_value=0.0)
@patch("time.sleep")
@patch("mail2hook.discord._open_request")
def test_post_to_discord_408_and_cloudflare_403_vs_json_403(
    mock_open: MagicMock, mock_sleep: MagicMock, _mock_rng: MagicMock
) -> None:
    resp_204 = MagicMock()
    resp_204.status = 204
    resp_204.__enter__.return_value = resp_204

    # 408 Request Timeout is temporary & retried
    mock_open.side_effect = [_make_http_error(408), resp_204]
    post_to_discord(FAKE_WEBHOOK_URL, {"test": True}, max_retries=1, initial_backoff=0.01)
    assert mock_open.call_count == 2

    # Cloudflare HTML 403 is temporary & raises DiscordTemporaryError when exhausted
    mock_open.reset_mock()
    mock_open.side_effect = [
        _make_http_error(403, headers={"Content-Type": "text/html"}, body=b"<html>Blocked</html>"),
        _make_http_error(403, headers={"Content-Type": "text/html"}, body=b"<html>Blocked</html>"),
    ]
    with pytest.raises(DiscordTemporaryError, match="HTTP 403"):
        post_to_discord(FAKE_WEBHOOK_URL, {"test": True}, max_retries=1, initial_backoff=0.01)
    assert mock_open.call_count == 2

    # Discord JSON 403 is permanent & not retried
    mock_open.reset_mock()
    mock_open.side_effect = _make_http_error(
        403,
        headers={"Content-Type": "application/json"},
        body=b'{"message": "Missing Access", "code": 50001}',
    )
    with pytest.raises(DiscordPermanentError, match="HTTP 403") as exc_info:
        post_to_discord(FAKE_WEBHOOK_URL, {"test": True}, max_retries=2)
    assert exc_info.value.status_code == 403
    assert mock_open.call_count == 1


@patch("random.uniform", return_value=0.0)
@patch("time.sleep")
@patch("mail2hook.discord._open_request")
def test_post_to_discord_http_exception_fresh_request_bounded_read_and_redaction(
    mock_open: MagicMock, mock_sleep: MagicMock, _mock_rng: MagicMock
) -> None:
    seen_requests: list[urllib.request.Request] = []
    resp_204 = MagicMock()
    resp_204.status = 204
    resp_204.__enter__.return_value = resp_204

    def side_effect_http_exc(req: urllib.request.Request, timeout: float = 15.0) -> object:
        seen_requests.append(req)
        if len(seen_requests) == 1:
            raise http.client.RemoteDisconnected(f"closed {FAKE_WEBHOOK_URL}")
        return resp_204

    mock_open.side_effect = side_effect_http_exc
    post_to_discord(FAKE_WEBHOOK_URL, {"test": True}, max_retries=1, initial_backoff=0.01)
    assert len(seen_requests) == 2
    assert seen_requests[0] is not seen_requests[1]

    # Exhausted RemoteDisconnected redacts secret and clears __cause__
    mock_open.side_effect = http.client.RemoteDisconnected(f"closed {FAKE_WEBHOOK_URL}")
    with pytest.raises(DiscordTemporaryError) as exc_info:
        post_to_discord(FAKE_WEBHOOK_URL, {"test": True}, max_retries=0)
    assert "secret-token" not in str(exc_info.value)
    assert "[REDACTED]" in str(exc_info.value)
    assert exc_info.value.__cause__ is None

    # Bounded error read (4096) + closed + secret redaction on permanent 400
    err_400 = _make_http_error(
        400,
        headers={"Content-Type": "application/json"},
        body=f"bad request to {FAKE_WEBHOOK_URL}".encode(),
    )
    mock_open.side_effect = err_400
    with (
        patch.object(err_400, "read", wraps=err_400.read) as spy_read,
        patch.object(err_400, "close", wraps=err_400.close) as spy_close,
        pytest.raises(DiscordPermanentError) as perm_exc,
    ):
        post_to_discord(FAKE_WEBHOOK_URL, {"test": True})
    spy_read.assert_called_once_with(MAX_ERROR_BODY_BYTES)
    spy_close.assert_called_once()
    assert "secret-token" not in str(perm_exc.value)
    assert perm_exc.value.__cause__ is None


def test_build_payload_to_field_and_total_embed_limit() -> None:
    config = Config(discord_webhook_url=FAKE_WEBHOOK_URL, max_body_chars=8000)
    msg = ParsedMessage(
        sender="root@server01.example.com",
        to="ops@example.com",
        subject="S" * 300,
        date="Wed, 11 Nov 2026 12:00:00 +0000",
        body="X" * 7000,
        status="info",
        hostname="H" * 300,
    )
    payload = build_payload(msg, config)
    embed = payload["embeds"][0]
    to_field = next(f for f in embed["fields"] if f["name"] == "To")
    assert to_field["value"] == "ops@example.com"
    assert len(embed["title"]) <= 256
    assert len(embed["description"]) <= 4096
    assert len(embed["author"]["name"]) <= 256
    total_chars = (
        len(embed["title"])
        + len(embed["description"])
        + len(embed["author"]["name"])
        + len(embed["footer"]["text"])
        + sum(len(f["name"]) + len(f["value"]) for f in embed["fields"])
    )
    assert total_chars <= 6000
