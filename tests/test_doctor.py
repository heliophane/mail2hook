"""Tests for mail2hook --doctor and --send-test diagnostics."""

from __future__ import annotations

import io
import os
from pathlib import Path
from typing import Any

import pytest

from mail2hook import cli, doctor
from mail2hook.discord import DiscordPermanentError, DiscordTemporaryError

_FAKE_WEBHOOK = "https://discord.com/api/webhooks/123456789012345678/UltraSecretWebhookToken999"


def test_inspect_sendmail_classifications(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Classify missing, broken symlink, mail2hook shim, Postfix, and Exim sendmail paths."""
    missing = tmp_path / "missing-sendmail"
    assert doctor.inspect_sendmail(missing)[1] == "missing"

    broken = tmp_path / "broken-sendmail"
    broken.symlink_to(tmp_path / "nonexistent-target")
    assert doctor.inspect_sendmail(broken)[1] == "broken symlink"

    shim_target = tmp_path / "mail2hook"
    shim_target.write_text("#!/usr/bin/env python3\n# mail2hook entrypoint\n", encoding="utf-8")
    shim_link = tmp_path / "sendmail-shim"
    shim_link.symlink_to(shim_target)
    assert doctor.inspect_sendmail(shim_link)[1] == "mail2hook shim"

    postfix_bin = tmp_path / "sendmail-postfix"
    postfix_bin.write_bytes(b"\x7fELF\x00\x00\x00")
    monkeypatch.setattr(
        doctor,
        "_run_cmd",
        lambda cmd: (0, f"postfix: {postfix_bin}\n", "") if cmd[0] == "dpkg-query" else None,
    )
    assert doctor.inspect_sendmail(postfix_bin)[1] == "Postfix"

    exim_bin = tmp_path / "sendmail-exim"
    exim_bin.write_bytes(b"\x7fELF\x00\x00\x00")
    monkeypatch.setattr(
        doctor,
        "_run_cmd",
        lambda cmd: (
            (0, f"exim4-daemon-light: {exim_bin}\n", "") if cmd[0] == "dpkg-query" else None
        ),
    )
    assert doctor.inspect_sendmail(exim_bin)[1] == "Exim"


def test_inspect_postfix_route_states(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Detect unconfigured, partially configured, and fully configured Postfix pipe routes."""
    main_cf = tmp_path / "main.cf"
    master_cf = tmp_path / "master.cf"
    transport = tmp_path / "mail2hook_transport"

    unconfigured = doctor.inspect_postfix_route(
        main_cf=main_cf, master_cf=master_cf, transport_file=transport
    )
    assert unconfigured["status"] == "not configured"

    master_cf.write_text(
        "mail2hook unix - n n - - pipe\n"
        "  flags=R user=nobody:mail "
        "argv=/usr/local/bin/mail2hook -oi -f ${sender} -- ${recipient}\n",
        encoding="utf-8",
    )
    partial = doctor.inspect_postfix_route(
        main_cf=main_cf, master_cf=master_cf, transport_file=transport
    )
    assert partial["status"].startswith("partially configured")

    main_cf.write_text(
        f"transport_maps = hash:{transport}\nmail2hook_destination_recipient_limit = 1\n",
        encoding="utf-8",
    )
    transport.write_text("mail2hook@localhost    mail2hook:\n", encoding="utf-8")
    Path(f"{transport}.db").write_bytes(b"db")
    monkeypatch.setattr(
        doctor,
        "_run_cmd",
        lambda cmd: (0, "mail2hook:\n", "") if cmd[0] == "postmap" else None,
    )
    configured = doctor.inspect_postfix_route(
        main_cf=main_cf, master_cf=master_cf, transport_file=transport
    )
    assert configured["status"] == "configured"
    assert configured["recipient_limit"] == "1"


def test_inspect_env_file_eacces_and_readable(tmp_path: Path) -> None:
    """Report permission denied distinctly from missing when directory is unreadable."""
    if os.geteuid() == 0:
        pytest.skip("chmod 000 does not deny root")

    env_dir = tmp_path / "mail2hook-cfg"
    env_dir.mkdir(mode=0o755)
    env_file = env_dir / "env"
    env_file.write_text(f"DISCORD_WEBHOOK_URL={_FAKE_WEBHOOK}\n", encoding="utf-8")

    readable_info = doctor.inspect_env_file(env_file)
    assert readable_info["exists"] == "yes"
    assert readable_info["readable"] == "yes"

    try:
        env_dir.chmod(0o000)
        denied_info = doctor.inspect_env_file(env_file)
        assert "permission denied" in denied_info["exists"]
        assert denied_info["readable"].startswith("no (EACCES")
    finally:
        env_dir.chmod(0o755)


def test_doctor_report_no_secret_leak_and_sample_dry_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Output full diagnostics and dry-run result without ever printing the webhook token."""
    env_file = tmp_path / "env"
    env_file.write_text(
        f"DISCORD_WEBHOOK_URL={_FAKE_WEBHOOK}\n"
        "SERVER_NAME=server01.example.com\n"
        "FILTER_SUBJECT_KEYWORDS=unattended-upgrades\n"
        "DISCORD_USERNAME=mail2hook-test\n",
        encoding="utf-8",
    )

    def fake_run_cmd(cmd: list[str], timeout: float = 5.0) -> tuple[int, str, str] | None:
        if cmd[0] == "dpkg-query" and "-W" in cmd:
            return (0, "postfix\tii \t3.9.1-1\n", "")
        if cmd[0] == "apt-config":
            return (
                0,
                'Unattended-Upgrade::Mail "mail2hook@localhost";\n'
                'Unattended-Upgrade::MailOnlyOnError "false";\n',
                "",
            )
        return None

    monkeypatch.setattr(doctor, "_run_cmd", fake_run_cmd)
    code = cli.run(["--doctor", "--env-file", str(env_file)])
    captured = capsys.readouterr()

    assert code == cli.EXIT_OK
    assert "UltraSecretWebhookToken999" not in captured.out
    assert "UltraSecretWebhookToken999" not in captured.err
    assert "123456789012345678" in captured.out
    assert "server01.example.com" in captured.out
    assert "unattended-upgrades" in captured.out
    assert "mail2hook-test" in captured.out
    assert "matched (would post)" in captured.out
    assert "postfix (3.9.1-1)" in captured.out
    assert 'Unattended-Upgrade::Mail "mail2hook@localhost";' in captured.out


def test_doctor_missing_config_still_prints_diagnostics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Return EXIT_CONFIG on invalid config while still printing environment diagnostics."""
    monkeypatch.setattr(doctor, "_run_cmd", lambda cmd: None)
    buf = io.StringIO()
    code = doctor.run_doctor(env_file=tmp_path / "missing-env", out=buf)
    report = buf.getvalue()

    assert code == cli.EXIT_CONFIG
    assert "Version" in report
    assert "Sendmail path" in report
    assert "Postfix route" in report
    assert "Config status      : ERROR" in report


def test_doctor_send_test_requires_doctor(capsys: pytest.CaptureFixture[str]) -> None:
    """Reject --send-test unless paired with --doctor."""
    code = cli.run(["--send-test"])
    captured = capsys.readouterr()
    assert code == cli.EXIT_CONFIG
    assert "--send-test requires --doctor" in captured.err


def test_doctor_send_test_success_and_error_redaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Verify --doctor --send-test posts a labeled payload and redacts tokens on errors."""
    env_file = tmp_path / "env"
    env_file.write_text(f"DISCORD_WEBHOOK_URL={_FAKE_WEBHOOK}\n", encoding="utf-8")
    monkeypatch.setattr(doctor, "_run_cmd", lambda cmd: None)

    sent: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(doctor, "post_to_discord", lambda url, payload: sent.append((url, payload)))

    code = cli.run(["--doctor", "--send-test", "--env-file", str(env_file)])
    captured = capsys.readouterr()
    assert code == cli.EXIT_OK
    assert len(sent) == 1
    assert sent[0][0] == _FAKE_WEBHOOK
    assert sent[0][1]["allowed_mentions"] == {"parse": []}
    assert "mail2hook --doctor --send-test" in sent[0][1]["embeds"][0]["title"]
    assert "Send test          : OK" in captured.out
    assert "UltraSecretWebhookToken999" not in captured.out
    assert "UltraSecretWebhookToken999" not in captured.err

    def raise_temp(url: str, payload: dict[str, Any]) -> None:
        raise DiscordTemporaryError(f"503 calling {url}")

    monkeypatch.setattr(doctor, "post_to_discord", raise_temp)
    assert cli.run(["--doctor", "--send-test", "--env-file", str(env_file)]) == cli.EXIT_TEMPFAIL
    err_out = capsys.readouterr().err
    assert "UltraSecretWebhookToken999" not in err_out
    assert "[REDACTED]" in err_out

    def raise_404(url: str, payload: dict[str, Any]) -> None:
        raise DiscordPermanentError(f"404 calling {url}", status_code=404)

    monkeypatch.setattr(doctor, "post_to_discord", raise_404)
    assert cli.run(["--doctor", "--send-test", "--env-file", str(env_file)]) == cli.EXIT_CONFIG
    err_out_404 = capsys.readouterr().err
    assert "UltraSecretWebhookToken999" not in err_out_404

    def raise_400(url: str, payload: dict[str, Any]) -> None:
        raise DiscordPermanentError("bad request", status_code=400)

    monkeypatch.setattr(doctor, "post_to_discord", raise_400)
    assert cli.run(["--doctor", "--send-test", "--env-file", str(env_file)]) == cli.EXIT_DATAERR
    capsys.readouterr()
