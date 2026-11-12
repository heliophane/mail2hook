"""Command-line interface and entry point for mail2hook."""

from __future__ import annotations

import argparse
import json
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

# Postfix / sysexits.h standard exit codes
EXIT_OK = 0  # EX_OK: Success or message filtered out
EXIT_DATAERR = 65  # EX_DATAERR: The input data was incorrect (unparseable email)
EXIT_TEMPFAIL = 75  # EX_TEMPFAIL: Temporary failure (retry later)
EXIT_CONFIG = 78  # EX_CONFIG: Configuration error (missing/invalid webhook, etc.)


def build_parser() -> argparse.ArgumentParser:
    """Create command-line argument parser."""
    parser = argparse.ArgumentParser(
        prog="mail2hook",
        description="Read raw email from stdin and forward a summary to a Discord webhook.",
    )
    parser.add_argument(
        "--file",
        "-f",
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
        "--version",
        "-v",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    return parser


def run(argv: list[str] | None = None) -> int:
    """Execute mail2hook CLI and return the appropriate exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)

    # 1. Load and validate configuration
    try:
        config = load_config(env_file=args.env_file)
    except ConfigError as err:
        sys.stderr.write(f"mail2hook: configuration error: {err}\n")
        return EXIT_CONFIG
    except Exception as err:
        sys.stderr.write(f"mail2hook: unexpected error loading config: {err}\n")
        return EXIT_CONFIG

    # 2. Read raw email bytes
    try:
        if args.file is not None:
            if not args.file.is_file():
                sys.stderr.write(f"mail2hook: input file not found: {args.file}\n")
                return EXIT_DATAERR
            raw_bytes = args.file.read_bytes()
        else:
            raw_bytes = sys.stdin.buffer.read()
    except Exception as err:
        sys.stderr.write(f"mail2hook: failed to read email data: {err}\n")
        return EXIT_DATAERR

    # 3. Parse email
    try:
        msg = parse_email(raw_bytes, max_body_chars=config.max_body_chars)
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
