"""Email parsing and body extraction for mail2hook."""

from __future__ import annotations

import email
import html
import re
import socket
from collections.abc import Sequence
from dataclasses import dataclass, field
from email import policy
from email.message import EmailMessage
from html.parser import HTMLParser
from pathlib import Path


class ParseError(Exception):
    """Raised when an email cannot be parsed."""


@dataclass(frozen=True)
class ParsedMessage:
    """Extracted and normalized email details."""

    sender: str
    subject: str
    date: str
    body: str
    status: str = "info"  # "success", "failure", or "info"
    hostname: str | None = None
    reboot_required: bool = False
    reboot_packages: list[str] = field(default_factory=list)


class _HTMLTextExtractor(HTMLParser):
    """Simple, dependency-free HTML to plain-text converter."""

    def __init__(self) -> None:
        super().__init__()
        self._pieces: list[str] = []
        self._ignore = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in ("script", "style", "head"):
            self._ignore = True
        elif tag.lower() in ("p", "br", "div", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "li"):
            self._pieces.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in ("script", "style", "head"):
            self._ignore = False
        elif tag.lower() in ("p", "div", "tr"):
            self._pieces.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._ignore:
            self._pieces.append(data)

    def get_text(self) -> str:
        raw_text = "".join(self._pieces)
        lines = [line.strip() for line in raw_text.splitlines()]
        cleaned: list[str] = []
        prev_blank = False
        for line in lines:
            if not line:
                if not prev_blank:
                    cleaned.append("")
                    prev_blank = True
            else:
                cleaned.append(line)
                prev_blank = False
        return html.unescape("\n".join(cleaned)).strip()


def strip_html(html_content: str) -> str:
    """Convert HTML content into readable plain text."""
    parser = _HTMLTextExtractor()
    parser.feed(html_content)
    return parser.get_text()


def _decode_part(part: EmailMessage) -> str:
    """Safely decode an EmailMessage part to a unicode string."""
    try:
        content = part.get_content()
        if isinstance(content, str):
            return content
    except Exception:
        pass

    payload = part.get_payload(decode=True)
    if isinstance(payload, bytes):
        charset = part.get_content_charset() or "utf-8"
        try:
            return payload.decode(charset, errors="replace")
        except LookupError:
            return payload.decode("utf-8", errors="replace")
    elif isinstance(payload, str):
        return payload

    return ""


def extract_body(msg: EmailMessage) -> str:
    """Extract plain text body, falling back to stripped HTML."""
    plain_parts: list[str] = []
    html_parts: list[str] = []

    if msg.is_multipart():
        for part in msg.walk():
            if part.is_multipart():
                continue
            if part.get_content_disposition() == "attachment":
                continue

            ctype = part.get_content_type()
            if ctype == "text/plain":
                text = _decode_part(part)
                if text.strip():
                    plain_parts.append(text.strip())
            elif ctype == "text/html":
                text = _decode_part(part)
                if text.strip():
                    html_parts.append(text.strip())
    else:
        ctype = msg.get_content_type()
        if ctype == "text/plain":
            text = _decode_part(msg)
            if text.strip():
                plain_parts.append(text.strip())
        elif ctype == "text/html":
            text = _decode_part(msg)
            if text.strip():
                html_parts.append(text.strip())

    if plain_parts:
        return "\n\n".join(plain_parts).strip()
    if html_parts:
        return strip_html("\n\n".join(html_parts))
    return ""


def truncate_text(text: str, max_chars: int) -> str:
    """Truncate text to max_chars, appending an ellipsis if truncated."""
    if len(text) <= max_chars:
        return text
    if max_chars <= 1:
        return "…"
    return text[: max_chars - 1].rstrip() + "…"


