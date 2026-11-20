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
    assert 'Unattended-Upgrade::MailReport "on-change";' in captured.out
    assert 'Unattended-Upgrade::MailOnlyOnError "false";' in captured.out
    assert "52mail2hook" in captured.out


def test_cli_missing_config(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("DISCORD_WEBHOOK", raising=False)
    monkeypatch.delenv("WEBHOOK_URL", raising=False)
    monkeypatch.setattr("mail2hook.config.DEFAULT_SYSTEM_ENV_PATH", tmp_path / "no_system_env")
    monkeypatch.chdir(tmp_path)
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
        mock_post.side_effect = DiscordPermanentError("400 Bad Request", status_code=400)
        exit_code = run(["--file", fixture_path])

    assert exit_code == EXIT_DATAERR
    captured = capsys.readouterr()
    assert "permanent failure sending to Discord" in captured.err

    with patch("mail2hook.cli.post_to_discord") as mock_post:
        mock_post.side_effect = DiscordPermanentError("404 Not Found", status_code=404)
        exit_code_404 = run(["--file", fixture_path])

    assert exit_code_404 == EXIT_CONFIG


def test_cli_mailq_mode(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = run(["-bp"])
    assert exit_code == EXIT_OK
    captured = capsys.readouterr()
    assert "Mail queue is empty" in captured.out


def test_cli_verify_mode(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = run(["-bv", "root@example.com"])
    assert exit_code == EXIT_OK
    captured = capsys.readouterr()
    assert "Recipient OK: deliverable via mail2hook" in captured.out


def test_cli_sendmail_standard_invocation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", VALID_WEBHOOK_URL)
    raw_eml = (FIXTURES_DIR / "unattended_upgrades_success.eml").read_bytes()
    mock_stdin = MagicMock()
    mock_stdin.buffer = io.BytesIO(raw_eml)
    monkeypatch.setattr("sys.stdin", mock_stdin)

    with patch("mail2hook.cli.post_to_discord") as mock_post:
        # Standard invocation used by unattended-upgrades and cron:
        exit_code = run(["-t", "-oi", "-f", "root@server01.example.com", "mail2hook"])

    assert exit_code == EXIT_OK
    assert mock_post.call_count == 1


def test_cli_sendmail_envelope_sender_and_dot_eof(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", VALID_WEBHOOK_URL)
    raw_eml = b"Subject: Cron job output\n\nLine one\n..leading dot\n.\nIgnored after single dot\n"
    mock_stdin = MagicMock()
    mock_stdin.buffer = io.BytesIO(raw_eml)
    monkeypatch.setattr("sys.stdin", mock_stdin)

    with patch("mail2hook.cli.post_to_discord") as mock_post:
        exit_code = run(["-f", "root@server01.example.com", "-F", "Cron Daemon", "root"])

    assert exit_code == EXIT_OK
    assert mock_post.call_count == 1
    payload = mock_post.call_args[0][1]
    embed = payload["embeds"][0]
    assert ".leading dot" in embed["description"]
    assert "Ignored after single dot" not in embed["description"]
    from_field = next(f for f in embed["fields"] if f["name"] == "From")
    assert "Cron Daemon (root@server01.example.com)" == from_field["value"]
    to_field = next(f for f in embed["fields"] if f["name"] == "To")
    assert to_field["value"] == "root"


def test_cli_oversized_input(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", VALID_WEBHOOK_URL)
    monkeypatch.setattr("mail2hook.cli.MAX_MESSAGE_BYTES", 32)
    mock_stdin = MagicMock()
    mock_stdin.buffer = io.BytesIO(b"Subject: Too large\n\n" + b"A" * 64)
    monkeypatch.setattr("sys.stdin", mock_stdin)

    exit_code = run([])
    assert exit_code == EXIT_DATAERR
    captured = capsys.readouterr()
    assert "exceeds maximum limit" in captured.err


def test_cli_postfix_pipe_invocation_unattended_upgrades(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", VALID_WEBHOOK_URL)
    monkeypatch.setenv("FILTER_SUBJECT_KEYWORDS", "unattended-upgrades")
    raw_eml = (
        b"From: unattended-upgrades <unattended-upgrades@pve01.example.com>\n"
        b"To: mail2hook@localhost\n"
        b"Subject: unattended-upgrades result for pve01.example.com: SUCCESS\n"
        b"\n"
        b"Unattended upgrade result: All upgrades installed\n\n"
        b"Packages that were upgraded:\n"
        b" nginx\n"
    )
    mock_stdin = MagicMock()
    mock_stdin.buffer = io.BytesIO(raw_eml)
    monkeypatch.setattr("sys.stdin", mock_stdin)

    with patch("mail2hook.cli.post_to_discord") as mock_post:
        exit_code = run(
            [
                "-oi",
                "-f",
                "unattended-upgrades@pve01.example.com",
                "--",
                "mail2hook@localhost",
            ]
        )

    assert exit_code == EXIT_OK
    assert mock_post.call_count == 1
    payload = mock_post.call_args[0][1]
    embed = payload["embeds"][0]
    server_field = next(f for f in embed["fields"] if f["name"] == "Server")
    to_field = next(f for f in embed["fields"] if f["name"] == "To")
    assert server_field["value"] == "`pve01.example.com`"
    assert to_field["value"] == "mail2hook@localhost"


@pytest.mark.parametrize(
    "raw_eml",
    [
        (
            b"From: Cron Daemon <root@server01.example.com>\n"
            b"To: root\n"
            b"Subject: Cron <root@server01.example.com> /usr/local/bin/backup\n\n"
            b"Backup completed\n"
        ),
        (
            b"From: root <root@server01.example.com>\n"
            b"To: root\n"
            b"Subject: apt-listchanges: news for server01.example.com\n\n"
            b"Package changelog details\n"
        ),
    ],
)
def test_cli_default_subject_filter_drops_non_upgrade_mail(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    raw_eml: bytes,
) -> None:
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", VALID_WEBHOOK_URL)
    monkeypatch.setenv("FILTER_SUBJECT_KEYWORDS", "unattended-upgrades")
    mock_stdin = MagicMock()
    mock_stdin.buffer = io.BytesIO(raw_eml)
    monkeypatch.setattr("sys.stdin", mock_stdin)

    with patch("mail2hook.cli.post_to_discord") as mock_post:
        exit_code = run(["-oi", "-t"])

    assert exit_code == EXIT_OK
    mock_post.assert_not_called()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_cli_default_subject_filter_forwards_unattended_upgrades_mail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", VALID_WEBHOOK_URL)
    monkeypatch.setenv("FILTER_SUBJECT_KEYWORDS", "unattended-upgrades")
    raw_eml = (
        b"From: unattended-upgrades <root@server01.example.com>\n"
        b"To: mail2hook@localhost\n"
        b"Subject: [reboot required] unattended-upgrades result for server01.example.com: SUCCESS\n"
        b"\n"
        b"Unattended upgrade result: All upgrades installed\n"
    )
    mock_stdin = MagicMock()
    mock_stdin.buffer = io.BytesIO(raw_eml)
    monkeypatch.setattr("sys.stdin", mock_stdin)

    with patch("mail2hook.cli.post_to_discord") as mock_post:
        exit_code = run(["-oi", "-t"])

    assert exit_code == EXIT_OK
    assert mock_post.call_count == 1


def test_cli_e2e_marker_success_filtered_failure_and_dry_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    marker_file = tmp_path / "e2e-confirm"
    marker_file.touch()
    monkeypatch.setattr("mail2hook.cli.E2E_MARKER_PATH", marker_file)
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", VALID_WEBHOOK_URL)

    token = "0123456789abcdef0123456789abcdef"
    raw_eml = (
        f"From: root\nTo: mail2hook@localhost\n"
        f"Subject: unattended-upgrades install test\n"
        f"X-Mail2Hook-Test-Id: {token}\n\n"
        f"Hello\n"
    ).encode("ascii")

    # 1. --dry-run must NOT touch the marker
    mock_stdin = MagicMock()
    mock_stdin.buffer = io.BytesIO(raw_eml)
    monkeypatch.setattr("sys.stdin", mock_stdin)
    assert run(["--dry-run"]) == EXIT_OK
    assert marker_file.read_text(encoding="utf-8") == ""

    # 2. Successful delivery writes ok:<token>
    mock_stdin.buffer = io.BytesIO(raw_eml)
    with patch("mail2hook.cli.post_to_discord"):
        assert run([]) == EXIT_OK
    assert marker_file.read_text(encoding="utf-8") == f"ok:{token}\n"

    # 3. Filtered out writes filtered:<token>
    monkeypatch.setenv("FILTER_SUBJECT_KEYWORDS", "something-else")
    mock_stdin.buffer = io.BytesIO(raw_eml)
    with patch("mail2hook.cli.post_to_discord"):
        assert run([]) == EXIT_OK
    assert marker_file.read_text(encoding="utf-8") == f"filtered:{token}\n"

    # 4. Delivery failure writes failed:<code>:<token>
    monkeypatch.delenv("FILTER_SUBJECT_KEYWORDS")
    mock_stdin.buffer = io.BytesIO(raw_eml)
    with patch(
        "mail2hook.cli.post_to_discord",
        side_effect=DiscordPermanentError("404 Not Found", status_code=404),
    ):
        assert run([]) == EXIT_CONFIG
    assert marker_file.read_text(encoding="utf-8") == f"failed:78:{token}\n"

    # 5. Config failure writes failed:78:<token>
    monkeypatch.delenv("DISCORD_WEBHOOK_URL")
    mock_stdin.buffer = io.BytesIO(raw_eml)
    assert run([]) == EXIT_CONFIG
    assert marker_file.read_text(encoding="utf-8") == f"failed:78:{token}\n"


def test_cli_e2e_marker_ignores_missing_symlink_and_bad_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    token = "0123456789abcdef0123456789abcdef"
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", VALID_WEBHOOK_URL)

    # 1. Absent marker file is never created
    absent_marker = tmp_path / "does-not-exist"
    monkeypatch.setattr("mail2hook.cli.E2E_MARKER_PATH", absent_marker)
    mock_stdin = MagicMock()
    mock_stdin.buffer = io.BytesIO(
        f"Subject: unattended-upgrades test\nX-Mail2Hook-Test-Id: {token}\n\nHi\n".encode("ascii")
    )
    monkeypatch.setattr("sys.stdin", mock_stdin)
    with patch("mail2hook.cli.post_to_discord"):
        assert run([]) == EXIT_OK
    assert not absent_marker.exists()

    # 2. Symlink marker is never followed
    target_file = tmp_path / "protected"
    target_file.write_text("unchanged", encoding="utf-8")
    symlink_marker = tmp_path / "e2e-link"
    symlink_marker.symlink_to(target_file)
    monkeypatch.setattr("mail2hook.cli.E2E_MARKER_PATH", symlink_marker)
    mock_stdin.buffer = io.BytesIO(
        f"Subject: unattended-upgrades test\nX-Mail2Hook-Test-Id: {token}\n\nHi\n".encode("ascii")
    )
    with patch("mail2hook.cli.post_to_discord"):
        assert run([]) == EXIT_OK
    assert target_file.read_text(encoding="utf-8") == "unchanged"

    # 3. Invalid token does not write marker
    real_marker = tmp_path / "e2e-real"
    real_marker.touch()
    monkeypatch.setattr("mail2hook.cli.E2E_MARKER_PATH", real_marker)
    mock_stdin.buffer = io.BytesIO(
        b"Subject: unattended-upgrades test\nX-Mail2Hook-Test-Id: bad-token!\n\nHi\n"
    )
    with patch("mail2hook.cli.post_to_discord"):
        assert run([]) == EXIT_OK
    assert real_marker.read_text(encoding="utf-8") == ""
