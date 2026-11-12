"""Tests for sender and subject filters."""

from __future__ import annotations

from mail2hook.config import Config
from mail2hook.filters import matches_sender, matches_subject, should_process_message
from mail2hook.parse import ParsedMessage


def _make_msg(sender: str = "root@example.com", subject: str = "Test Subject") -> ParsedMessage:
    return ParsedMessage(
        sender=sender,
        subject=subject,
        date="Wed, 11 Nov 2026 12:00:00 +0000",
        body="Test message body",
    )


def test_matches_sender_empty_filter() -> None:
    assert matches_sender("root@example.com", []) is True


def test_matches_sender_exact() -> None:
    sender = "Admin User <admin@example.com>"
    assert matches_sender(sender, ["admin@example.com"]) is True
    assert matches_sender(sender, ["other@example.com"]) is False


def test_matches_sender_domain() -> None:
    assert matches_sender("root@server01.example.com", ["example.com"]) is True
    assert matches_sender("root@server01.example.com", ["@example.com"]) is True
    assert matches_sender("root@other.org", ["example.com"]) is False


def test_matches_sender_username() -> None:
    assert matches_sender("root@server01.example.com", ["root"]) is True
    assert matches_sender("daemon@server01.example.com", ["root"]) is False
    # Display name match (e.g. Unattended-Upgrade::Sender "unattended-upgrades")
    assert (
        matches_sender("unattended-upgrades <root@server01.example.com>", ["unattended-upgrades"])
        is True
    )


def test_matches_subject_empty_filter() -> None:
    assert matches_subject("Weekly cron job", []) is True


def test_matches_subject_keywords() -> None:
    subject = "unattended-upgrades result for server01: SUCCESS"
    assert matches_subject(subject, ["unattended-upgrades"]) is True
    assert matches_subject(subject, ["SUCCESS"]) is True
    assert matches_subject(subject, ["FAILURE", "backup"]) is False


def test_should_process_message_no_filters() -> None:
    config = Config(discord_webhook_url="https://discord.com/api/webhooks/123/token")
    msg = _make_msg()
    assert should_process_message(msg, config) is True


def test_should_process_message_with_filters() -> None:
    config = Config(
        discord_webhook_url="https://discord.com/api/webhooks/123/token",
        filter_senders=["root@example.com"],
        filter_subject_keywords=["unattended-upgrades"],
    )

    # Both match
    msg1 = _make_msg(
        sender="root@example.com",
        subject="unattended-upgrades result: SUCCESS",
    )
    assert should_process_message(msg1, config) is True

    # Sender mismatch
    msg2 = _make_msg(
        sender="hacker@other.com",
        subject="unattended-upgrades result: SUCCESS",
    )
    assert should_process_message(msg2, config) is False

    # Subject mismatch
    msg3 = _make_msg(
        sender="root@example.com",
        subject="Casual conversation",
    )
    assert should_process_message(msg3, config) is False
