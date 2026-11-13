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


def _sanitize_code_span(value: str) -> str:
    """Remove backticks and newlines so values are safe inside Discord inline code spans."""
    return re.sub(r"[`\r\n]+", "", value).strip()


@dataclass(frozen=True)
class PackageUpgrade:
    """Version transition details for an upgraded or newly installed package."""

    name: str
    old_version: str | None = None
    new_version: str | None = None

    def format_line(self) -> str:
        """Format package entry as a single bulleted line for Discord."""
        safe_name = _sanitize_code_span(self.name)
        safe_old = _sanitize_code_span(self.old_version) if self.old_version else None
        safe_new = _sanitize_code_span(self.new_version) if self.new_version else None

        if safe_old and safe_new:
            return f"• `{safe_name}`: `{safe_old}` → `{safe_new}`"
        if safe_new:
            return f"• `{safe_name}`: → `{safe_new}`"
        return f"• `{safe_name}`"


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
    upgraded_packages: list[PackageUpgrade] = field(default_factory=list)


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


# dpkg output: "Unpacking libssl3:amd64 (3.0.15-1~deb12u1) over (3.0.14-1~deb12u1) ..."
_DPKG_UNPACK_OVER_RE = re.compile(
    r"^\s*Unpacking\s+([^\s:(]+)(?::([^\s(]+))?\s+\(([^)]+)\)\s+over\s+\(([^)]+)\)",
    re.MULTILINE,
)
# dpkg output for newly installed package: "Unpacking linux-image-generic (6.8.0-48.48) ..."
_DPKG_UNPACK_NEW_RE = re.compile(
    r"^\s*Unpacking\s+([^\s:(]+)(?::([^\s(]+))?\s+\(([^)]+)\)\s+\.\.\.",
    re.MULTILINE,
)
# /var/log/dpkg.log format: "2026-11-12 22:00:05 upgrade apache2:amd64 2.4.58-1 2.4.58-2"
_DPKG_LOG_UPGRADE_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\s+upgrade\s+([^\s:]+)(?::([^\s]+))?\s+(\S+)\s+(\S+)",
    re.MULTILINE,
)
# /var/log/dpkg.log install format: "2026-11-12 22:00:05 install pkg:amd64 <none> 1.2.3"
_DPKG_LOG_INSTALL_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\s+install\s+([^\s:]+)(?::([^\s]+))?\s+<none>\s+(\S+)",
    re.MULTILINE,
)
# apt output: "Inst apache2 [2.4.58-1ubuntu8.4] (2.4.58-1ubuntu8.5 ...)"
_APT_INST_UPGRADE_RE = re.compile(
    r"^\s*Inst\s+([^\s:\[]+)(?::([^\s\[]+))?\s+\[([^\]]+)\]\s+\(([^\s)]+)",
    re.MULTILINE,
)

_PACKAGE_SECTION_RE = re.compile(r"^(Packages\b[^:]*):\s*(.*)$", re.IGNORECASE)

_DPKG_PROGRESS_PREFIXES = (
    "preparing to unpack",
    "unpacking ",
    "setting up ",
    "selecting previously unselected",
    "(reading database",
    "processing triggers for",
)

_LOG_ERROR_LINE_RE = re.compile(
    r"^\s*E:\s+"
    r"|\bdpkg:\s*error\b"
    r"|\berrors\s+were\s+encountered\b"
    r"|\berror\s+processing\b"
    r"|\berror\s+exit\s+status\b"
    r"|\berror\s+code\b"
    r"|\bfailed\b"
    r"|\bERROR\b",
    re.IGNORECASE,
)

DEFAULT_DPKG_LOG_PATHS: tuple[Path, ...] = (
    Path("/var/log/unattended-upgrades/unattended-upgrades-dpkg.log"),
    Path("/var/log/dpkg.log"),
)


