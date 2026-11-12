"""Tests for the mail2hook command-line interface."""

from __future__ import annotations

import io
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from mail2hook import __version__
from mail2hook.cli import (
    EXIT_CONFIG,
    EXIT_DATAERR,
    EXIT_OK,
    EXIT_TEMPFAIL,
    run,
)
from mail2hook.discord import DiscordPermanentError, DiscordTemporaryError

FIXTURES_DIR = Path(__file__).parent / "fixtures"
VALID_WEBHOOK_URL = (
    "https://discord.com/api/webhooks/123456789012345678/abcdefghijklmnopqrstuvwxyz0123456789"
)


def test_cli_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        run(["--version"])
    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    assert f"mail2hook {__version__}" in captured.out


def test_cli_print_apt_config(capsys: pytest.CaptureFixture[str]) -> None:
    # Does not require DISCORD_WEBHOOK_URL
    exit_code = run(["--print-apt-config"])
    assert exit_code == EXIT_OK
    captured = capsys.readouterr()
    assert "Unattended-Upgrade::Mail" in captured.out
    assert "52mail2hook" in captured.out


def test_cli_missing_config(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    # Ensure no default env file is picked up
    exit_code = run(["--dry-run"])
    assert exit_code == EXIT_CONFIG
    captured = capsys.readouterr()
    assert "configuration error: Missing required environment variable" in captured.err


def test_cli_file_not_found(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", VALID_WEBHOOK_URL)
    exit_code = run(["--file", "/nonexistent/path/email.eml"])
    assert exit_code == EXIT_DATAERR
    captured = capsys.readouterr()
    assert "input file not found" in captured.err


def test_cli_empty_stdin(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", VALID_WEBHOOK_URL)
    mock_stdin = MagicMock()
    mock_stdin.buffer = io.BytesIO(b"")
    monkeypatch.setattr("sys.stdin", mock_stdin)
    exit_code = run([])
    assert exit_code == EXIT_DATAERR
    captured = capsys.readouterr()
    assert "unparseable email data" in captured.err


def test_cli_filtered_out_exits_zero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", VALID_WEBHOOK_URL)
    monkeypatch.setenv("FILTER_SENDERS", "someone_else@example.com")

    fixture_path = str(FIXTURES_DIR / "unattended_upgrades_success.eml")
    exit_code = run(["--file", fixture_path])

    assert exit_code == EXIT_OK
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_cli_dry_run(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", VALID_WEBHOOK_URL)
    fixture_path = str(FIXTURES_DIR / "unattended_upgrades_success.eml")

    with patch("mail2hook.cli.post_to_discord") as mock_post:
        exit_code = run(["--file", fixture_path, "--dry-run"])

    assert exit_code == EXIT_OK
    mock_post.assert_not_called()

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["username"] == "mail2hook"
    assert payload["allowed_mentions"] == {"parse": []}
    assert "SUCCESS" in payload["embeds"][0]["title"]


def test_cli_stdin_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", VALID_WEBHOOK_URL)
    raw_eml = (FIXTURES_DIR / "unattended_upgrades_success.eml").read_bytes()
    mock_stdin = MagicMock()
    mock_stdin.buffer = io.BytesIO(raw_eml)
    monkeypatch.setattr("sys.stdin", mock_stdin)

    with patch("mail2hook.cli.post_to_discord") as mock_post:
        exit_code = run([])

    assert exit_code == EXIT_OK
    assert mock_post.call_count == 1
    args = mock_post.call_args[0]
    assert args[0] == VALID_WEBHOOK_URL
    assert "SUCCESS" in args[1]["embeds"][0]["title"]


def test_cli_temporary_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", VALID_WEBHOOK_URL)
    fixture_path = str(FIXTURES_DIR / "unattended_upgrades_success.eml")

    with patch("mail2hook.cli.post_to_discord") as mock_post:
        mock_post.side_effect = DiscordTemporaryError("503 Service Unavailable")
        exit_code = run(["--file", fixture_path])

    assert exit_code == EXIT_TEMPFAIL
    captured = capsys.readouterr()
    assert "temporary failure sending to Discord" in captured.err


def test_cli_permanent_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", VALID_WEBHOOK_URL)
    fixture_path = str(FIXTURES_DIR / "unattended_upgrades_success.eml")

    with patch("mail2hook.cli.post_to_discord") as mock_post:
        mock_post.side_effect = DiscordPermanentError("404 Not Found")
        exit_code = run(["--file", fixture_path])

    assert exit_code == EXIT_DATAERR
    captured = capsys.readouterr()
    assert "permanent failure sending to Discord" in captured.err
