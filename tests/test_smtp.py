"""Tests for the built-in SMTP server daemon in mail2hook."""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from mail2hook.config import Config
from mail2hook.smtp import run_smtp_server

VALID_WEBHOOK_URL = (
    "https://discord.com/api/webhooks/123456789012345678/abcdefghijklmnopqrstuvwxyz0123456789"
)


@pytest.fixture
def test_config() -> Config:
    return Config(
        discord_webhook_url=VALID_WEBHOOK_URL,
        server_name="testserver",
    )


def test_smtp_server_complete_flow(test_config: Config) -> None:
    """Test full SMTP transaction: EHLO -> MAIL FROM -> RCPT TO -> DATA -> QUIT."""

    async def _test() -> None:
        stop_event = asyncio.Event()
        port_future: asyncio.Future[int] = asyncio.get_running_loop().create_future()

        server_task = asyncio.create_task(
            run_smtp_server(
                "127.0.0.1",
                0,
                test_config,
                stop_event=stop_event,
                ready_callback=port_future.set_result,
            )
        )

        port = await port_future

        try:
            with patch("mail2hook.smtp.post_to_discord") as mock_post:
                reader, writer = await asyncio.open_connection("127.0.0.1", port)

                banner = await reader.readline()
                assert banner.startswith(b"220 ")

                writer.write(b"EHLO client.example.com\r\n")
                await writer.drain()
                ehlo_resp1 = await reader.readline()
                assert ehlo_resp1.startswith(b"250-")
                ehlo_resp2 = await reader.readline()
                assert ehlo_resp2.startswith(b"250-")
                ehlo_resp3 = await reader.readline()
                assert ehlo_resp3.startswith(b"250-")
                ehlo_resp4 = await reader.readline()
                assert ehlo_resp4.startswith(b"250 ")

                writer.write(b"MAIL FROM:<root@server01.example.com>\r\n")
                await writer.drain()
                mail_resp = await reader.readline()
                assert mail_resp.startswith(b"250 ")

                writer.write(b"RCPT TO:<mail2hook@localhost>\r\n")
                await writer.drain()
                rcpt_resp = await reader.readline()
                assert rcpt_resp.startswith(b"250 ")

                writer.write(b"DATA\r\n")
                await writer.drain()
                data_resp = await reader.readline()
                assert data_resp.startswith(b"354 ")

                email_body = (
                    b"Subject: unattended-upgrades result: SUCCESS\r\n"
                    b"To: root@server01.example.com\r\n"
                    b"\r\n"
                    b"All upgrades installed successfully.\r\n"
                    b"..\r\n"  # Dot transparency stuffing test
                    b".\r\n"
                )
                writer.write(email_body)
                await writer.drain()

                done_resp = await reader.readline()
                assert done_resp.startswith(b"250 ")

                # After DATA completes, envelope state must be reset (DATA without new RCPT fails)
                writer.write(b"DATA\r\n")
                await writer.drain()
                data_err = await reader.readline()
                assert data_err.startswith(b"503 ")

                writer.write(b"QUIT\r\n")
                await writer.drain()
                quit_resp = await reader.readline()
                assert quit_resp.startswith(b"221 ")

                writer.close()
                await writer.wait_closed()

                assert mock_post.call_count == 1
                payload = mock_post.call_args[0][1]
                assert "SUCCESS" in payload["embeds"][0]["title"]
                assert payload["embeds"][0]["author"]["name"].startswith("Server: testserver")
                # Envelope sender fallback used when From: header was omitted
                from_field = next(f for f in payload["embeds"][0]["fields"] if f["name"] == "From")
                assert "root@server01.example.com" in from_field["value"]
        finally:
            stop_event.set()
            await server_task

    asyncio.run(_test())


def test_smtp_server_commands(test_config: Config) -> None:
    """Test NOOP, RSET, unrecognized command, and empty DATA error."""

    async def _test() -> None:
        stop_event = asyncio.Event()
        port_future: asyncio.Future[int] = asyncio.get_running_loop().create_future()

        server_task = asyncio.create_task(
            run_smtp_server(
                "127.0.0.1",
                0,
                test_config,
                stop_event=stop_event,
                ready_callback=port_future.set_result,
            )
        )

        port = await port_future

        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            await reader.readline()  # banner

            # NOOP
            writer.write(b"NOOP\r\n")
            await writer.drain()
            noop_resp = await reader.readline()
            assert noop_resp.startswith(b"250 ")

            # DATA without RCPT
            writer.write(b"DATA\r\n")
            await writer.drain()
            data_err = await reader.readline()
            assert data_err.startswith(b"503 ")

            # Unrecognized command
            writer.write(b"FOOBAR\r\n")
            await writer.drain()
            unrec_resp = await reader.readline()
            assert unrec_resp.startswith(b"500 ")

            # RSET
            writer.write(b"RSET\r\n")
            await writer.drain()
            rset_resp = await reader.readline()
            assert rset_resp.startswith(b"250 ")

            writer.write(b"QUIT\r\n")
            await writer.drain()
            await reader.readline()

            writer.close()
            await writer.wait_closed()
        finally:
            stop_event.set()
            await server_task

    asyncio.run(_test())


def test_smtp_server_esmtp_params_and_size_limit(
    test_config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test ESMTP parameters on MAIL FROM / RCPT TO and rejection of oversized messages."""
    monkeypatch.setattr("mail2hook.smtp.MAX_MESSAGE_BYTES", 64)

    async def _test() -> None:
        stop_event = asyncio.Event()
        port_future: asyncio.Future[int] = asyncio.get_running_loop().create_future()

        server_task = asyncio.create_task(
            run_smtp_server(
                "127.0.0.1",
                0,
                test_config,
                stop_event=stop_event,
                ready_callback=port_future.set_result,
            )
        )
        port = await port_future

        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            await reader.readline()  # banner

            writer.write(b"MAIL FROM:<root@server01.example.com> SIZE=1234 BODY=8BITMIME\r\n")
            await writer.drain()
            assert (await reader.readline()).startswith(b"250 ")

            writer.write(b"RCPT TO:<>\r\n")
            await writer.drain()
            assert (await reader.readline()).startswith(b"501 ")

            writer.write(b"RCPT TO:<admin@server01.example.com>\r\n")
            await writer.drain()
            assert (await reader.readline()).startswith(b"250 ")

            writer.write(b"DATA\r\n")
            await writer.drain()
            assert (await reader.readline()).startswith(b"354 ")

            writer.write(b"Subject: big\r\n\r\n" + (b"x" * 100) + b"\r\n.\r\n")
            await writer.drain()
            assert (await reader.readline()).startswith(b"552 ")

            writer.write(b"QUIT\r\n")
            await writer.drain()
            await reader.readline()

            writer.close()
            await writer.wait_closed()
        finally:
            stop_event.set()
            await server_task

    asyncio.run(_test())