def _record_version(
    versions: dict[str, tuple[str | None, str | None]],
    pkg_base: str,
    arch: str | None,
    old_ver: str | None,
    new_ver: str | None,
    *,
    preserve_initial_old: bool = True,
) -> None:
    """Store old and new versions keyed by both base package name and arch-qualified name."""
    keys = [pkg_base]
    if arch:
        keys.append(f"{pkg_base}:{arch}")
    for key in keys:
        if preserve_initial_old and key in versions:
            initial_old, _ = versions[key]
            versions[key] = (initial_old or old_ver, new_ver)
        else:
            versions[key] = (old_ver, new_ver)


def extract_package_versions(
    text: str,
    *,
    preserve_initial_old: bool = True,
) -> dict[str, tuple[str | None, str | None]]:
    """Extract (old_version, new_version) mappings from dpkg/apt logs in text."""
    versions: dict[str, tuple[str | None, str | None]] = {}

    for match in _DPKG_UNPACK_OVER_RE.finditer(text):
        pkg_base, arch, new_ver, old_ver = (
            match.group(1),
            match.group(2),
            match.group(3).strip(),
            match.group(4).strip(),
        )
        _record_version(
            versions,
            pkg_base,
            arch,
            old_ver,
            new_ver,
            preserve_initial_old=preserve_initial_old,
        )

    for match in _DPKG_UNPACK_NEW_RE.finditer(text):
        pkg_base, arch, new_ver = match.group(1), match.group(2), match.group(3).strip()
        if pkg_base not in versions:
            _record_version(
                versions,
                pkg_base,
                arch,
                None,
                new_ver,
                preserve_initial_old=preserve_initial_old,
            )

    for match in _DPKG_LOG_UPGRADE_RE.finditer(text):
        pkg_base, arch, old_ver, new_ver = (
            match.group(1),
            match.group(2),
            match.group(3).strip(),
            match.group(4).strip(),
        )
        _record_version(
            versions,
            pkg_base,
            arch,
            old_ver,
            new_ver,
            preserve_initial_old=preserve_initial_old,
        )

    for match in _DPKG_LOG_INSTALL_RE.finditer(text):
        pkg_base, arch, new_ver = match.group(1), match.group(2), match.group(3).strip()
        if pkg_base not in versions:
            _record_version(
                versions,
                pkg_base,
                arch,
                None,
                new_ver,
                preserve_initial_old=preserve_initial_old,
            )

    for match in _APT_INST_UPGRADE_RE.finditer(text):
        pkg_base, arch, old_ver, new_ver = (
            match.group(1),
            match.group(2),
            match.group(3).strip(),
            match.group(4).strip(),
        )
        if pkg_base not in versions:
            _record_version(
                versions,
                pkg_base,
                arch,
                old_ver,
                new_ver,
                preserve_initial_old=preserve_initial_old,
            )

    return versions


def _read_system_dpkg_versions(
    dpkg_log_paths: Sequence[Path] = DEFAULT_DPKG_LOG_PATHS,
) -> dict[str, tuple[str | None, str | None]]:
    """Read recent package version transitions from local dpkg logs as a fallback."""
    combined_versions: dict[str, tuple[str | None, str | None]] = {}
    for path in dpkg_log_paths:
        try:
            if not path.is_file():
                continue
            with path.open("rb") as f:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - 131072))
                tail_text = f.read().decode("utf-8", errors="replace")
            file_versions = extract_package_versions(tail_text, preserve_initial_old=False)
            for k, v in file_versions.items():
                if k not in combined_versions:
                    combined_versions[k] = v
        except Exception:
            pass
    return combined_versions


def _lookup_package_version(
    pkg_name: str,
    versions: dict[str, tuple[str | None, str | None]],
) -> tuple[str | None, str | None]:
    """Find (old_version, new_version) for pkg_name, matching with or without :arch."""
    if pkg_name in versions:
        return versions[pkg_name]
    base_name = pkg_name.split(":", 1)[0]
    if base_name in versions:
        return versions[base_name]
    return (None, None)


def _split_package_tokens(lines: list[str]) -> list[str]:
    """Extract clean package names from one or more space/comma-separated package list lines."""
    pkgs: list[str] = []
    for line in lines:
        for raw_token in line.replace(",", " ").split():
            token = raw_token.strip().lstrip("•*-").strip()
            if token:
                pkgs.append(token)
    return pkgs


