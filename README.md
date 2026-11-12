# mail2hook

[![CI](https://github.com/heliophane/mail2hook/actions/workflows/ci.yml/badge.svg)](https://github.com/heliophane/mail2hook/actions)

A lightweight Python tool that forwards system emails to a Discord webhook.

`mail2hook` can **completely replace Postfix** as a drop-in `/usr/sbin/sendmail` binary, run as a zero-dependency lightweight SMTP daemon on `127.0.0.1:25`, or integrate into an existing Postfix installation via pipe transport.

Specifically designed for automated system notifications:
- **`unattended-upgrades`** (package installation results, security updates, failure warnings, reboot notifications)
- **`apt-listchanges`** (changelogs and critical package alerts)
- **`cron`** / systemd timer output

---

## Features

- **No Postfix Required**: Acts directly as a `/usr/sbin/sendmail` and `/usr/bin/mailq` drop-in binary. Accepts standard sendmail flags (`-t`, `-oi`, `-i`, `-f`, `-F`, `-bm`, `-bp`, `-bv`, `-B`, `-o...`).
- **Built-in Async SMTP Daemon**: Optional zero-dependency lightweight RFC 5321 SMTP server (`mail2hook --smtp-listen 127.0.0.1:25`) for services that deliver over TCP sockets.
- **Zero Heavy Dependencies**: Uses standard Python library modules (`email`, `urllib.request`, `asyncio`, `argparse`) with only `python-dotenv` for configuration loading.
- **Reboot Detection**: Automatically alerts with `Reboot: ⚠️ Required` (and provides a handy `sudo reboot` hint) when `/var/run/reboot-required` exists or is indicated in package logs.
- **Multi-Server Friendly**: Automatically detects OS distribution and server hostname, with optional `SERVER_NAME` override.
- **Strict Security**:
  - Webhook URL token is never logged or printed in clear text.
  - Mentions are neutralized with `"allowed_mentions": {"parse": []}` to prevent email content from triggering `@everyone`, `@here`, or role pings.
- **Robust Charset & Multipart Handling**: Parses UTF-8, ISO-8859-1/Latin-1, Windows-1252, and multi-part messages; prefers `text/plain` and cleanly strips `text/html`.
- **Specialized Summary Formatting**: Automatically extracts package lists and error logs from `unattended-upgrades` emails.
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

## 2. Installation on Ubuntu 24 / Debian 12

### Step 1: Install in a dedicated virtual environment

```bash
sudo mkdir -p /opt/mail2hook /etc/mail2hook
sudo python3 -m venv /opt/mail2hook/venv
sudo /opt/mail2hook/venv/bin/pip install --upgrade pip
sudo /opt/mail2hook/venv/bin/pip install --upgrade git+https://github.com/heliophane/mail2hook.git
sudo ln -sf /opt/mail2hook/venv/bin/mail2hook /usr/local/bin/mail2hook
```

### Step 2: Configure `/etc/mail2hook/env`

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
# SERVER_NAME=bookstack

# Optional: Comma-separated sender addresses or domains to match. If unset, all senders match.
# FILTER_SENDERS=root@bookstack, unattended-upgrades

# Optional: Comma-separated subject keywords to match (case-insensitive). If unset, all subjects match.
# FILTER_SUBJECT_KEYWORDS=unattended-upgrades, apt-listchanges, cron

# Optional: Maximum body characters to include in Discord excerpt (default: 1500)
# MAX_BODY_CHARS=1500

# Optional: Bot display name in Discord (default: mail2hook)
# DISCORD_USERNAME=mail2hook
```

---

## 3. Cutting Out Postfix (Recommended)

In Linux, tools like `unattended-upgrades`, `cron`, and `apt-listchanges` do not talk to an SMTP daemon. They invoke `/usr/sbin/sendmail -t -oi` directly and write raw email to `stdin`.

By replacing `/usr/sbin/sendmail` with `mail2hook`, you can **purge Postfix completely** and eliminate unnecessary memory, open ports, and queue maintenance.

### Purge Postfix and symlink `mail2hook`:

```bash
# 1. Stop and remove Postfix
sudo systemctl stop postfix 2>/dev/null || true
sudo apt purge postfix -y

# 2. Symlink mail2hook as sendmail and mailq
sudo ln -sf /opt/mail2hook/venv/bin/mail2hook /usr/sbin/sendmail
sudo ln -sf /opt/mail2hook/venv/bin/mail2hook /usr/bin/mailq
```

### Configure `unattended-upgrades`:

Generate and install the drop-in configuration directly:

```bash
mail2hook --print-apt-config | sudo tee /etc/apt/apt.conf.d/52mail2hook
```

*(Using a dedicated `52mail2hook` drop-in file avoids modifying `/etc/apt/apt.conf.d/50unattended-upgrades`, preventing dpkg configuration merge prompts during future Debian/Ubuntu package upgrades).*

---

## 4. Alternative: Built-in SMTP Daemon (`127.0.0.1:25`)

If you have applications on your server that connect to `localhost:25` over TCP rather than invoking `sendmail`, you can run `mail2hook` as a systemd SMTP service without needing Postfix.

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

## 6. Testing & Verification

### Test sendmail drop-in directly:

```bash
printf "Subject: unattended-upgrades result for server: SUCCESS\nFrom: root <root@example.com>\n\nAll upgrades installed successfully.\nPackages that were upgraded:\n libssl3 openssl\n" | sendmail -t -oi root
```

### Test dry-run output (does not send to Discord):

```bash
printf "Subject: Test Notification\nFrom: root <root@example.com>\n\nTest message." | mail2hook --dry-run
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

---

## 7. Exit Code Reference

`mail2hook` conforms to standard `sysexits.h` exit codes:

| Exit Code | Name | Description |
| --- | --- | --- |
| `0` | `EX_OK` | Message successfully sent or filtered out. |
| `65` | `EX_DATAERR` | Unparseable email input or invalid data. |
| `75` | `EX_TEMPFAIL` | Discord rate limit exhausted, 5xx server error, network timeout (retry later). |
| `78` | `EX_CONFIG` | Missing or invalid configuration (e.g. invalid webhook URL). |

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
