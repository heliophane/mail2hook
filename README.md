# mail2hook

[![CI](https://github.com/example/mail2hook/actions/workflows/ci.yml/badge.svg)](https://github.com/example/mail2hook/actions)

A lightweight Python CLI that receives raw email messages piped directly from Postfix (or any MTA), parses the message, and forwards a formatted summary to a Discord channel via webhook.

Specifically designed for automated system notifications such as:
- **`unattended-upgrades`** (package installation results, security updates, failure warnings)
- **`apt-listchanges`** (changelogs and critical package alerts)
- **`cron`** / systemd timer output

---

## Features

- **Near-Zero Dependencies**: Uses Python standard library modules (`email`, `urllib.request`, `html.parser`, `argparse`) with only `python-dotenv` for configuration loading.
- **Postfix Pipe Compatible**: Reads raw email from `sys.stdin.buffer` and exits with standard sysexits exit codes (`0` success/filtered, `75` temporary failure for Postfix retry, `65`/`78` permanent failure).
- **Strict Security**:
  - Webhook URL token is never logged or printed in clear text.
  - Mentions are neutralized with `"allowed_mentions": {"parse": []}` to prevent email content from triggering `@everyone`, `@here`, or role pings.
- **Robust Charset & Multipart Handling**: Parses UTF-8, ISO-8859-1/Latin-1, Windows-1252, and multi-part messages; prefers `text/plain` and cleanly strips `text/html`.
- **Specialized Summary Formatting**: Automatically detects and extracts package lists and error logs from `unattended-upgrades` emails.
- **Rate Limit & Transient Retry**: Automatically handles Discord HTTP `429` (honoring `Retry-After`) and transient `5xx` / network failures with exponential backoff before signaling Postfix to retry.
- **Optional Filtering**: Drop unrelated emails silently by matching sender addresses/domains or subject keywords.

---

## Requirements

- Linux server (Debian 12 Bookworm is the primary target; compatible with Ubuntu 22.04+, RHEL/Rocky 9+, etc.)
- Python 3.11+

---

## 1. Creating a Discord Webhook

1. Open Discord and go to **Server Settings** > **Integrations** > **Webhooks** (or open the settings for a specific channel > **Integrations**).
2. Click **New Webhook**.
3. Choose a name (e.g. `Server Upgrades`) and select the target channel.
4. Click **Copy Webhook URL**. The URL looks like:
   ```text
   https://discord.com/api/webhooks/123456789012345678/example-token-placeholder
   ```

---

## 2. Installation on Debian 12

### Option A: Dedicated Virtual Environment in `/opt/mail2hook` (Recommended)

1. Create a dedicated system user and group for mail2hook:
   ```bash
   sudo useradd --system --no-create-home --user-group --shell /usr/sbin/nologin mail2hook
   ```

2. Create the directory and python virtual environment:
   ```bash
   sudo mkdir -p /opt/mail2hook
   sudo python3 -m venv /opt/mail2hook/venv
   sudo /opt/mail2hook/venv/bin/pip install --upgrade pip
   ```

3. Install `mail2hook`:
   ```bash
   # From git repository
   sudo /opt/mail2hook/venv/bin/pip install git+https://github.com/example/mail2hook.git

   # Or if deploying from source directory
   sudo /opt/mail2hook/venv/bin/pip install /path/to/mail2hook
   ```

4. Symlink the executable into `/usr/local/bin`:
   ```bash
   sudo ln -sf /opt/mail2hook/venv/bin/mail2hook /usr/local/bin/mail2hook
   ```

### Option B: Using `pipx`

If `pipx` is installed on your Debian system (`apt install pipx`):

```bash
sudo pipx install --global git+https://github.com/example/mail2hook.git
```

---

## 3. Configuration (`/etc/mail2hook/env`)

Create the configuration directory and secure environment file:

```bash
sudo mkdir -p /etc/mail2hook
sudo touch /etc/mail2hook/env
sudo chown root:mail2hook /etc/mail2hook/env
sudo chmod 0640 /etc/mail2hook/env
```

Edit `/etc/mail2hook/env` with your settings:

```ini
# Required: Discord webhook URL
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/123456789012345678/example-token-placeholder

# Optional: Comma-separated sender addresses or domains to match. If unset, all senders match.
FILTER_SENDERS=root@server01.example.com, unattended-upgrades@example.com

# Optional: Comma-separated subject keywords to match (case-insensitive). If unset, all subjects match.
FILTER_SUBJECT_KEYWORDS=unattended-upgrades, apt-listchanges, cron

# Optional: Maximum body characters to include in Discord excerpt (default: 1500)
MAX_BODY_CHARS=1500

# Optional: Bot display name in Discord (default: mail2hook)
DISCORD_USERNAME=mail2hook
```

---

## 4. Postfix Configuration

Postfix can deliver email to `mail2hook` using either a dedicated `pipe` transport (recommended for security) or an alias.

### Method A: Dedicated `pipe` Transport in `master.cf` (Recommended)

This method ensures the process runs strictly as the unprivileged `mail2hook` user.

1. Add the transport definition to `/etc/postfix/master.cf`:
   ```text
   mail2hook unix  -       n       n       -       -       pipe
     flags=FR user=mail2hook argv=/usr/local/bin/mail2hook
   ```

2. Configure a transport map in `/etc/postfix/transport`:
   ```text
   mail2hook@example.com    mail2hook:
   ```
   *(Replace `example.com` with your server's domain or hostname)*

3. Compile the transport database and configure Postfix:
   ```bash
   sudo postmap /etc/postfix/transport
   sudo postconf -e "transport_maps = hash:/etc/postfix/transport"
   sudo systemctl reload postfix
   ```

### Method B: Alias in `/etc/aliases`

Alternatively, add an alias to `/etc/aliases`:

```text
mail2hook: "|/usr/local/bin/mail2hook"
```

Then rebuild the alias database:
```bash
sudo newaliases
```

*(Note: Postfix executes aliases using the default pipe user configured in `default_privs`, typically `nobody`.)*

---

## 5. Unattended-Upgrades Configuration

To forward automated update reports to Discord, you can automatically install the recommended drop-in configuration using `mail2hook`:

### Option A: Install Modular Drop-in Configuration (Recommended)

Generate and install the drop-in configuration directly with one command:

```bash
mail2hook --print-apt-config | sudo tee /etc/apt/apt.conf.d/52mail2hook
```

This creates `/etc/apt/apt.conf.d/52mail2hook`:
```apt
// /etc/apt/apt.conf.d/52mail2hook
// Default unattended-upgrades configuration for mail2hook

APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
APT::Periodic::AutocleanInterval "7";

// Send reports to mail2hook
Unattended-Upgrade::Mail "mail2hook";

// "always" sends Discord notifications for every run; "only-on-error" alerts only on failure
Unattended-Upgrade::MailReport "always";

Unattended-Upgrade::Sender "unattended-upgrades";
```

*(Using a dedicated `52mail2hook` drop-in file avoids modifying `/etc/apt/apt.conf.d/50unattended-upgrades`, preventing dpkg configuration merge prompts during future Debian/Ubuntu package upgrades).*

### Option B: Edit `/etc/apt/apt.conf.d/50unattended-upgrades` Manually

Alternatively, edit `/etc/apt/apt.conf.d/50unattended-upgrades` directly:

```apt
Unattended-Upgrade::Mail "mail2hook";
Unattended-Upgrade::MailReport "always";
```

---

## 6. CLI Usage & Testing

### Testing with a sample email file:

```bash
# Preview what would be sent to Discord (does not make network calls)
mail2hook --file /path/to/sample.eml --dry-run

# Send a test email file to Discord
mail2hook --file /path/to/sample.eml
```

### Testing with stdin pipe:

```bash
cat /path/to/sample.eml | mail2hook
```

### Testing live through Postfix `sendmail`:

```bash
printf "Subject: Test Notification\nFrom: root@server01.example.com\n\nThis is a test notification from mail2hook." | sendmail mail2hook@example.com
```

### Command Flags:

- `--file PATH`, `-f PATH`: Read raw email from a file instead of stdin.
- `--dry-run`: Parse and format payload, printing JSON to stdout without sending to Discord.
- `--env-file PATH`: Use a specific environment file instead of default locations.
- `--print-apt-config`: Print recommended `/etc/apt/apt.conf.d/52mail2hook` configuration and exit.
- `--version`, `-v`: Display version information and exit.

---

## 7. Exit Code Reference

Postfix relies on standard exit codes (`sysexits.h`) to handle delivery status:

| Exit Code | Name | Description | Postfix Action |
| --- | --- | --- | --- |
| `0` | `EX_OK` | Message successfully sent or filtered out | Message accepted; considered delivered. |
| `65` | `EX_DATAERR` | Unparseable email input or invalid data | Permanent error; message bounced. |
| `75` | `EX_TEMPFAIL` | Discord rate limit exhausted, 5xx server error, network timeout | Temporary failure; Postfix retries delivery later. |
| `78` | `EX_CONFIG` | Missing or invalid configuration (e.g. malformed webhook URL) | Permanent error; message bounced with stderr reason. |

---

## 8. Development & Testing

Run tests and code formatting locally:

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
