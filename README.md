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

- **Hosts with Postfix** (e.g. Proxmox VE or mail-enabled Debian/Ubuntu hosts): `install.sh` **leaves `/usr/sbin/sendmail` untouched** so existing root/admin mail forwarding continues working. Instead, it configures `mail2hook` alongside Postfix so **only `unattended-upgrades` reports** go to Discord:
  - Adds a managed `mail2hook` pipe transport to `/etc/postfix/master.cf` (`flags=R user=nobody:mail argv=/usr/local/bin/mail2hook -oi -f ${sender} -- ${recipient}`).
  - Creates `/etc/postfix/mail2hook_transport` (`mail2hook@localhost mail2hook:`), compiles it with `postmap`, prepends it to `transport_maps`, and sets `mail2hook_destination_recipient_limit = 1` in `/etc/postfix/main.cf`. Other `root` mail is never captured or diverted.
  - Replaces `Unattended-Upgrade::Mail` with `"mail2hook@localhost"` (backing up the previous target in `/var/backups/mail2hook/` and restoring it on `--uninstall`).
  - Sets `FILTER_SUBJECT_KEYWORDS=unattended-upgrades` in `/etc/mail2hook/env` (if unset) as defense in depth.
  - Saves backups of modified files in `/var/backups/mail2hook/`.

  Manual Postfix pipe setup (if configuring by hand):
  1. Add to `/etc/postfix/master.cf`:
     ```text
     mail2hook unix  -       n       n       -       -       pipe
       flags=R user=nobody:mail argv=/usr/local/bin/mail2hook -oi -f ${sender} -- ${recipient}
     ```
  2. Create `/etc/postfix/mail2hook_transport`:
     ```text
     mail2hook@localhost mail2hook:
     mail2hook@localhost.localdomain mail2hook:
     ```
  3. Compile, configure `main.cf`, verify, and reload:
     ```bash
     sudo postmap /etc/postfix/mail2hook_transport
     sudo postconf -e "transport_maps = hash:/etc/postfix/mail2hook_transport"
     sudo postconf -e "mail2hook_destination_recipient_limit = 1"
     sudo postfix check && sudo systemctl reload postfix
     ```
     *(If Postfix already has a `transport_maps` entry, prepend `hash:/etc/postfix/mail2hook_transport` or `lmdb:/etc/postfix/mail2hook_transport` matching `postconf -h default_database_type`.)*

- **Hosts with another MTA (`exim4`, `msmtp-mta`, `nullmailer`, `dma`, etc.)**: `install.sh` detects it, warns, and leaves `/usr/sbin/sendmail` untouched. Either remove the conflicting MTA (`sudo apt purge 'exim4*'`) and re-run `install.sh`, or configure that MTA to pipe mail to `/usr/local/bin/mail2hook`.

---

## Quick Start Installation

On a supported Debian or Ubuntu server, run:

```bash
curl -fsSL https://raw.githubusercontent.com/heliophane/mail2hook/main/install.sh | sudo bash
```

