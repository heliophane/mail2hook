"""Tests for email parsing and body extraction."""

from __future__ import annotations

from pathlib import Path

import pytest

from mail2hook.parse import (
    PackageUpgrade,
    ParseError,
    detect_status,
    extract_hostname,
    extract_package_versions,
    extract_upgraded_packages,
    format_unattended_upgrades_body,
    parse_email,
    strip_html,
    truncate_text,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def test_truncate_text() -> None:
    text = "Hello world, this is a longer string."
    assert truncate_text(text, 50) == text
    assert truncate_text(text, 10) == "Hello wor…"
    assert truncate_text(text, 1) == "…"


def test_strip_html() -> None:
    raw_html = """
    <html>
      <head><title>Ignore Me</title><style>.hidden { display: none; }</style></head>
      <body>
        <h1>Heading</h1>
        <p>First paragraph with <a href="#">link</a> &amp; special characters.</p>
        <div>Second section</div>
      </body>
    </html>
    """
    cleaned = strip_html(raw_html)
    assert "Ignore Me" not in cleaned
    assert ".hidden" not in cleaned
    assert "Heading" in cleaned
    assert "First paragraph with link & special characters." in cleaned
    assert "Second section" in cleaned


def test_parse_empty_input() -> None:
    with pytest.raises(ParseError, match="Cannot parse empty email input"):
        parse_email(b"")
    with pytest.raises(ParseError, match="Cannot parse empty email input"):
        parse_email(b"   \n\t  ")


def test_parse_unattended_upgrades_success() -> None:
    eml_bytes = (FIXTURES_DIR / "unattended_upgrades_success.eml").read_bytes()
    parsed = parse_email(eml_bytes, check_system_reboot=False)

    assert "unattended-upgrades result for server01.example.com: SUCCESS" in parsed.subject
    assert "root@server01.example.com" in parsed.sender
    assert parsed.status == "success"
    assert parsed.hostname == "server01.example.com"
    assert "All upgrades installed" in parsed.body
    # Packages are formatted as a vertical list with old -> new versions
    expected_lines = (
        "Packages that were upgraded:\n"
        "• `libssl3`: `3.0.14-1~deb12u1` → `3.0.15-1~deb12u1`\n"
        "• `openssl`: `3.0.14-1~deb12u1` → `3.0.15-1~deb12u1`\n"
        "• `curl`: `7.88.1-10+deb12u7` → `7.88.1-10+deb12u8`\n"
        "• `libcurl4`: `7.88.1-10+deb12u7` → `7.88.1-10+deb12u8`"
    )
    assert expected_lines in parsed.body
    # Ensure "Allowed origins are:" does not falsely trigger "Errors encountered:"
    assert "Errors encountered:" not in parsed.body
    assert "Allowed origins are:" not in parsed.body
    assert parsed.upgraded_packages == [
        PackageUpgrade("libssl3", "3.0.14-1~deb12u1", "3.0.15-1~deb12u1"),
        PackageUpgrade("openssl", "3.0.14-1~deb12u1", "3.0.15-1~deb12u1"),
        PackageUpgrade("curl", "7.88.1-10+deb12u7", "7.88.1-10+deb12u8"),
        PackageUpgrade("libcurl4", "7.88.1-10+deb12u7", "7.88.1-10+deb12u8"),
    ]


def test_parse_unattended_upgrades_failure() -> None:
    eml_bytes = (FIXTURES_DIR / "unattended_upgrades_failure.eml").read_bytes()
    parsed = parse_email(eml_bytes, check_system_reboot=False)

    assert "FAILURE" in parsed.subject
    assert parsed.status == "failure"
    assert parsed.hostname == "server01.example.com"
    assert "Some errors were encountered" in parsed.body
    assert "• `base-files`: `12.4+deb12u7` → `12.4+deb12u8`" in parsed.body
    assert "• `broken-pkg-example`" in parsed.body
    assert "dpkg: error" in parsed.body.lower() and "errors encountered:" in parsed.body.lower()


def test_parse_html_only() -> None:
    eml_bytes = (FIXTURES_DIR / "html_only.eml").read_bytes()
    parsed = parse_email(eml_bytes, check_system_reboot=False)

    assert parsed.subject == "[ALERT] High memory usage detected"
    assert "alerts@monitor.example.com" in parsed.sender
    assert parsed.status == "failure"  # Contains 'ALERT'
    assert "System Resource Alert" in parsed.body
    assert "Server server01.example.com has exceeded 90% memory threshold." in parsed.body
    assert "Current usage: 94.2%" in parsed.body
    assert "<html>" not in parsed.body


def test_parse_non_utf8_charset() -> None:
    eml_bytes = (FIXTURES_DIR / "non_utf8.eml").read_bytes()
    parsed = parse_email(eml_bytes, check_system_reboot=False)

    # Subject is encoded in ISO-8859-1 Q-encoding: =?ISO-8859-1?Q?Rapport_d=27activit=E9?=
    assert "Rapport d'activité" in parsed.subject
    # Sender is encoded in ISO-8859-1 Q-encoding: =?ISO-8859-1?Q?Mise_=E0_jour?=
    assert "Mise à jour" in parsed.sender
    # Body contains accented French characters
    assert "Opération terminée avec succès" in parsed.body
    assert "contrôle" in parsed.body


def test_parse_missing_headers() -> None:
    raw = b"Just a plain body with no headers.\nSecond line.\n"
    parsed = parse_email(raw, check_system_reboot=False)
    assert parsed.sender == "(unknown sender)"
    assert parsed.subject == "(no subject)"
    assert parsed.date == ""
    assert "Just a plain body" in parsed.body

    parsed_fallback = parse_email(
        raw, fallback_sender="Cron Daemon <root@server01.example.com>", check_system_reboot=False
    )
    assert parsed_fallback.sender == "Cron Daemon <root@server01.example.com>"
    assert parsed_fallback.hostname == "server01.example.com"


def test_detect_status() -> None:
    assert detect_status("Weekly cron report", "All jobs completed") == "info"
    assert detect_status("Backup failed: timeout", "Connection lost") == "failure"
    assert detect_status("Database error alert", "Crash detected") == "failure"
    assert detect_status("Daily status: SUCCESS", "All good") == "success"
    # Avoid substring false positives on 'fail2ban' or '0 errors'
    assert detect_status("fail2ban daily summary", "Banned 3 IPs") == "info"
    assert detect_status("Upgrade report: 0 errors", "Completed") == "info"


def test_extract_hostname() -> None:
    assert (
        extract_hostname("Upgrade result for host01.example.com: SUCCESS", "root@example.com")
        == "host01.example.com"
    )
    assert (
        extract_hostname("unattended-upgrades result for ubuntu24: SUCCESS", "root@ubuntu24")
        == "ubuntu24"
    )
    assert extract_hostname("Nightly run on server01", "root@localhost") == "server01"
    assert extract_hostname("Cron <root@web01> /usr/local/bin/backup", "root@localhost") == "web01"
    assert (
        extract_hostname("Cron job output", "root@server02.infra.local") == "server02.infra.local"
    )
    # Do not extract common English words after "on"/"for"/"at" as hostnames
    assert (
        extract_hostname("Backup failed on Monday", "root@server01.example.com")
        == "server01.example.com"
    )
    assert (
        extract_hostname("Summary report for today", "root@server01.example.com")
        == "server01.example.com"
    )
    assert extract_hostname("Generic Subject", "simpleuser") is None
    assert extract_hostname("Generic Subject", "simpleuser", fallback_to_local=True) is not None


def test_check_reboot_required(tmp_path: Path) -> None:
    from mail2hook.parse import check_reboot_required

    # Email mentions reboot
    req1, pkgs1 = check_reboot_required(
        "unattended-upgrades result",
        "Packages upgraded: linux-image-generic\nA reboot is required to finish install.",
        check_system=False,
    )
    assert req1 is True
    assert pkgs1 == []

    # Email does not mention reboot
    req2, pkgs2 = check_reboot_required(
        "unattended-upgrades result",
        "All upgrades installed successfully",
        check_system=False,
    )
    assert req2 is False
    assert pkgs2 == []

    # Email explicitly states no reboot is required
    for phrase in (
        "No reboot is required.",
        "Reboot is not required.",
        "No system restart is required.",
    ):
        req_neg, _ = check_reboot_required("result", phrase, check_system=False)
        assert req_neg is False

    # System file indicates reboot and lists packages
    flag_file = tmp_path / "reboot-required"
    pkgs_file = tmp_path / "reboot-required.pkgs"
    flag_file.write_text("*** System restart required ***\n")
    pkgs_file.write_text("linux-image-6.8.0-generic\nlibc6\n")

    req_sys, pkgs_sys = check_reboot_required(
        "unattended-upgrades result",
        "All upgrades installed",
        check_system=True,
        reboot_flag_paths=(flag_file,),
        reboot_pkgs_paths=(pkgs_file,),
    )
    assert req_sys is True
    assert pkgs_sys == ["linux-image-6.8.0-generic", "libc6"]


def test_format_unattended_upgrades_multiline_and_no_false_errors() -> None:
    body = (
        "Unattended upgrade result: All upgrades installed\n"
        "\n"
        "Packages that were upgraded:\n"
        " apache2 apache2-bin apache2-data apache2-utils libc-bin libc-dev-bin\n"
        " libc-devtools libc6 libc6-dev linux-firmware-amd-graphics libgpg-error0\n"
        "\n"
        "Package installation log:\n"
        "Log started: 2026-11-12  22:00:02\n"
        "Unpacking apache2 (2.4.58-1ubuntu8.5) over (2.4.58-1ubuntu8.4) ...\n"
        "Unpacking apache2-bin (2.4.58-1ubuntu8.5) over (2.4.58-1ubuntu8.4) ...\n"
        "Unpacking apache2-data (2.4.58-1ubuntu8.5) over (2.4.58-1ubuntu8.4) ...\n"
        "Unpacking apache2-utils (2.4.58-1ubuntu8.5) over (2.4.58-1ubuntu8.4) ...\n"
        "Unpacking libc-bin (2.39-0ubuntu8.4) over (2.39-0ubuntu8.3) ...\n"
        "Unpacking libc-dev-bin (2.39-0ubuntu8.4) over (2.39-0ubuntu8.3) ...\n"
        "Unpacking libc-devtools (2.39-0ubuntu8.4) over (2.39-0ubuntu8.3) ...\n"
        "Unpacking libc6:amd64 (2.39-0ubuntu8.4) over (2.39-0ubuntu8.3) ...\n"
        "Unpacking libc6-dev:amd64 (2.39-0ubuntu8.4) over (2.39-0ubuntu8.3) ...\n"
        "Unpacking linux-firmware-amd-graphics (20240318.git3b128b60-0ubuntu2.7) "
        "over (20240318.git3b128b60-0ubuntu2.6) ...\n"
        "Unpacking libgpg-error0:amd64 (1.47-3build2) over (1.47-3build1) ...\n"
        "Log ended: 2026-11-12  22:00:15\n"
        "\n"
        "Unattended-upgrades log:\n"
        "Allowed origins are: o=Ubuntu,a=noble-security, o=UbuntuESMApps,a=noble-apps-security\n"
    )
    formatted = format_unattended_upgrades_body(body, max_chars=2000, check_system=False)

    assert "Errors encountered:" not in formatted
    assert "Allowed origins are:" not in formatted
    assert (
        "Packages that were upgraded:\n"
        "• `apache2`: `2.4.58-1ubuntu8.4` → `2.4.58-1ubuntu8.5`\n"
        "• `apache2-bin`: `2.4.58-1ubuntu8.4` → `2.4.58-1ubuntu8.5`\n"
        "• `apache2-data`: `2.4.58-1ubuntu8.4` → `2.4.58-1ubuntu8.5`\n"
        "• `apache2-utils`: `2.4.58-1ubuntu8.4` → `2.4.58-1ubuntu8.5`\n"
        "• `libc-bin`: `2.39-0ubuntu8.3` → `2.39-0ubuntu8.4`\n"
        "• `libc-dev-bin`: `2.39-0ubuntu8.3` → `2.39-0ubuntu8.4`\n"
        "• `libc-devtools`: `2.39-0ubuntu8.3` → `2.39-0ubuntu8.4`\n"
        "• `libc6`: `2.39-0ubuntu8.3` → `2.39-0ubuntu8.4`\n"
        "• `libc6-dev`: `2.39-0ubuntu8.3` → `2.39-0ubuntu8.4`\n"
        "• `linux-firmware-amd-graphics`: `20240318.git3b128b60-0ubuntu2.6` "
        "→ `20240318.git3b128b60-0ubuntu2.7`\n"
        "• `libgpg-error0`: `1.47-3build1` → `1.47-3build2`"
    ) in formatted


def test_extract_versions_from_dpkg_log_fallback(tmp_path: Path) -> None:
    dpkg_log = tmp_path / "dpkg.log"
    dpkg_log.write_text(
        "2026-11-01 06:00:00 upgrade nginx:amd64 1.24.0-1 1.24.0-2\n"
        "2026-11-12 22:00:05 upgrade nginx:amd64 1.24.0-2 1.24.0-3\n"
        "2026-11-12 22:00:06 install linux-image-6.8.0-48-generic:amd64 <none> 6.8.0-48.48\n"
    )
    body = (
        "Unattended upgrade result: All upgrades installed\n\n"
        "Packages that were upgraded:\n"
        " nginx linux-image-6.8.0-48-generic unknown-pkg\n"
    )
    upgrades = extract_upgraded_packages(
        body,
        check_system=True,
        dpkg_log_paths=(dpkg_log,),
    )
    assert upgrades == [
        PackageUpgrade("nginx", "1.24.0-2", "1.24.0-3"),
        PackageUpgrade("linux-image-6.8.0-48-generic", None, "6.8.0-48.48"),
        PackageUpgrade("unknown-pkg", None, None),
    ]

    formatted = format_unattended_upgrades_body(
        body,
        max_chars=1500,
        check_system=True,
        dpkg_log_paths=(dpkg_log,),
    )
    assert "• `nginx`: `1.24.0-2` → `1.24.0-3`" in formatted
    assert "• `linux-image-6.8.0-48-generic`: → `6.8.0-48.48`" in formatted
    assert "• `unknown-pkg`" in formatted


def test_extract_package_versions_apt_and_new_unpack() -> None:
    text = (
        "Unpacking new-dep (1.0.0-1) ...\n"
        "Inst tzdata [2024a-0ubuntu0.24.04] (2024a-0ubuntu0.24.04.1 Ubuntu:24.04/noble-updates)\n"
    )
    versions = extract_package_versions(text)
    assert versions["new-dep"] == (None, "1.0.0-1")
    assert versions["tzdata"] == ("2024a-0ubuntu0.24.04", "2024a-0ubuntu0.24.04.1")


def test_parse_email_to_header_and_fallback() -> None:
    msg_with_to = parse_email(
        b"From: root@example.com\nTo: admin@example.com\nSubject: Hi\n\nBody\n",
        fallback_to="ignored@example.com",
    )
    assert msg_with_to.to == "admin@example.com"

    msg_without_to = parse_email(
        b"From: root@example.com\nSubject: Hi\n\nBody\n",
        fallback_to="root",
    )
    assert msg_without_to.to == "root"


def test_parse_unattended_upgrades_qp_and_reboot_required() -> None:
    raw = (
        b'Content-Type: text/plain; charset="utf-8"\n'
        b"MIME-Version: 1.0\n"
        b"Content-Transfer-Encoding: quoted-printable\n"
        b"Subject: [reboot required] unattended-upgrades result for web01.example.com: SUCCESS\n"
        b"From: unattended-upgrades\n"
        b"To: root\n"
        b"Auto-Submitted: auto-generated\n"
        b"\n"
        b"Unattended upgrade result: All upgrades installed\n"
        b"\n"
        b"Warning: A reboot is required to complete this upgrade, or a previous one.=\n"
        b"\n"
        b"\n"
        b"Packages that were upgraded:\n"
        b" libssl3\n"
        b"\n"
        b"Package installation log:\n"
        b"Unpacking libssl3:amd64 (3.0.15-1) over (3.0.14-1) ...\n"
    )
    parsed = parse_email(raw, check_system_reboot=False)
    assert parsed.status == "success"
    assert parsed.hostname == "web01.example.com"
    assert parsed.to == "root"
    assert parsed.reboot_required is True
    assert "`libssl3`: `3.0.14-1` → `3.0.15-1`" in parsed.body
