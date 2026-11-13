# AGENTS.md

Guidance for AI coding agents working in this repo.

## Project

**mail2hook** is a lightweight `sendmail` shim that forwards system emails (`unattended-upgrades`, `apt-listchanges`, `cron`) to a Discord webhook.

Design constraints:
- **No daemon, no listening port, no dedicated service user.**
- Runs only when invoked (directly or via `/usr/sbin/sendmail`), reads one raw email message from `stdin`, posts a formatted summary to Discord, and exits.
- Minimal dependencies: pure Python 3.11+ standard library only (`email`, `urllib.request`, `json`, `argparse`, `html.parser`). No third-party runtime dependencies.
- Primary targets: Debian 12/13 and Ubuntu 24.04/26.04.

This repo is **public**. Never commit secrets or personal data.

## How it is deployed

- Installed as a standalone executable at `/usr/local/bin/mail2hook`, registered via `update-alternatives` (`mta` -> `/usr/sbin/sendmail`, slave `mta-mailq` -> `/usr/bin/mailq`) when no conflicting MTA is present.
- Config is read from `/etc/mail2hook/env` (`root:mail`, mode `0640`), `--env-file PATH`, `.env`, or environment variables.
- Installable via `install.sh`, `pip install .`, or `pipx install .`.

## Security rules (highest priority)

1. **Never hardcode or print secrets.** The Discord webhook URL comes from `/etc/mail2hook/env` or environment variables, never CLI arguments.
2. **Never commit `.env`.** Commit only `.env.example` with placeholder values.
3. **Do not log the webhook URL or full email bodies.** Always use `redact_webhook_url` in errors.
4. **Treat all email content as untrusted input.** Never execute, eval, or shell out with email-derived strings. Set `"allowed_mentions": {"parse": []}` in every Discord payload and sanitize inline code spans.
5. **Do not add real emails, hostnames, IPs, or account links** to tests, fixtures, docs, or commit messages. Use fake data (`user@example.com`, `server01.example.com`).
6. **Exit with standard `sysexits.h` codes**: `0` (`EX_OK`) on success/filtered, `65` (`EX_DATAERR`) on unparseable/oversized input or HTTP `400`, `75` (`EX_TEMPFAIL`) on rate limit/`5xx`/network errors, `78` (`EX_CONFIG`) on missing/invalid config or HTTP `401`/`403`/`404`.

## Configuration

All config is via environment variables (see `.env.example`):

| Variable | Purpose |
| --- | --- |
| `DISCORD_WEBHOOK_URL` | Required (aliases: `DISCORD_WEBHOOK`, `WEBHOOK_URL`). Discord webhook URL |
| `SERVER_NAME` | Optional (alias: `HOSTNAME_OVERRIDE`). Server name override |
| `FILTER_SENDERS` | Optional. Comma-separated sender addresses, domains, or usernames to match |
| `FILTER_SUBJECT_KEYWORDS` | Optional. Comma-separated subject keywords to match (case-insensitive) |
| `MAX_BODY_CHARS` | Optional. Excerpt length, default `1500` |
| `DISCORD_USERNAME` | Optional. Display name for posts, default `mail2hook` |

## Code style

- Keep it small and readable. Prefer simple functions over classes.
- Type hints on all functions. Format with `ruff format`, lint with `ruff check`.
- Modules: `config.py`, `parse.py`, `filters.py`, `discord.py`, `cli.py`.
- Run `ruff check .`, `ruff format --check .`, `pytest`, and `shellcheck install.sh` before committing.
