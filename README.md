# mail2hook

[![CI](https://github.com/heliophane/mail2hook/actions/workflows/ci.yml/badge.svg)](https://github.com/heliophane/mail2hook/actions)

A tiny, lightweight tool that links into the system `sendmail` interface and forwards local system mail (`unattended-upgrades`, `apt-listchanges`, `cron`, monitoring scripts) to a Discord webhook.

When a system tool invokes `sendmail`, `mail2hook` reads the message from `stdin`, formats a clean Discord embed, posts it to your webhook, and exits immediately.

---

## Design Goals

- **Lightweight**: Runs only when `sendmail` is called, delivers the message, and exits.
- **No daemon**: No background process to supervise or restart.
- **No listening port**: Nothing binds to port 25 or any network socket.
- **Minimal dependencies**: Pure Python 3.11+ standard library only (`email`, `urllib.request`, `json`, `argparse`) with zero third-party runtime packages.

> [!NOTE]
> **Contributors**: Please keep `mail2hook` small and focused. Do not add background daemons, listening ports, dedicated service users, or heavy runtime dependencies.

---

## How It Hooks Into `sendmail`

Standard Linux tools (`unattended-upgrades`, `cron`, `apt-listchanges`) deliver local notifications by piping raw email to `/usr/sbin/sendmail`.

`install.sh` installs `mail2hook` as a standalone executable at `/usr/local/bin/mail2hook` and registers it as the system `mta` alternative via `update-alternatives` (linking `/usr/sbin/sendmail` and `/usr/bin/mailq` to `/usr/local/bin/mail2hook`).

### Interaction With an Existing MTA

If another Mail Transfer Agent (`postfix`, `exim4`, `msmtp`, `nullmailer`, etc.) is already installed or owns `/usr/sbin/sendmail`, `install.sh` **detects it, warns you, and leaves `/usr/sbin/sendmail` untouched** so it never silently breaks existing mail routing.

You can then choose either option:
- **Use `mail2hook` directly as `sendmail`**: Remove the existing MTA package (e.g. `sudo apt purge postfix` or `sudo apt purge 'exim4*'`) and re-run `install.sh`.
- **Keep your existing MTA (e.g. Postfix)**: Configure a pipe transport in `/etc/postfix/master.cf` pointing to `/usr/local/bin/mail2hook`:
  ```text
  mail2hook unix  -       n       n       -       -       pipe
    flags=FR user=nobody argv=/usr/local/bin/mail2hook
  ```
  and route target addresses (e.g. `mail2hook@localhost mail2hook:`) via `/etc/postfix/transport`.

---

## Quick Start Installation

On a supported Debian or Ubuntu server, run:

```bash
curl -fsSL https://raw.githubusercontent.com/heliophane/mail2hook/main/install.sh | sudo bash
```

The installer:
1. Detects the OS and installs minimal dependencies (`python3`, `ca-certificates`, `curl`, and `unattended-upgrades`).
2. Installs `/usr/local/bin/mail2hook` and links `/usr/sbin/sendmail` via `update-alternatives` (unless another MTA owns it).
3. Prompts securely for your Discord webhook URL via `/dev/tty` (hidden input) and writes `/etc/mail2hook/env`.
4. Configures conservative, nightly **security-only** `unattended-upgrades` (`Automatic-Reboot "false"`).
5. Sends an end-to-end test message through `sendmail` and reports the result.

### Supported Operating Systems

| OS | Codename | Default Python | Security Origin Configuration |
| :--- | :--- | :--- | :--- |
| **Debian 12** | `bookworm` | Python 3.11 | `Unattended-Upgrade::Origins-Pattern` (`label=Debian-Security`) |
| **Debian 13** | `trixie` | Python 3.13 | `Unattended-Upgrade::Origins-Pattern` (`label=Debian-Security`) |
| **Ubuntu 24.04 LTS** | `noble` | Python 3.12 | `Unattended-Upgrade::Allowed-Origins` (`${distro_id}:${distro_codename}-security`) |
| **Ubuntu 26.04 LTS** | `resolute` | Python 3.14 | `Unattended-Upgrade::Allowed-Origins` (`${distro_id}:${distro_codename}-security`) |

### Installer Flags

| Flag | Description |
| :--- | :--- |
| `--webhook-url <url>` | Set the Discord webhook URL non-interactively (also reads `DISCORD_WEBHOOK_URL` from the environment). |
| `--skip-upgrades` | Install `mail2hook` only; skip installing and configuring `unattended-upgrades`. |
| `--yes`, `-y` | Non-interactive mode; keep existing valid config and accept defaults without prompting. |
| `--uninstall` | Remove `/usr/local/bin/mail2hook` and its `sendmail`/`mailq` alternative (restoring any prior alternative), and prompt before deleting `/etc/mail2hook`. |
| `--purge-upgrades` | Used with `--uninstall` to also remove the `unattended-upgrades` drop-ins and timer overrides. |
| `--help`, `-h` | Show usage information and exit. |

Non-interactive example:

```bash
curl -fsSL https://raw.githubusercontent.com/heliophane/mail2hook/main/install.sh | sudo bash -s -- \
  --webhook-url "https://discord.com/api/webhooks/123456789012345678/example-token-placeholder" \
  --yes
```

For multi-server deployment details and manual installation steps, see [docs/MULTI_SERVER_SETUP.md](docs/MULTI_SERVER_SETUP.md).

---

## Webhook Configuration & Permissions

Configuration lives in a single environment file:

- **Path**: `/etc/mail2hook/env`
- **Ownership & Mode**: `root:mail` (`0640`), inside `/etc/mail2hook` (`root:mail`, `0750`)

This permission model allows `root` callers (`unattended-upgrades`, `apt-listchanges`, root `cron`) and any local service account explicitly added to the standard `mail` group (`sudo usermod -aG mail <user>`) to invoke `sendmail`, while keeping the webhook secret unreadable by unprivileged local users.

Supported variables in `/etc/mail2hook/env`:

```ini
# Required: Discord webhook URL (discord.com or discordapp.com)
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/123456789012345678/example-token-placeholder

# Optional: Friendly server name shown in Discord (defaults to system hostname)
# SERVER_NAME=web01

# Optional: Comma-separated sender addresses or domains to match (unset = match all)
# FILTER_SENDERS=

# Optional: Comma-separated subject keywords to match, case-insensitive (unset = match all)
# FILTER_SUBJECT_KEYWORDS=

# Optional: Maximum body characters in the Discord embed excerpt (default: 1500)
# MAX_BODY_CHARS=1500

# Optional: Bot display name in Discord (default: mail2hook)
# DISCORD_USERNAME=mail2hook
```

Never pass the webhook URL on a command line.

---

## Testing

### Send a test message through `sendmail`

```bash
echo "Subject: test" | sudo sendmail root
```

Or test a simulated `unattended-upgrades` message:

```bash
printf 'Subject: unattended-upgrades result for %s: SUCCESS\nFrom: root\n\nAll upgrades installed.\nPackages that were upgraded:\n libssl3 openssl\n' "$(hostname)" | sudo sendmail -t -oi root
```

### Dry-run locally without calling Discord

```bash
printf 'Subject: Test\nFrom: root\n\nHello\n' | sudo mail2hook --dry-run
```

---

## CLI Flags & Exit Codes

### `mail2hook` Flags

- `--file PATH`: Read raw email from a file instead of `stdin`.
- `--dry-run`: Print the Discord JSON payload to `stdout` without sending.
- `--env-file PATH`: Use a custom env file instead of `/etc/mail2hook/env` or `.env`.
- `--print-apt-config`: Print the recommended `/etc/apt/apt.conf.d/52mail2hook` snippet and exit.
- `--version`, `-V`: Show version and exit.
- Sendmail compatibility flags: `-t`, `-i`, `-oi`, `-bm`, `-bp`, `-bv`, `-bs`, `-f`, `-r`, `-F`, `-B`, `-v`, `-o...`, and positional recipient arguments.

### Exit Codes (`sysexits.h`)

| Exit Code | Name | Meaning |
| :--- | :--- | :--- |
| `0` | `EX_OK` | Message delivered or silently filtered out. |
| `65` | `EX_DATAERR` | Empty/unparseable email, oversized message, or Discord `400` rejection. |
| `75` | `EX_TEMPFAIL` | Discord `429` rate limit exhausted, `5xx` error, or network failure. |
| `78` | `EX_CONFIG` | Missing/invalid config or unauthorized/deleted webhook (`401`/`403`/`404`). |

---

## Checking `unattended-upgrades` Logs & Pending Reboots

### View `unattended-upgrades` logs

```bash
sudo tail -n 50 /var/log/unattended-upgrades/unattended-upgrades.log
sudo tail -n 50 /var/log/unattended-upgrades/unattended-upgrades-dpkg.log
```

### Check if a reboot is pending

`Automatic-Reboot` is set to `"false"` by default so servers never reboot unexpectedly. When an update requires a restart, `mail2hook` highlights `Reboot: ⚠️ Required` in Discord. Check manually on the server with:

```bash
if [ -f /var/run/reboot-required ]; then
  cat /var/run/reboot-required
  cat /var/run/reboot-required.pkgs 2>/dev/null || true
fi
```

---

## Updating & Uninstalling

### Update

Re-run the installer (it is idempotent and preserves your existing `/etc/mail2hook/env` configuration):

```bash
curl -fsSL https://raw.githubusercontent.com/heliophane/mail2hook/main/install.sh | sudo bash -s -- --yes
```

### Uninstall

```bash
sudo ./install.sh --uninstall
```

This removes `/usr/local/bin/mail2hook`, cleans up its `mta` alternative (restoring any prior alternative), and asks before deleting `/etc/mail2hook/env`. Pass `--purge-upgrades` as well if you also want to remove the `unattended-upgrades` drop-in files and timer overrides.

---

## Development

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

ruff check .
ruff format --check .
pytest
```

## License

MIT
