"""Email parsing and body extraction for mail2hook."""

from __future__ import annotations

import email
import html
import re
from dataclasses import dataclass, field
from email import policy
from email.message import EmailMessage
from html.parser import HTMLParser


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


def detect_status(subject: str, body: str) -> str:
    """Determine message status (success, failure, or info) from subject and body."""
    sub_lower = subject.lower()
    body_lower = body.lower()

    # Explicit failure indicators
    if any(
        term in sub_lower
        for term in ("fail", "failure", "error", "critical", "broken", "fatal", "alert")
    ):
        return "failure"

    if (
        "unattended upgrade result: some errors" in body_lower
        or "packages that failed" in body_lower
        or "dpkg: error" in body_lower
    ):
        return "failure"

    # Explicit success indicators
    if "success" in sub_lower or "all upgrades installed" in body_lower:
        return "success"

    return "info"


_HOSTNAME_REGEX = re.compile(
    r"\b(?:for|on|at|host:?)\s+([a-zA-Z0-9][-a-zA-Z0-9]*(?:\.[a-zA-Z0-9][-a-zA-Z0-9]*)*)",
    re.IGNORECASE,
)

_IGNORED_HOST_WORDS = frozenset(
    {"all", "the", "a", "an", "root", "job", "cron", "upgrades", "upgrade", "result", "mail"}
)


def extract_hostname(subject: str, sender: str, fallback_to_local: bool = False) -> str | None:
    """Try to infer the host name from the subject or sender, optionally falling back to local."""
    # Check subject like 'unattended-upgrades result for ubuntu24: SUCCESS'
    match = _HOSTNAME_REGEX.search(subject)
    if match:
        candidate = match.group(1).rstrip(":.,")
        if candidate.lower() not in _IGNORED_HOST_WORDS:
            return candidate

    # Check sender domain e.g. root@ubuntu24 or root@server01.example.com
    if "@" in sender:
        domain = sender.split("@")[-1].strip(">").strip()
        if domain and domain.lower() not in ("localhost", "localdomain"):
            return domain

    if fallback_to_local:
        try:
            import socket

            local_host = socket.gethostname()
            if local_host and local_host.lower() not in ("localhost", "localhost.localdomain"):
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


def check_reboot_required(
    subject: str, body: str, check_system: bool = True
) -> tuple[bool, list[str]]:
    """Determine whether a reboot is required based on email contents or local system state."""
    text = f"{subject}\n{body}"
    email_indicates_reboot = bool(_REBOOT_REQUIRED_REGEX.search(text))

    system_pkgs: list[str] = []
    system_indicates_reboot = False
    if check_system:
        try:
            from pathlib import Path

            for path in (Path("/run/reboot-required"), Path("/var/run/reboot-required")):
                if path.is_file():
                    system_indicates_reboot = True
                    break

            for pkg_file in (
                Path("/run/reboot-required.pkgs"),
                Path("/var/run/reboot-required.pkgs"),
            ):
                if pkg_file.is_file():
                    system_pkgs = [
                        p.strip()
                        for p in pkg_file.read_text(encoding="utf-8", errors="replace").splitlines()
                        if p.strip()
                    ]
                    break
        except Exception:
            pass

    return (email_indicates_reboot or system_indicates_reboot), system_pkgs


def parse_email(raw_bytes: bytes, max_body_chars: int = 1500) -> ParsedMessage:
    """Parse raw email bytes into a structured ParsedMessage."""
    if not raw_bytes or not raw_bytes.strip():
        raise ParseError("Cannot parse empty email input")

    try:
        msg = email.message_from_bytes(raw_bytes, policy=policy.default)
    except Exception as err:
        raise ParseError(f"Malformed email message: {err}") from err

    sender = str(msg.get("From", "(unknown sender)"))
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
        subject=subject, body=raw_body, check_system=True
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