def format_unattended_upgrades_body(body: str, max_chars: int) -> str:
    """Format unattended-upgrades body: prioritize package summary and errors."""
    if "Package installation log:" in body:
        header, _, log_section = body.partition("Package installation log:")
        header = header.strip()
        log_section = log_section.strip()

        # If header alone is within limit and fits some log, include key lines from log
        if len(header) + 30 < max_chars:
            available_for_log = max_chars - len(header) - 35
            # Check for error lines in log
            error_lines = [
                line
                for line in log_section.splitlines()
                if any(
                    err_term in line.lower()
                    for err_term in ("error", "failed", "dpkg: error", "e: ")
                )
            ]
            if error_lines:
                error_summary = "\nErrors encountered:\n" + "\n".join(error_lines[:5])
                combined = f"{header}\n\n{error_summary}"
                return truncate_text(combined, max_chars)

            log_excerpt = truncate_text(log_section, available_for_log)
            return f"{header}\n\nPackage installation log:\n{log_excerpt}"

        return truncate_text(header, max_chars)

    return truncate_text(body, max_chars)


_SUBJECT_FAILURE_RE = re.compile(
    r"(?<!\bno\s)(?<!\b0\s)\b(?:fail(?:ed|ure|ures)?|errors?|critical|broken|fatal|alert)\b",
    re.IGNORECASE,
)
_SUBJECT_SUCCESS_RE = re.compile(r"\bsuccess(?:ful|fully)?\b", re.IGNORECASE)


def detect_status(subject: str, body: str) -> str:
    """Determine message status (success, failure, or info) from subject and body."""
    body_lower = body.lower()

    # Check explicit unattended-upgrades body outcomes first
    if (
        "unattended upgrade result: some errors" in body_lower
        or "packages that failed" in body_lower
        or "dpkg: error" in body_lower
    ):
        return "failure"

    if _SUBJECT_FAILURE_RE.search(subject):
        return "failure"

    if _SUBJECT_SUCCESS_RE.search(subject) or "all upgrades installed" in body_lower:
        return "success"

    return "info"


# Match hostnames after specific system prefixes, or user@host patterns in subjects
_HOSTNAME_USER_AT_RE = re.compile(
    r"\b(?:for\s+)?[a-zA-Z0-9_.+-]+@([a-zA-Z0-9][-a-zA-Z0-9]*(?:\.[a-zA-Z0-9][-a-zA-Z0-9]*)*)",
    re.IGNORECASE,
)
_HOSTNAME_RESULT_RE = re.compile(
    r"\b(?:result\s+for|upgrades?\s+for|run\s+on|host:?)\s+"
    r"([a-zA-Z0-9][-a-zA-Z0-9]*(?:\.[a-zA-Z0-9][-a-zA-Z0-9]*)*)",
    re.IGNORECASE,
)
_HOSTNAME_FOR_FQDN_RE = re.compile(
    r"\b(?:for|on|at)\s+([a-zA-Z0-9][-a-zA-Z0-9]*(?:\.[a-zA-Z0-9][-a-zA-Z0-9]*)+)",
    re.IGNORECASE,
)

_IGNORED_HOST_WORDS = frozenset(
    {
        "all",
        "the",
        "a",
        "an",
        "root",
        "job",
        "cron",
        "upgrades",
        "upgrade",
        "result",
        "mail",
        "today",
        "yesterday",
        "tomorrow",
        "monday",
        "tuesday",
        "wednesday",
        "thursday",
        "friday",
        "saturday",
        "sunday",
        "night",
        "morning",
        "daily",
        "weekly",
        "monthly",
        "line",
        "port",
        "server",
        "system",
        "localhost",
        "localdomain",
    }
)


def _clean_hostname(value: str) -> str:
    """Strip characters that cannot appear in a valid hostname or would break Discord markdown."""
    cleaned = re.sub(r"[^a-zA-Z0-9._-]", "", value.strip().rstrip(":.,"))
    return cleaned


def extract_hostname(subject: str, sender: str, fallback_to_local: bool = False) -> str | None:
    """Try to infer the host name from the subject or sender, optionally falling back to local."""
    for regex in (_HOSTNAME_RESULT_RE, _HOSTNAME_USER_AT_RE, _HOSTNAME_FOR_FQDN_RE):
        match = regex.search(subject)
        if match:
            candidate = _clean_hostname(match.group(1))
            if candidate and candidate.lower() not in _IGNORED_HOST_WORDS:
                return candidate

    # Check sender domain e.g. root@ubuntu24 or root@server01.example.com
    if "@" in sender:
        domain = _clean_hostname(sender.split("@")[-1].strip(">").strip())
        if domain and domain.lower() not in _IGNORED_HOST_WORDS:
            return domain

    if fallback_to_local:
        try:
            local_host = _clean_hostname(socket.gethostname())
            if local_host and local_host.lower() not in (
                "localhost",
                "localhost.localdomain",
                "localdomain",
            ):
                return local_host
        except Exception:
            pass

    return None