def _split_unattended_header_and_logs(body: str) -> tuple[str, str]:
    """Split an unattended-upgrades body into summary header and trailing log sections."""
    markers = ("Package installation log:", "Unattended-upgrades log:")
    earliest_idx: int | None = None
    for marker in markers:
        idx = body.find(marker)
        if idx != -1 and (earliest_idx is None or idx < earliest_idx):
            earliest_idx = idx

    if earliest_idx is not None:
        return body[:earliest_idx].strip(), body[earliest_idx:].strip()
    return body.strip(), ""


def _extract_error_lines(log_section: str) -> list[str]:
    """Extract genuine dpkg/apt error lines from log_section without false positives."""
    if not log_section:
        return []
    # Only inspect the Package installation log part if Unattended-upgrades log follows,
    # unless an explicit ERROR line appears in Unattended-upgrades log.
    error_lines: list[str] = []
    for line in log_section.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        lower = stripped.lower()
        if lower.startswith(_DPKG_PROGRESS_PREFIXES):
            continue
        if lower.startswith("allowed origins are:"):
            continue
        if _LOG_ERROR_LINE_RE.search(stripped):
            error_lines.append(line)
    return error_lines


def extract_upgraded_packages(
    body: str,
    *,
    check_system: bool = False,
    dpkg_log_paths: Sequence[Path] = DEFAULT_DPKG_LOG_PATHS,
) -> list[PackageUpgrade]:
    """Extract the list of upgraded packages and their version transitions from an email body."""
    versions = extract_package_versions(body)
    header, _ = _split_unattended_header_and_logs(body)

    upgraded_names: list[str] = []
    lines = header.splitlines()
    i = 0
    while i < len(lines):
        match = _PACKAGE_SECTION_RE.match(lines[i].strip())
        if match:
            section_title = match.group(1).strip()
            inline_rest = match.group(2).strip()
            pkg_lines: list[str] = [inline_rest] if inline_rest else []
            i += 1
            while i < len(lines):
                next_stripped = lines[i].strip()
                if not next_stripped or next_stripped.endswith(":"):
                    break
                pkg_lines.append(next_stripped)
                i += 1
            if "upgraded" in section_title.lower():
                upgraded_names.extend(_split_package_tokens(pkg_lines))
            continue
        i += 1

    # If no explicit "Packages that were upgraded:" list was present, fall back to unpacked packages
    if not upgraded_names:
        seen_bases: set[str] = set()
        for match in _DPKG_UNPACK_OVER_RE.finditer(body):
            pkg_base = match.group(1)
            if pkg_base not in seen_bases:
                seen_bases.add(pkg_base)
                upgraded_names.append(pkg_base)

    # Check local system dpkg logs if any package is still missing version info
    if check_system and any(
        _lookup_package_version(pkg, versions) == (None, None) for pkg in upgraded_names
    ):
        sys_versions = _read_system_dpkg_versions(dpkg_log_paths)
        for k, v in sys_versions.items():
            if k not in versions:
                versions[k] = v

    return [
        PackageUpgrade(
            name=pkg,
            old_version=_lookup_package_version(pkg, versions)[0],
            new_version=_lookup_package_version(pkg, versions)[1],
        )
        for pkg in upgraded_names
    ]


