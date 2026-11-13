"""Command-line interface and entry point for mail2hook."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from mail2hook import __version__
from mail2hook.config import ConfigError, load_config
from mail2hook.discord import (
    DiscordPermanentError,
    DiscordTemporaryError,
    build_payload,
    post_to_discord,
)
from mail2hook.filters import should_process_message
from mail2hook.parse import ParseError, parse_email

MAX_MESSAGE_BYTES = 10 * 1024 * 1024  # 10 MiB

# Postfix / sysexits.h standard exit codes
EXIT_OK = 0  # EX_OK: Success or message filtered out
EXIT_DATAERR = 65  # EX_DATAERR: The input data was incorrect (unparseable email)
EXIT_TEMPFAIL = 75  # EX_TEMPFAIL: Temporary failure (retry later)
EXIT_CONFIG = 78  # EX_CONFIG: Configuration error (missing/invalid webhook, etc.)


DEFAULT_APT_CONFIG = """// /etc/apt/apt.conf.d/52mail2hook
// Default unattended-upgrades configuration for mail2hook.
//
// To install on Debian/Ubuntu:
//   mail2hook --print-apt-config | sudo tee /etc/apt/apt.conf.d/52mail2hook

// 1. Enable automatic package update lists and unattended upgrades
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
APT::Periodic::AutocleanInterval "7";

// 2. Deliver upgrade reports to mail2hook
// When mail2hook is linked to /usr/sbin/sendmail,
// unattended-upgrades delivers directly to mail2hook.
Unattended-Upgrade::Mail "root";

// 3. Reporting frequency:
// - "on-change": Send a Discord notification when upgrades occur or fail (recommended)
// - "always": Send a Discord notification on every upgrade run, even with zero changes
// - "only-on-error": Send a Discord notification only when an upgrade fails
Unattended-Upgrade::MailReport "on-change";

// 4. Sender identity for clean email headers
Unattended-Upgrade::Sender "unattended-upgrades";

