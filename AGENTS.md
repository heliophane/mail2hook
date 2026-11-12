# AGENTS.md

Guidance for AI coding agents (T3 Code, Antigravity, etc.) working in this repo.

## Project

**mail2hook** forwards emails about automatic upgrades/updates (for example from `unattended-upgrades`, `apt-listchanges`, or cron) to a Discord channel via a webhook.

It reads **one raw email message from stdin**, which is how Postfix delivers mail to a pipe command. It parses the message, optionally filters it, and posts a short summary to Discord. It then exits.

It must run on **any Linux server**, with **Debian** as the primary target, and must not depend on a specific mail provider. An IMAP polling mode may be added later as an optional extra, but it is out of scope for v1.

This repo is **public**. Never commit secrets or personal data.

## How it is deployed

- Postfix delivers mail to the tool through a pipe (an alias like `mail2hook: "|/usr/local/bin/mail2hook"` or a dedicated `pipe` transport in `master.cf` running as an unprivileged user).
- Config is read from environment variables, loaded from a root-owned, mode `0640` file such as `/etc/mail2hook/env` (group readable by the service user only), or from a local `.env` when developing.
- Installable with a plain `pipx install .` or `pip install .` into a venv. Document the Debian steps in the README.

## Stack

- Python 3.11+ (Debian 12 ships 3.11)
- Standard library `email` for parsing, `argparse` for the CLI
- Prefer standard library `urllib.request` for the Discord call to keep dependencies near zero; `python-dotenv` only for local `.env` loading
- `pytest` and `ruff` for development

If a different stack is clearly better, ask before switching.

## Security rules (highest priority)

1. **Never hardcode or print secrets.** The Discord webhook URL comes from an environment variable.
2. **Never commit `.env`.** Confirm it is listed in `.gitignore` before the first commit. Commit only `.env.example` with placeholder values.
3. **Do not log the full webhook URL or full email bodies.** Log subjects and sender at most.
4. **Treat all email content as untrusted input.** Never execute, eval, or shell out with email-derived strings. Strip or neutralize `@everyone`, `@here`, and role/user mentions by setting `allowed_mentions` to `{"parse": []}` in every Discord payload.
5. **Do not add real emails, hostnames, IPs, or account links** to tests, fixtures, docs, or commit messages. Use fake data (`user@example.com`, `server01.example.com`).
6. **Always exit with a code Postfix handles sensibly**: `0` on success. On temporary failure (Discord down, network error after retries) exit `75` (EX_TEMPFAIL) so Postfix retries later. On permanent failure (unparseable input, bad config) exit `65` or `78` and print the reason to stderr, so mail is not silently lost or looped forever.
7. If a secret is ever committed by mistake, stop and tell the user to revoke it. Deleting it in a later commit is not enough.

## Configuration

All config is via environment variables (see `.env.example`):

| Variable | Purpose |
| --- | --- |
| `DISCORD_WEBHOOK_URL` | Required. Discord webhook to post to |
| `FILTER_SENDERS` | Optional. Comma-separated sender addresses/domains to match |
| `FILTER_SUBJECT_KEYWORDS` | Optional. Comma-separated subject keywords to match |
| `MAX_BODY_CHARS` | Optional. Excerpt length, default `1500` |
| `DISCORD_USERNAME` | Optional. Display name for posts, default `mail2hook` |

If no filters are set, post every message. Fail fast with a clear stderr message if `DISCORD_WEBHOOK_URL` is missing or does not look like a Discord webhook URL.

## Behavior requirements

- Read the full raw message from stdin (`sys.stdin.buffer`) and parse it with the `email` package using the modern `policy.default`.
- Apply filters if configured. Messages that do not match are dropped silently with exit `0`.
- Post to Discord as an embed with: hostname/sender, subject, date, and a plain-text excerpt of the body.
- Discord limits: message content is **2000 characters**, embed descriptions **4096**, embed titles **256**. Truncate with an ellipsis; a long email must never cause a failed post.
- Prefer the `text/plain` part; fall back to stripping HTML tags. Decode headers and charsets properly, including non-UTF-8 bodies.
- For `unattended-upgrades` mail, make the excerpt useful: keep the package list and result lines, and drop boilerplate when practical.
- Handle Discord rate limits (HTTP 429, honor `retry_after`) and transient network errors with a small number of retries and backoff, then exit `75`.
- CLI flags: `--dry-run` (print the payload that would be sent, do not call Discord), `--file PATH` (read a message from a file instead of stdin, for testing), and `--version`.

## Code style

- Keep it small and readable. Prefer simple functions over classes unless state requires one.
- Type hints on all functions. Format with `ruff format`, lint with `ruff check`.
- Separate concerns: `config.py`, `parse.py` (email to a simple message object), `filters.py`, `discord.py` (build and send payload), `cli.py` (entry point and exit codes).
- Write tests for filtering, truncation, mention-neutralization, charset handling, HTML fallback, and exit codes using fake `.eml` fixtures. Do not hit the real Discord API in tests; mock the HTTP call.

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