_REBOOT_REQUIRED_REGEX = re.compile(
    r"(?<!\bno\s)(?<!\bnot\s)\breboot(?:\s+is)?\s+required\b"
    r"|(?<!\bno\s)(?<!\bnot\s)(?<!\bno\ssystem\s)(?<!\bnot\ssystem\s)\brestart(?:\s+is)?\s+required\b"
    r"|\*\*\*\s*system restart required\s*\*\*\*"
    r"|\breboot:\s*true\b",
    re.IGNORECASE,
)

DEFAULT_REBOOT_FLAG_PATHS: tuple[Path, ...] = (
    Path("/run/reboot-required"),
    Path("/var/run/reboot-required"),
)
DEFAULT_REBOOT_PKGS_PATHS: tuple[Path, ...] = (
    Path("/run/reboot-required.pkgs"),
    Path("/var/run/reboot-required.pkgs"),
)


def check_reboot_required(
    subject: str,
    body: str,
    check_system: bool = True,
    *,
    reboot_flag_paths: Sequence[Path] = DEFAULT_REBOOT_FLAG_PATHS,
    reboot_pkgs_paths: Sequence[Path] = DEFAULT_REBOOT_PKGS_PATHS,
) -> tuple[bool, list[str]]:
    """Determine whether a reboot is required based on email contents or local system state."""
    text = f"{subject}\n{body}"
    email_indicates_reboot = bool(_REBOOT_REQUIRED_REGEX.search(text))

    system_pkgs: list[str] = []
    system_indicates_reboot = False
    if check_system:
        try:
            for path in reboot_flag_paths:
                if path.is_file():
                    system_indicates_reboot = True
                    break

            for pkg_file in reboot_pkgs_paths:
                if pkg_file.is_file():
                    system_pkgs = [
                        re.sub(r"[`\r\n]", "", p.strip())
                        for p in pkg_file.read_text(encoding="utf-8", errors="replace").splitlines()
                        if p.strip()
                    ]
                    break
        except Exception:
            pass

    return (email_indicates_reboot or system_indicates_reboot), system_pkgs


def parse_email(
    raw_bytes: bytes,
    max_body_chars: int = 1500,
    *,
    fallback_sender: str | None = None,
    check_system_reboot: bool = True,
) -> ParsedMessage:
    """Parse raw email bytes into a structured ParsedMessage."""
    if not raw_bytes or not raw_bytes.strip():
        raise ParseError("Cannot parse empty email input")

    try:
        msg = email.message_from_bytes(raw_bytes, policy=policy.default)
    except Exception as err:
        raise ParseError(f"Malformed email message: {err}") from err

    raw_from = msg.get("From")
    if raw_from and str(raw_from).strip():
        sender = str(raw_from).strip()
    elif fallback_sender and fallback_sender.strip():
        sender = fallback_sender.strip()
    else:
        sender = "(unknown sender)"

    subject = str(msg.get("Subject", "(no subject)"))
    date = str(msg.get("Date", ""))

    raw_body = extract_body(msg)

    # Format body: special handling for unattended-upgrades or standard truncation
    if "unattended upgrade" in subject.lower() or "unattended upgrade" in raw_body.lower():
        body = format_unattended_upgrades_body(raw_body, max_chars=max_body_chars)
    else:
        body = truncate_text(raw_body, max_chars=max_body_chars)

    status = detect_status(subject=subject, body=raw_body)
    hostname = extract_hostname(subject=subject, sender=sender, fallback_to_local=True)
    reboot_required, reboot_packages = check_reboot_required(
        subject=subject, body=raw_body, check_system=check_system_reboot
    )

    return ParsedMessage(
        sender=sender,
        subject=subject,
        date=date,
        body=body,
        status=status,
        hostname=hostname,
        reboot_required=reboot_required,
        reboot_packages=reboot_packages,
    )
