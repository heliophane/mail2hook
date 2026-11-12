"""Filtering logic for sender addresses and subject keywords."""

from __future__ import annotations

from email.utils import parseaddr

from mail2hook.config import Config
from mail2hook.parse import ParsedMessage


def _normalize_filter_item(item: str) -> str:
    """Normalize a filter pattern to lowercase and stripped."""
    return item.strip().lower()


def matches_sender(sender_header: str, filter_senders: list[str]) -> bool:
    """Check if the sender header matches any pattern in filter_senders.

    Supported patterns:
    - Exact email match: 'user@example.com'
    - Domain match with leading '@': '@example.com'
    - Bare domain match: 'example.com' (matches user@example.com and user@sub.example.com)
    - Username match: 'root'
    """
    if not filter_senders:
        return True

    # Parse address part from 'Display Name <email@domain>'
    _, email_addr = parseaddr(sender_header)
    email_clean = (email_addr or sender_header).strip().lower()

    # Extract user and domain components
    user_part, _, domain_part = email_clean.partition("@")

    for raw_pattern in filter_senders:
        pattern = _normalize_filter_item(raw_pattern)
        if not pattern:
            continue

        # Exact match
        if pattern == email_clean:
            return True

        # Domain pattern starting with '@'
        if pattern.startswith("@"):
            target_domain = pattern[1:]
            if domain_part == target_domain or domain_part.endswith("." + target_domain):
                return True

        # Bare domain match (contains a dot)
        elif "." in pattern:
            if domain_part == pattern or domain_part.endswith("." + pattern):
                return True

        # Username / local-part match
        elif user_part == pattern:
            return True

    return False


def matches_subject(subject_header: str, filter_keywords: list[str]) -> bool:
    """Check if the subject header contains any of the specified keywords (case-insensitive)."""
    if not filter_keywords:
        return True

    subject_lower = subject_header.lower()
    for raw_keyword in filter_keywords:
        keyword = _normalize_filter_item(raw_keyword)
        if keyword and keyword in subject_lower:
            return True

    return False


def should_process_message(msg: ParsedMessage, config: Config) -> bool:
    """Evaluate whether an incoming email should be forwarded to Discord."""
    if config.filter_senders:
        if not matches_sender(msg.sender, config.filter_senders):
            return False

    if config.filter_subject_keywords:
        if not matches_subject(msg.subject, config.filter_subject_keywords):
            return False

    return True
