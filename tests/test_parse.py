"""Tests for email parsing and body extraction."""

from __future__ import annotations

from pathlib import Path

import pytest

from mail2hook.parse import (
    ParseError,
    detect_status,
    extract_hostname,
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
    parsed = parse_email(eml_bytes)

    assert "unattended-upgrades result for server01.example.com: SUCCESS" in parsed.subject
    assert "root@server01.example.com" in parsed.sender
    assert parsed.status == "success"
    assert parsed.hostname == "server01.example.com"
    assert "All upgrades installed" in parsed.body
    assert "libssl3 openssl curl libcurl4" in parsed.body


def test_parse_unattended_upgrades_failure() -> None:
    eml_bytes = (FIXTURES_DIR / "unattended_upgrades_failure.eml").read_bytes()
    parsed = parse_email(eml_bytes)

    assert "FAILURE" in parsed.subject
    assert parsed.status == "failure"
    assert parsed.hostname == "server01.example.com"
    assert "Some errors were encountered" in parsed.body
    assert "broken-pkg-example" in parsed.body
    assert "dpkg: error" in parsed.body.lower() or "errors encountered" in parsed.body.lower()


def test_parse_html_only() -> None:
    eml_bytes = (FIXTURES_DIR / "html_only.eml").read_bytes()
    parsed = parse_email(eml_bytes)

    assert parsed.subject == "[ALERT] High memory usage detected"
    assert "alerts@monitor.example.com" in parsed.sender
    assert parsed.status == "failure"  # Contains 'ALERT'
    assert "System Resource Alert" in parsed.body
    assert "Server server01.example.com has exceeded 90% memory threshold." in parsed.body
    assert "Current usage: 94.2%" in parsed.body
    assert "<html>" not in parsed.body


def test_parse_non_utf8_charset() -> None:
    eml_bytes = (FIXTURES_DIR / "non_utf8.eml").read_bytes()
    parsed = parse_email(eml_bytes)

    # Subject is encoded in ISO-8859-1 Q-encoding: =?ISO-8859-1?Q?Rapport_d=27activit=E9?=
    assert "Rapport d'activité" in parsed.subject
    # Sender is encoded in ISO-8859-1 Q-encoding: =?ISO-8859-1?Q?Mise_=E0_jour?=
    assert "Mise à jour" in parsed.sender
    # Body contains accented French characters
    assert "Opération terminée avec succès" in parsed.body
    assert "contrôle" in parsed.body


def test_parse_missing_headers() -> None:
    raw = b"Just a plain body with no headers.\nSecond line.\n"
    parsed = parse_email(raw)
    assert parsed.sender == "(unknown sender)"
    assert parsed.subject == "(no subject)"
    assert parsed.date == ""
    assert "Just a plain body" in parsed.body


def test_detect_status() -> None:
    assert detect_status("Weekly cron report", "All jobs completed") == "info"
    assert detect_status("Backup failed: timeout", "Connection lost") == "failure"
    assert detect_status("Database error alert", "Crash detected") == "failure"
    assert detect_status("Daily status: SUCCESS", "All good") == "success"


def test_extract_hostname() -> None:
    assert (
        extract_hostname("Upgrade result for host01.example.com: SUCCESS", "root@example.com")
        == "host01.example.com"
    )
    assert (
        extract_hostname("Cron job output", "root@server02.infra.local") == "server02.infra.local"
    )
    assert extract_hostname("Generic Subject", "simpleuser") is None