// 5. Reboot settings (optional)
// By default, unattended-upgrades will NOT reboot automatically.
// Mail2hook will notify you in Discord with "Reboot: Required" when a reboot is needed.
// To enable automatic reboots when a kernel or core library update requires it:
// Unattended-Upgrade::Automatic-Reboot "true";
// Unattended-Upgrade::Automatic-Reboot-Time "03:00";
"""


def build_parser() -> argparse.ArgumentParser:
    """Create command-line argument parser with sendmail CLI compatibility."""
    parser = argparse.ArgumentParser(
        prog="mail2hook",
        description=(
            "Read raw email from stdin and forward a summary to Discord (sendmail compatible)."
        ),
    )
    # Core mail2hook options
    parser.add_argument(
        "--file",
        type=Path,
        metavar="PATH",
        help="Read raw email from a file instead of stdin.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print Discord webhook JSON payload to stdout without sending.",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        metavar="PATH",
        help="Path to custom environment file (defaults to /etc/mail2hook/env or .env).",
    )
    parser.add_argument(
        "--print-apt-config",
        action="store_true",
        help="Print recommended /etc/apt/apt.conf.d/52mail2hook configuration and exit.",
    )
    parser.add_argument(
        "--version",
        "-V",
        action="version",
        version=f"%(prog)s {__version__}",
    )

    # Standard sendmail CLI compatibility options
    parser.add_argument(
        "-t",
        action="store_true",
        help="Read message for recipients (To:, Cc:, Bcc:). Standard sendmail flag.",
    )
    parser.add_argument(
        "-i",
        action="store_true",
        help="Ignore single dots on lines by themselves. Standard sendmail flag.",
    )
    parser.add_argument(
        "-oi",
        action="store_true",
        help="Ignore single dots on lines by themselves (alias for -i).",
    )
    parser.add_argument(
        "-bm",
        action="store_true",
        help="Read mail from standard input and deliver it (default mode).",
    )
    parser.add_argument(
        "-bp",
        action="store_true",
        help="Print mail queue (standard sendmail / mailq flag).",
    )
    parser.add_argument(
        "-bv",
        action="store_true",
        help="Verify recipients mode (standard sendmail flag).",
    )
    parser.add_argument(
        "-bs",
        action="store_true",
        help="Accepted for sendmail CLI flag compatibility.",
    )
    parser.add_argument(
        "-f",
        "-r",
        dest="sender",
        type=str,
        help="Set envelope sender address (standard sendmail flag).",
    )
    parser.add_argument(
        "-F",
        dest="full_name",
        type=str,
        help="Set sender full name (standard sendmail flag).",
    )
    parser.add_argument(
        "-B",
        dest="body_type",
        type=str,
        help="Set body type (e.g. 8BITMIME or 7BIT).",
    )
    parser.add_argument(
        "-v",
        dest="verbose",
        action="store_true",
        help="Verbose mode / sendmail verbose flag.",
    )
    parser.add_argument(
        "-o",
        dest="options",
        action="append",
        help="Set sendmail option (e.g. -oi, -odb, -oem).",
    )
    parser.add_argument(
        "recipients",
        nargs="*",
        help="Optional recipient email addresses (ignored in -t mode).",
    )

    return parser


def _build_fallback_sender(args: argparse.Namespace) -> str | None:
    """Build a fallback sender string from sendmail -f/-r and -F flags if provided."""
    sender_addr = (getattr(args, "sender", None) or "").strip()
    full_name = (getattr(args, "full_name", None) or "").strip()
    if sender_addr and full_name:
        return f"{full_name} <{sender_addr}>"
    if sender_addr:
        return sender_addr
    if full_name:
        return full_name
    return None


def _ignore_dots_enabled(args: argparse.Namespace) -> bool:
    """Return True if sendmail -i / -oi (ignore single dots) is active."""
    if getattr(args, "i", False) or getattr(args, "oi", False):
        return True
    options: list[str] = getattr(args, "options", None) or []
    return any(opt.startswith("i") for opt in options)


def _normalize_sendmail_input(raw_bytes: bytes, *, ignore_dots: bool) -> bytes:
    """Handle standard sendmail single-dot EOF and dot-unstuffing when -i/-oi is not set."""
    if ignore_dots:
        return raw_bytes

    out_lines: list[bytes] = []
    for line in raw_bytes.splitlines(keepends=True):
        if line in (b".\r\n", b".\n", b"."):
            break
        if line.startswith(b".."):
            line = line[1:]
        out_lines.append(line)
    return b"".join(out_lines)


def run(argv: list[str] | None = None) -> int:
    """Execute mail2hook CLI and return the appropriate exit code."""
    parser = build_parser()
    args, _unknown = parser.parse_known_args(argv)

    # Detect if invoked as 'mailq'
    prog_name = Path(sys.argv[0]).name if argv is None and sys.argv else ""

    if getattr(args, "bp", False) or prog_name == "mailq":
        sys.stdout.write("Mail queue is empty\n")
        return EXIT_OK

    if getattr(args, "bv", False):
        sys.stdout.write("Recipient OK: deliverable via mail2hook\n")
        return EXIT_OK

    if args.print_apt_config:
        sys.stdout.write(DEFAULT_APT_CONFIG)
        return EXIT_OK

    if getattr(args, "verbose", False):
        logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    # 1. Load and validate configuration
    try:
        config = load_config(env_file=args.env_file)
    except ConfigError as err:
        sys.stderr.write(f"mail2hook: configuration error: {err}\n")
        return EXIT_CONFIG
    except Exception as err:
        sys.stderr.write(f"mail2hook: unexpected error loading config: {err}\n")
        return EXIT_CONFIG

    # 2. Read raw email bytes (bounded to MAX_MESSAGE_BYTES)
    try:
        if args.file is not None:
            if not args.file.is_file():
                sys.stderr.write(f"mail2hook: input file not found: {args.file}\n")
                return EXIT_DATAERR
            with args.file.open("rb") as f:
                raw_bytes = f.read(MAX_MESSAGE_BYTES + 1)
        else:
            raw_bytes = sys.stdin.buffer.read(MAX_MESSAGE_BYTES + 1)
    except Exception as err:
        sys.stderr.write(f"mail2hook: failed to read email data: {err}\n")
        return EXIT_DATAERR

    if len(raw_bytes) > MAX_MESSAGE_BYTES:
        sys.stderr.write(
            f"mail2hook: message size exceeds maximum limit of {MAX_MESSAGE_BYTES} bytes\n"
        )
        return EXIT_DATAERR

    if args.file is None:
        raw_bytes = _normalize_sendmail_input(raw_bytes, ignore_dots=_ignore_dots_enabled(args))

    # 3. Parse email
    fallback_sender = _build_fallback_sender(args)
    fallback_to = (
        ", ".join(r.strip() for r in args.recipients if r.strip()) if args.recipients else None
    )
    try:
        msg = parse_email(
            raw_bytes,
            max_body_chars=config.max_body_chars,
            fallback_sender=fallback_sender,
            fallback_to=fallback_to,
        )
    except ParseError as err:
        sys.stderr.write(f"mail2hook: unparseable email data: {err}\n")
        return EXIT_DATAERR
    except Exception as err:
        sys.stderr.write(f"mail2hook: unexpected parsing error: {err}\n")
        return EXIT_DATAERR

    # 4. Apply filters (silently exit 0 if not matched)
    if not should_process_message(msg, config):
        return EXIT_OK

    # 5. Build Discord payload
    payload = build_payload(msg, config)

    # 6. Handle --dry-run
    if args.dry_run:
        sys.stdout.write(json.dumps(payload, indent=2) + "\n")
        return EXIT_OK

    # 7. Post to Discord
    try:
        post_to_discord(config.discord_webhook_url, payload)
    except DiscordTemporaryError as err:
        sys.stderr.write(f"mail2hook: temporary failure sending to Discord: {err}\n")
        return EXIT_TEMPFAIL
    except DiscordPermanentError as err:
        sys.stderr.write(f"mail2hook: permanent failure sending to Discord: {err}\n")
        if err.status_code in (401, 403, 404):
            return EXIT_CONFIG
        return EXIT_DATAERR
    except Exception as err:
        sys.stderr.write(f"mail2hook: unexpected error sending to Discord: {err}\n")
        return EXIT_TEMPFAIL

    return EXIT_OK


def main() -> None:
    """Console script entry point."""
    sys.exit(run())


if __name__ == "__main__":
    main()