The installer:
1. Detects the OS and installs minimal dependencies (`python3`, `ca-certificates`, `curl`, and `unattended-upgrades`).
2. Installs `/usr/local/bin/mail2hook` (zipapp pinned to `/usr/bin/python3 -sE`) and either configures a dedicated Postfix pipe (`mail2hook@localhost`, leaving `/usr/sbin/sendmail` untouched) or links `/usr/sbin/sendmail` via `update-alternatives` on hosts without an MTA.
3. Prompts securely for your Discord webhook URL via `/dev/tty` (hidden input) and writes `/etc/mail2hook/env` (defaulting `FILTER_SUBJECT_KEYWORDS=unattended-upgrades` so non-upgrade mail such as `cron` or `apt-listchanges` is dropped silently with exit `0`).
4. Configures conservative, nightly **security-only** `unattended-upgrades` (`Mail "mail2hook@localhost"` with Postfix or `Mail "root"` with the `sendmail` shim, `MailReport "on-change"`, `MailOnlyOnError "false"`, `Automatic-Reboot "false"`). No `mailx` package is needed because `unattended-upgrades` invokes `/usr/sbin/sendmail` directly.
5. Sends an end-to-end test message and reports all changes and backups (saved in `/var/backups/mail2hook/`).

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
| `--skip-upgrades` | Install `mail2hook` only; skip installing and configuring `unattended-upgrades`. |
| `--yes`, `-y` | Non-interactive mode; read `DISCORD_WEBHOOK_URL` from the environment or keep existing valid `/etc/mail2hook/env`, and accept defaults without prompting. |
| `--uninstall` | Remove `/usr/local/bin/mail2hook`, cleanly remove any Postfix pipe/transport additions and `sendmail`/`mailq` alternative, and prompt before deleting `/etc/mail2hook`. |
| `--purge-upgrades` | Used with `--uninstall` to also remove the `unattended-upgrades` drop-ins and timer overrides. |
| `--help`, `-h` | Show usage information and exit. |

Non-interactive example (pass the secret via environment variable, never CLI arguments):

```bash
export DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/123456789012345678/example-token-placeholder"
curl -fsSL https://raw.githubusercontent.com/heliophane/mail2hook/main/install.sh \
  | sudo --preserve-env=DISCORD_WEBHOOK_URL bash -s -- --yes
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

# Optional: Comma-separated subject keywords to match, case-insensitive.
# install.sh defaults this to unattended-upgrades; set empty to match all subjects.
FILTER_SUBJECT_KEYWORDS=unattended-upgrades

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
printf 'To: root\nFrom: root\nSubject: unattended-upgrades test on %s\n\nHello from mail2hook\n' "$(hostname)" | sudo sendmail -oi -t
```

On Postfix hosts configured with the `mail2hook` pipe transport, address the test to `mail2hook@localhost`:

```bash
printf 'To: mail2hook@localhost\nFrom: root\nSubject: unattended-upgrades test on %s\n\nHello via Postfix\n' "$(hostname)" | sudo sendmail -oi -t
```

Or test a simulated `unattended-upgrades` message:

```bash
printf 'To: root\nFrom: root\nSubject: unattended-upgrades result for %s: SUCCESS\n\nAll upgrades installed.\nPackages that were upgraded:\n libssl3 openssl\n' "$(hostname)" | sudo sendmail -t -oi
```

### Dry-run locally without calling Discord

```bash
printf 'Subject: unattended-upgrades test\nFrom: root\n\nHello\n' | sudo mail2hook --dry-run
```

---

## CLI Flags & Exit Codes

### `mail2hook` Flags

- `--file PATH`: Read raw email from a file instead of `stdin`.
- `--dry-run`: Print the Discord JSON payload to `stdout` without sending.
- `--env-file PATH`: Use a custom env file instead of `/etc/mail2hook/env` or `.env`.
- `--print-apt-config`: Print the recommended `/etc/apt/apt.conf.d/52mail2hook` snippet and exit.
- `--doctor`: Print installation diagnostics (sendmail owner, Postfix route, env readability, redacted webhook ID, APT mail settings, installed MTAs, and sample dry-run) without exposing secrets.
- `--doctor --send-test`: Run diagnostics and post a clearly labeled test message to Discord.
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

This removes `/usr/local/bin/mail2hook`, cleanly removes any Postfix pipe/transport additions (`main.cf`, `master.cf`, `/etc/postfix/mail2hook_transport*`), restores the original `Unattended-Upgrade::Mail` target in `52-unattended-upgrades-local`, removes the `mta` alternative if linked, and asks before deleting `/etc/mail2hook/env`. Backups of modified configs are preserved under `/var/backups/mail2hook/`. Pass `--purge-upgrades` as well if you also want to remove the `unattended-upgrades` drop-in files and timer overrides.

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
