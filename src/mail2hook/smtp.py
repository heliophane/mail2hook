"""Lightweight, standard-library SMTP server daemon for mail2hook."""

from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING

from mail2hook.discord import DiscordTemporaryError, build_payload, post_to_discord
from mail2hook.filters import should_process_message
from mail2hook.parse import ParseError, parse_email

if TYPE_CHECKING:
    from mail2hook.config import Config

logger = logging.getLogger(__name__)

MAX_MESSAGE_BYTES = 10 * 1024 * 1024  # 10 MiB (matches EHLO SIZE advertisement)
READ_TIMEOUT_SECONDS = 60.0
_ADDR_BRACKET_RE = re.compile(r"<([^>]*)>")


def _extract_envelope_address(value: str) -> str:
    """Extract the email address from an SMTP MAIL FROM / RCPT TO argument."""
    match = _ADDR_BRACKET_RE.search(value)
    if match:
        return match.group(1).strip()
    return value.strip().split()[0] if value.strip() else ""


class SMTPSession:
    """Handles an individual SMTP client connection."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        config: Config,
    ) -> None:
        self.reader = reader
        self.writer = writer
        self.config = config
        self.envelope_from: str = ""
        self.envelope_to: list[str] = []

    async def write_line(self, line: str) -> None:
        """Write an RFC 5321 response line ending in CRLF."""
        self.writer.write(f"{line}\r\n".encode())
        await self.writer.drain()

    async def handle(self) -> None:
        """Process SMTP commands for the connection."""
        try:
            await self.write_line("220 mail2hook ESMTP ready")

            data_mode = False
            data_buffer: list[bytes] = []
            data_bytes_count = 0
            data_oversized = False

            while True:
                try:
                    line_bytes = await asyncio.wait_for(
                        self.reader.readline(), timeout=READ_TIMEOUT_SECONDS
                    )
                except TimeoutError:
                    await self.write_line("421 4.4.2 Connection timed out")
                    break

                if not line_bytes:
                    break

                if data_mode:
                    # Check for end of DATA: single dot on a line
                    if line_bytes in (b".\r\n", b".\n"):
                        data_mode = False
                        if data_oversized:
                            data_buffer.clear()
                            data_bytes_count = 0
                            data_oversized = False
                            await self.write_line(
                                "552 5.3.4 Message size exceeds fixed maximum message size"
                            )
                            continue
                        raw_message = b"".join(data_buffer)
                        data_buffer.clear()
                        data_bytes_count = 0
                        await self._deliver(raw_message)
                    else:
                        # RFC 5321 transparency un-stuffing (leading .. becomes .)
                        if line_bytes.startswith(b".."):
                            line_bytes = line_bytes[1:]
                        if not data_oversized:
                            if data_bytes_count + len(line_bytes) > MAX_MESSAGE_BYTES:
                                data_oversized = True
                                data_buffer.clear()
                            else:
                                data_buffer.append(line_bytes)
                                data_bytes_count += len(line_bytes)
                    continue

                command_line = line_bytes.decode("utf-8", errors="replace").strip()
                if not command_line:
                    continue

                parts = command_line.split(" ", 1)
                cmd = parts[0].upper()
                arg = parts[1] if len(parts) > 1 else ""

                if cmd in ("HELO", "EHLO"):
                    await self.write_line("250-mail2hook Hello")
                    await self.write_line("250-8BITMIME")
                    await self.write_line(f"250-SIZE {MAX_MESSAGE_BYTES}")
                    await self.write_line("250 OK")

                elif cmd == "MAIL":
                    # Expected format: MAIL FROM:<address> [ESMTP params]
                    if arg.upper().startswith("FROM:"):
                        self.envelope_from = _extract_envelope_address(arg[5:])
                        await self.write_line("250 2.1.0 Sender OK")
                    else:
                        await self.write_line("501 5.5.4 Syntax error in MAIL FROM parameter")

                elif cmd == "RCPT":
                    # Expected format: RCPT TO:<address> [ESMTP params]
                    if arg.upper().startswith("TO:"):
                        recipient = _extract_envelope_address(arg[3:])
                        if not recipient:
                            await self.write_line("501 5.1.3 Empty recipient address")
                            continue
                        self.envelope_to.append(recipient)
                        await self.write_line("250 2.1.5 Recipient OK")
                    else:
                        await self.write_line("501 5.5.4 Syntax error in RCPT TO parameter")

                elif cmd == "DATA":
                    if not self.envelope_to:
                        await self.write_line("503 5.5.1 Need RCPT command")
                        continue
                    data_mode = True
                    data_buffer.clear()
                    data_bytes_count = 0
                    data_oversized = False
                    await self.write_line("354 Start mail input; end with <CRLF>.<CRLF>")

                elif cmd == "RSET":
                    self.envelope_from = ""
                    self.envelope_to.clear()
                    data_buffer.clear()
                    data_bytes_count = 0
                    data_oversized = False
                    data_mode = False
                    await self.write_line("250 2.0.0 Reset state OK")

                elif cmd == "NOOP":
                    await self.write_line("250 2.0.0 OK")

                elif cmd == "QUIT":
                    await self.write_line("221 2.0.0 mail2hook closing connection")
                    break

                else:
                    await self.write_line(f"500 5.5.2 Command unrecognized: {cmd}")
        except (ConnectionError, EOFError):
            logger.debug("SMTP client disconnected")
        finally:
            try:
                self.writer.close()
                await self.writer.wait_closed()
            except Exception:
                pass

    async def _deliver(self, raw_bytes: bytes) -> None:
        """Parse received email and post to Discord."""
        try:
            msg = parse_email(raw_bytes, max_body_chars=self.config.max_body_chars)
        except ParseError as err:
            logger.warning("Unparseable email received via SMTP: %s", err)
            await self.write_line(f"554 5.6.0 Message rejected: unparseable email ({err})")
            return

        if not should_process_message(msg, self.config):
            logger.info("Message filtered out silently: %s", msg.subject)
            await self.write_line("250 2.0.0 Message accepted (filtered)")
            return

        payload = build_payload(msg, self.config)

        # Run synchronous HTTP request in thread pool to avoid blocking asyncio event loop
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(
                None, post_to_discord, self.config.discord_webhook_url, payload
            )
            await self.write_line("250 2.0.0 Message accepted and forwarded to Discord")
        except DiscordTemporaryError as err:
            logger.error("Temporary failure posting to Discord: %s", err)
            await self.write_line("451 4.3.0 Temporary delivery failure, please retry later")
        except Exception as err:
            logger.error("Permanent failure posting to Discord: %s", err)
            await self.write_line(f"554 5.3.0 Delivery failed: {err}")


async def run_smtp_server(
    host: str,
    port: int,
    config: Config,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Run the SMTP server loop until stopped."""

    async def client_connected(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        session = SMTPSession(reader, writer, config)
        await session.handle()

    server = await asyncio.start_server(client_connected, host, port)
    logger.info("mail2hook SMTP daemon listening on %s:%d", host, port)

    async with server:
        if stop_event is not None:
            await stop_event.wait()
        else:
            await server.serve_forever()