def format_unattended_upgrades_body(
    body: str,
    max_chars: int,
    *,
    check_system: bool = False,
    dpkg_log_paths: Sequence[Path] = DEFAULT_DPKG_LOG_PATHS,
) -> str:
    """Format unattended-upgrades body with vertical package lists and version transitions."""
    versions = extract_package_versions(body)
    header, log_section = _split_unattended_header_and_logs(body)

    # First pass: collect all package tokens in header to see if system log fallback is needed
    lines = header.splitlines()
    all_header_pkgs: list[str] = []
    idx = 0
    while idx < len(lines):
        m = _PACKAGE_SECTION_RE.match(lines[idx].strip())
        if m:
            inline_rest = m.group(2).strip()
            sec_lines = [inline_rest] if inline_rest else []
            idx += 1
            while idx < len(lines):
                nxt = lines[idx].strip()
                if not nxt or nxt.endswith(":"):
                    break
                sec_lines.append(nxt)
                idx += 1
            all_header_pkgs.extend(_split_package_tokens(sec_lines))
            continue
        idx += 1

    if check_system and any(
        _lookup_package_version(pkg, versions) == (None, None) for pkg in all_header_pkgs
    ):
        sys_versions = _read_system_dpkg_versions(dpkg_log_paths)
        for k, v in sys_versions.items():
            if k not in versions:
                versions[k] = v

    # Second pass: rewrite package sections in header as vertical lists with old -> new versions
    formatted_lines: list[str] = []
    has_package_section = False
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        match = _PACKAGE_SECTION_RE.match(stripped)
        if match:
            has_package_section = True
            section_title = match.group(1).strip()
            inline_rest = match.group(2).strip()
            pkg_lines: list[str] = [inline_rest] if inline_rest else []
            i += 1
            while i < len(lines):
                next_stripped = lines[i].strip()
                if not next_stripped or next_stripped.endswith(":"):
                    break
                pkg_lines.append(next_stripped)
                i += 1

            pkgs = _split_package_tokens(pkg_lines)
            formatted_lines.append(f"{section_title}:")
            for pkg in pkgs:
                old_ver, new_ver = _lookup_package_version(pkg, versions)
                formatted_lines.append(
                    PackageUpgrade(name=pkg, old_version=old_ver, new_version=new_ver).format_line()
                )
            continue

        formatted_lines.append(lines[i])
        i += 1

    # If header had no "Packages ...:" section at all, but dpkg unpacked upgrades in the log
    if not has_package_section and versions:
        unpacked = extract_upgraded_packages(
            body, check_system=False, dpkg_log_paths=dpkg_log_paths
        )
        if unpacked:
            if formatted_lines and formatted_lines[-1].strip():
                formatted_lines.append("")
            formatted_lines.append("Packages that were upgraded:")
            for item in unpacked:
                formatted_lines.append(item.format_line())

    formatted_header = "\n".join(formatted_lines).strip()

    if log_section:
        error_lines = _extract_error_lines(log_section)
        if error_lines:
            error_summary = "Errors encountered:\n" + "\n".join(error_lines[:5])
            if formatted_header:
                combined = f"{formatted_header}\n\n{error_summary}"
            else:
                combined = error_summary
            return truncate_text(combined, max_chars)

        if not formatted_header:
            return truncate_text(f"Package installation log:\n{log_section}", max_chars)

    return truncate_text(formatted_header, max_chars)


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


def _is_upgrade_email(subject: str, body: str) -> bool:
    """Return True if the email is an unattended-upgrades or package upgrade report."""
    sub_lower = subject.lower()
    body_lower = body.lower()
    return (
        "unattended-upgrade" in sub_lower
        or "unattended upgrade" in sub_lower
        or "unattended-upgrade" in body_lower
        or "unattended upgrade" in body_lower
        or "packages that were upgraded:" in body_lower
    )


def parse_email(
    raw_bytes: bytes,
    max_body_chars: int = 1500,
    *,
    fallback_sender: str | None = None,
    check_system_reboot: bool = True,
    dpkg_log_paths: Sequence[Path] = DEFAULT_DPKG_LOG_PATHS,
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
    upgraded_packages: list[PackageUpgrade] = []
    if _is_upgrade_email(subject, raw_body):
        upgraded_packages = extract_upgraded_packages(
            raw_body,
            check_system=check_system_reboot,
            dpkg_log_paths=dpkg_log_paths,
        )
        body = format_unattended_upgrades_body(
            raw_body,
            max_chars=max_body_chars,
            check_system=check_system_reboot,
            dpkg_log_paths=dpkg_log_paths,
        )
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
        upgraded_packages=upgraded_packages,
    )
