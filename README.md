# mail2hook

[![CI](https://github.com/heliophane/mail2hook/actions/workflows/ci.yml/badge.svg)](https://github.com/heliophane/mail2hook/actions)

A lightweight Python tool that forwards system emails to a Discord webhook.

`mail2hook` can **completely replace Postfix/Exim** as a drop-in `/usr/sbin/sendmail` binary, run as a zero-dependency lightweight SMTP daemon on `127.0.0.1:25`, or integrate into an existing Postfix installation via pipe transport.

Specifically designed for automated system notifications:
- **`unattended-upgrades`** (package installation results, security updates, failure warnings, reboot notifications)
- **`apt-listchanges`** (changelogs and critical package alerts)
- **`cron`** / systemd timer output

---

## Features

- **No Postfix/Exim Required**: Acts directly as a `/usr/sbin/sendmail` and `/usr/bin/mailq` drop-in binary. Accepts standard sendmail flags (`-t`, `-oi`, `-i`, `-f`, `-F`, `-bm`, `-bp`, `-bv`, `-B`, `-o...`).
- **Built-in Async SMTP Daemon**: Optional zero-dependency lightweight RFC 5321 SMTP server (`mail2hook --smtp-listen 127.0.0.1:25`) for services that deliver over TCP sockets.
- **Near-Zero Dependencies**: Uses standard Python library modules (`email`, `urllib.request`, `asyncio`, `argparse`) with only `python-dotenv` for configuration loading.
- **Reboot Detection**: Automatically displays `Reboot: ⚠️ Required` (or `🟢 Not needed`) and adds a `sudo reboot` hint when `/run/reboot-required` exists or an upgrade log indicates a restart is needed.
- **Multi-Server Friendly**: Automatically detects OS distribution and server hostname, with optional `SERVER_NAME` override.
- **Strict Security**:
  - Webhook URL token is never logged or printed in clear text.
  - Mentions are neutralized with `"allowed_mentions": {"parse": []}` to prevent email content from triggering `@everyone`, `@here`, or role pings.
- **Robust Charset & Multipart Handling**: Parses UTF-8, ISO-8859-1/Latin-1, Windows-1252, and multi-part messages; prefers `text/plain` and cleanly strips `text/html`.
- **Specialized Summary Formatting**: Automatically formats `unattended-upgrades` package lists vertically with `old → new` version transitions extracted from `dpkg` / `apt` logs and highlights genuine errors.
- **Rate Limit & Transient Retry**: Automatically handles Discord HTTP `429` (honoring `Retry-After`) and transient `5xx` / network failures with exponential backoff.
- **Optional Filtering**: Drop unrelated emails silently by matching sender addresses/domains or subject keywords.

---

## 1. Creating a Discord Webhook

1. In Discord, go to **Server Settings** > **Integrations** > **Webhooks** (or channel settings > **Integrations**).
2. Click **New Webhook**.
3. Choose a name (e.g. `Server Updates`) and select the target channel.
4. Click **Copy Webhook URL**. The URL looks like:
   ```text
   https://discord.com/api/webhooks/123456789012345678/example-token-placeholder
   ```

---

## 2. Installation (Ubuntu 24/26 & Debian 12/13)

For a complete multi-server deployment walkthrough, see [docs/MULTI_SERVER_SETUP.md](docs/MULTI_SERVER_SETUP.md).

### Step 1: Install prerequisites & remove existing MTAs

Install Python virtual environment tools and remove any conflicting Mail Transfer Agents (`postfix`, `exim4`, etc.):

**On Ubuntu 24.04 LTS & 26.04 LTS:**
```bash
sudo apt update && sudo apt install -y python3 python3-venv git unattended-upgrades
sudo systemctl stop postfix 2>/dev/null || true
sudo apt purge postfix bsd-mailx -y
```

**On Debian 12 (Bookworm) & Debian 13 (Trixie):**
```bash
sudo apt update && sudo apt install -y python3 python3-venv git unattended-upgrades
# Debian ships with exim4 by default; purge it to free /usr/sbin/sendmail
sudo systemctl stop exim4 2>/dev/null || true
sudo systemctl stop postfix 2>/dev/null || true
sudo apt purge 'exim4*' postfix bsd-mailx -y
```

### Step 2: Install in a dedicated virtual environment

```bash
sudo mkdir -p /opt/mail2hook /etc/mail2hook
sudo python3 -m venv /opt/mail2hook/venv
sudo /opt/mail2hook/venv/bin/pip install --upgrade pip
sudo /opt/mail2hook/venv/bin/pip install --upgrade git+https://github.com/heliophane/mail2hook.git
sudo ln -sf /opt/mail2hook/venv/bin/mail2hook /usr/local/bin/mail2hook

# Symlink mail2hook as the system sendmail and mailq binaries
sudo ln -sf /opt/mail2hook/venv/bin/mail2hook /usr/sbin/sendmail
sudo ln -sf /opt/mail2hook/venv/bin/mail2hook /usr/bin/mailq
```

### Step 3: Configure `/etc/mail2hook/env`

Create and secure the environment configuration:

```bash
sudo touch /etc/mail2hook/env
sudo chmod 0600 /etc/mail2hook/env
```

Edit `/etc/mail2hook/env`:

```ini
# Required: Discord webhook URL
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/123456789012345678/example-token-placeholder

# Optional: Friendly server name display in Discord (defaults to system hostname)
# SERVER_NAME=my-server-name

# Optional: Comma-separated sender addresses or domains to match. If unset, all senders match.
# FILTER_SENDERS=root@my-server, unattended-upgrades

# Optional: Comma-separated subject keywords to match (case-insensitive). If unset, all subjects match.
# FILTER_SUBJECT_KEYWORDS=unattended-upgrades, apt-listchanges, cron

# Optional: Maximum body characters to include in Discord excerpt (default: 1500)
# MAX_BODY_CHARS=1500

# Optional: Bot display name in Discord (default: mail2hook)
# DISCORD_USERNAME=mail2hook
```

*(With `0600` permissions, `root` processes such as `unattended-upgrades` and root `cron` jobs can read the config while keeping the webhook secret hidden from unprivileged users. If non-root local services need to send mail, use the built-in SMTP daemon below.)*

---

## 3. Configure `unattended-upgrades`

In Linux, tools like `unattended-upgrades`, `cron`, and `apt-listchanges` invoke `/usr/sbin/sendmail -t -oi` directly and write raw email to `stdin`. Because `mail2hook` is symlinked to `/usr/sbin/sendmail`, no background mail daemon is needed.

Generate and install the drop-in configuration directly:

```bash
mail2hook --print-apt-config | sudo tee /etc/apt/apt.conf.d/52mail2hook
```

*(Using a dedicated `52mail2hook` drop-in file avoids modifying `/etc/apt/apt.conf.d/50unattended-upgrades`, preventing dpkg configuration merge prompts during future Debian/Ubuntu package upgrades).*

---

## 4. Alternative: Built-in SMTP Daemon (`127.0.0.1:25`)

If you have applications on your server that connect to `localhost:25` over TCP rather than invoking `sendmail`, or unprivileged local users that should send mail without reading `/etc/mail2hook/env`, you can run `mail2hook` as a systemd SMTP service without Postfix:

1. Generate and install the systemd service unit:
   ```bash
   mail2hook --print-systemd-service | sudo tee /etc/systemd/system/mail2hook.service
   ```

2. Reload systemd, enable, and start the service:
   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable --now mail2hook.service
   ```

3. Check service status:
   ```bash
   sudo systemctl status mail2hook.service
   ```

---

## 5. Alternative: Postfix Integration (If keeping Postfix)

If your server already uses Postfix for other mail routing, configure Postfix to pipe messages to `mail2hook`:

1. Add the transport definition to `/etc/postfix/master.cf`:
   ```text
   mail2hook unix  -       n       n       -       -       pipe
     flags=FR user=nobody argv=/usr/local/bin/mail2hook
   ```

2. Configure a transport map in `/etc/postfix/transport`:
   ```text
   mail2hook@localhost    mail2hook:
   ```

3. Compile the transport database and reload Postfix:
   ```bash
   sudo postmap /etc/postfix/transport
   sudo postconf -e "transport_maps = hash:/etc/postfix/transport"
   sudo systemctl reload postfix
   ```

---

## 6. CLI Usage & Testing

### Test sendmail drop-in directly:

```bash
printf 'Subject: unattended-upgrades result for server: SUCCESS\nFrom: root <root@example.com>\n\nAll upgrades installed successfully.\nPackages that were upgraded:\n libssl3 openssl\n' | sudo /usr/sbin/sendmail -t -oi root
```

### Test dry-run output (does not send to Discord):

```bash
printf 'Subject: Test Notification\nFrom: root <root@example.com>\n\nTest message.\n' | sudo mail2hook --dry-run
```

### Test live unattended-upgrades run:

```bash
sudo unattended-upgrades --dry-run --debug
```

### Test mail queue command:

```bash
mailq
# Output: Mail queue is empty
```

### Command Flags:

- `--file PATH`: Read raw email from a file instead of stdin.
- `--dry-run`: Parse and format payload, printing JSON to stdout without sending to Discord.
- `--env-file PATH`: Use a specific environment file instead of `/etc/mail2hook/env` or `.env`.
- `--smtp-listen [HOST:]PORT`: Run the built-in SMTP daemon (default `127.0.0.1:25`).
- `--print-apt-config`: Print recommended `/etc/apt/apt.conf.d/52mail2hook` configuration and exit.
- `--print-systemd-service`: Print systemd service unit template for `--smtp-listen` and exit.
- `--version`, `-V`: Display version information and exit.
- Sendmail compatibility flags: `-t`, `-i`, `-oi`, `-bm`, `-bp`, `-bv`, `-bs`, `-f`, `-r`, `-F`, `-B`, `-v`, `-o...`

---

## 7. Exit Code Reference

`mail2hook` conforms to standard `sysexits.h` exit codes:

| Exit Code | Name | Description |
| --- | --- | --- |
| `0` | `EX_OK` | Message successfully sent or filtered out. |
| `65` | `EX_DATAERR` | Unparseable email input, oversized message, or rejected payload (`400`). |
| `75` | `EX_TEMPFAIL` | Discord rate limit exhausted, `5xx` server error, network timeout (retry later). |
| `78` | `EX_CONFIG` | Missing/invalid configuration or unauthorized/deleted webhook (`401`/`403`/`404`). |

---

## 8. Development

```bash
# Create venv and install dependencies
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# Run linter and formatting checks
ruff check .
ruff format --check .

# Run test suite
pytest
```

---

## License

MIT
