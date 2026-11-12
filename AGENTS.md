# AGENTS.md

Guidance for AI coding agents (T3 Code, Antigravity, etc.) working in this repo.

## Project

**mail2hook** forwards emails about automatic upgrades/updates (for example from `unattended-upgrades`, `apt-listchanges`, or cron) to a Discord channel via a webhook.

It can operate in three modes:
1. **Drop-in `/usr/sbin/sendmail` and `/usr/bin/mailq` replacement**: Invoked directly by `unattended-upgrades`, `cron`, or `apt-listchanges`, reading a raw email message from `stdin`.
2. **Built-in async SMTP daemon (`--smtp-listen`)**: Listens on `127.0.0.1:25` (or custom `HOST:PORT`) for local services submitting mail over TCP.
3. **Postfix pipe transport**: Reads one raw email message from `stdin` delivered by Postfix.

It must run on **any Linux server**, with **Debian and Ubuntu** as the primary targets, and must not depend on a specific mail provider.

This repo is **public**. Never commit secrets or personal data.

## How it is deployed

- Typically installed into `/opt/mail2hook/venv` and symlinked to `/usr/local/bin/mail2hook`, `/usr/sbin/sendmail`, and `/usr/bin/mailq` (or run as a systemd service with `--smtp-listen`, or piped from Postfix).
- Config is read from environment variables, loaded from `/etc/mail2hook/env` (mode `0600` when run by root via `sendmail`/`systemd`, or `0640` group-readable if run as a dedicated service user), `--env-file PATH`, or a local `.env` when developing.
- Installable with `pip install .` into a venv or `pipx install .`.

## Stack

- Python 3.11+ (Debian 12 ships 3.11)
- Standard library `email` for parsing, `argparse` for the CLI, `asyncio` for the optional SMTP daemon
- Standard library `urllib.request` for the Discord call to keep dependencies near zero; `python-dotenv` only for env file loading
- `pytest` and `ruff` for development

If a different stack is clearly better, ask before switching.

## Security rules (highest priority)

1. **Never hardcode or print secrets.** The Discord webhook URL comes from an environment variable.
2. **Never commit `.env`.** Confirm it is listed in `.gitignore` before the first commit. Commit only `.env.example` with placeholder values.
3. **Do not log the full webhook URL or full email bodies.** Log subjects and sender at most.
4. **Treat all email content as untrusted input.** Never execute, eval, or shell out with email-derived strings. Strip or neutralize `@everyone`, `@here`, and role/user mentions by setting `allowed_mentions` to `{"parse": []}` in every Discord payload. Sanitize inline code spans so untrusted headers cannot break embed formatting.
5. **Do not add real emails, hostnames, IPs, or account links** to tests, fixtures, docs, or commit messages. Use fake data (`user@example.com`, `server01.example.com`).
6. **Always exit with a code `sysexits.h` / Postfix handles sensibly**: `0` on success. On temporary failure (Discord down, rate limit, network error after retries) exit `75` (`EX_TEMPFAIL`) so MTAs retry later. On permanent data failure (unparseable input, oversized message, HTTP 400) exit `65` (`EX_DATAERR`) and on configuration/webhook auth errors (missing config, HTTP 401/403/404) exit `78` (`EX_CONFIG`), printing the reason to stderr.
7. If a secret is ever committed by mistake, stop and tell the user to revoke it. Deleting it in a later commit is not enough.

## Configuration

All config is via environment variables (see `.env.example`):

| Variable | Purpose |
| --- | --- |
| `DISCORD_WEBHOOK_URL` | Required (aliases: `DISCORD_WEBHOOK`, `WEBHOOK_URL`). Discord webhook to post to |
| `SERVER_NAME` | Optional (alias: `HOSTNAME_OVERRIDE`). Server name override for multi-server setups |
| `FILTER_SENDERS` | Optional. Comma-separated sender addresses, domains, usernames, or display names to match |
| `FILTER_SUBJECT_KEYWORDS` | Optional. Comma-separated subject keywords to match (case-insensitive) |
| `MAX_BODY_CHARS` | Optional. Excerpt length, default `1500` |
| `DISCORD_USERNAME` | Optional. Display name for posts, default `mail2hook` |

If no filters are set, post every message. Fail fast with a clear stderr message if `DISCORD_WEBHOOK_URL` is missing or does not look like a Discord webhook URL.

## Behavior requirements

- Read raw email bytes from `stdin` (`sys.stdin.buffer`, bounded by `MAX_MESSAGE_BYTES`) and parse with the `email` package using `policy.default`.
- Apply filters if configured. Messages that do not match are dropped silently with exit `0`.
- Post to Discord as an embed with: server hostname, OS distribution, reboot status (when applicable), sender, subject, date, log inspection commands, and a plain-text excerpt of the body.
- Discord limits: message content is **2000 characters**, embed descriptions **4096**, embed titles **256**. Truncate with an ellipsis; a long email must never cause a failed post.
- Prefer the `text/plain` part; fall back to stripping HTML tags. Decode headers and charsets properly, including non-UTF-8 bodies.
- For `unattended-upgrades` mail, make the excerpt useful: keep the package list and result lines, highlight errors, and detect reboot requirements.
- Handle Discord rate limits (HTTP 429, honor `Retry-After`) and transient network/5xx errors with retries and backoff, then exit `75`.
- CLI flags: `--dry-run`, `--file PATH`, `--env-file PATH`, `--smtp-listen [HOST:]PORT`, `--print-apt-config`, `--print-systemd-service`, `--version`, and standard `sendmail`/`mailq` compatibility flags (`-t`, `-i`, `-oi`, `-f`, `-r`, `-F`, `-bm`, `-bp`, `-bv`, `-bs`, `-B`, `-v`, `-o...`).

## Code style

- Keep it small and readable. Prefer simple functions over classes unless state requires one.
- Type hints on all functions. Format with `ruff format`, lint with `ruff check`.
- Separate concerns: `config.py`, `parse.py` (email to `ParsedMessage`), `filters.py`, `discord.py` (build and send payload), `smtp.py` (async SMTP daemon), `cli.py` (entry point and exit codes).
- Write tests for filtering, truncation, mention-neutralization, charset handling, HTML fallback, SMTP transactions, and exit codes using fake `.eml` fixtures. Do not hit the real Discord API in tests; mock the HTTP call.

## Workflow

- Work on a branch and make small, focused commits with clear messages.
- Run `ruff check`, `ruff format --check`, and `pytest` before committing.
- Run `git status` before every commit and confirm no `.env` or other local files are staged.
- Update `README.md` when behavior or configuration changes.
- Ask before adding a new dependency or changing the project structure.

## Out of scope (for now)

- IMAP polling or Exchange Online / Microsoft Graph integration
- Replying to or deleting emails
- Web UI or database
- Forwarding attachments
- Deduplication (Postfix delivers each message once)
