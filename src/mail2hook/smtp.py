"""Lightweight, standard-library SMTP server daemon for mail2hook."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from mail2hook.discord import DiscordTemporaryError, build_payload, post_to_discord
from mail2hook.filters import should_process_message
from mail2hook.parse import ParseError, parse_email

if TYPE_CHECKING:
    from mail2hook.config import Config

logger = logging.getLogger(__name__)


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
        await self.write_line("220 mail2hook ESMTP ready")

        data_mode = False
        data_buffer: list[bytes] = []

        while True:
            line_bytes = await self.reader.readline()
            if not line_bytes:
                break

            if data_mode:
                # Check for end of DATA: single dot on a line
                if line_bytes in (b".\r\n", b".\n"):
                    data_mode = False
                    raw_message = b"".join(data_buffer)
                    data_buffer.clear()
                    await self._deliver(raw_message)
                else:
                    # RFC 5321 transparency un-stuffing (leading .. becomes .)
                    if line_bytes.startswith(b".."):
                        line_bytes = line_bytes[1:]
                    data_buffer.append(line_bytes)
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
                await self.write_line("250-SIZE 10485760")
                await self.write_line("250 OK")

            elif cmd == "MAIL":
                # Expected format: MAIL FROM:<address>
                clean_arg = arg.upper()
                if clean_arg.startswith("FROM:"):
                    self.envelope_from = arg[5:].strip().strip("<>")
                    await self.write_line("250 2.1.0 Sender OK")
                else:
                    await self.write_line("501 5.5.4 Syntax error in MAIL FROM parameter")

            elif cmd == "RCPT":
                # Expected format: RCPT TO:<address>
                clean_arg = arg.upper()
                if clean_arg.startswith("TO:"):
                    recipient = arg[3:].strip().strip("<>")
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
                await self.write_line("354 Start mail input; end with <CRLF>.<CRLF>")

            elif cmd == "RSET":
                self.envelope_from = ""
                self.envelope_to.clear()
                data_buffer.clear()
                data_mode = False
                await self.write_line("250 2.0.0 Reset state OK")

            elif cmd == "NOOP":
                await self.write_line("250 2.0.0 OK")

            elif cmd == "QUIT":
                await self.write_line("221 2.0.0 mail2hook closing connection")
                break

            else:
                await self.write_line(f"500 5.5.2 Command unrecognized: {cmd}")

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
