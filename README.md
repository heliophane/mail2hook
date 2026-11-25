# mail2hook

[![CI](https://github.com/heliophane/mail2hook/actions/workflows/ci.yml/badge.svg)](https://github.com/heliophane/mail2hook/actions)

A tiny, lightweight tool that links into the system `sendmail` interface and forwards local system mail (`unattended-upgrades`, `apt-listchanges`, `cron`, monitoring scripts) to a Discord webhook.

When a system tool invokes `sendmail`, `mail2hook` reads the message from `stdin`, formats a clean Discord embed, posts it to your webhook, and exits immediately.

---

## Design Goals

- **Lightweight**: Runs only when `sendmail` is called, delivers the message, and exits.
- **No daemon**: No background process to supervise or restart.
- **No listening port**: Nothing binds to port 25 or any network socket.
- **Minimal dependencies**: Pure Python 3.11+ standard library only (`email`, `urllib.request`, `json`, `argparse`, `html.parser`) with zero third-party runtime packages.

> [!NOTE]
> **Contributors**: Please keep `mail2hook` small and focused. Do not add background daemons, listening ports, dedicated service users, or heavy runtime dependencies.

---

## How It Hooks Into `sendmail`

Standard Linux tools (`unattended-upgrades`, `cron`, `apt-listchanges`) deliver local notifications by piping raw email to `/usr/sbin/sendmail`.

When no conflicting MTA is installed, `install.sh` installs `mail2hook` as a standalone executable at `/usr/local/bin/mail2hook` and registers it as the system `mta` alternative via `update-alternatives` (linking `/usr/sbin/sendmail` and `/usr/bin/mailq` to `/usr/local/bin/mail2hook`).

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
  3. Compile, configure `main.cf`, verify, and reload (using your Postfix `default_database_type`, typically `hash` or `lmdb`):
     ```bash
     DB_TYPE="$(postconf -h default_database_type)"
     sudo postmap "${DB_TYPE}:/etc/postfix/mail2hook_transport"
     sudo postconf -e "transport_maps = ${DB_TYPE}:/etc/postfix/mail2hook_transport"
     sudo postconf -e "mail2hook_destination_recipient_limit = 1"
     sudo postfix check && sudo systemctl reload postfix
     ```
     *(If Postfix already has a `transport_maps` entry, prepend `${DB_TYPE}:/etc/postfix/mail2hook_transport` to the existing list instead of overwriting it.)*

- **Hosts with another MTA (`exim4`, `msmtp-mta`, `nullmailer`, `dma`, etc.)**: `install.sh` detects it, warns, and leaves `/usr/sbin/sendmail` untouched. Either remove the conflicting MTA (`sudo apt purge 'exim4*'`) and re-run `install.sh`, or configure that MTA to pipe mail to `/usr/local/bin/mail2hook`.

---

## Discord Notifications

Each forwarded message is posted to Discord as a single clean embed with `"allowed_mentions": {"parse": []}` (disabling accidental `@everyone`/`@here`/user pings from untrusted email content).

### Status Colors (Left Bar)

| Color | Hex | When Used |
| :--- | :--- | :--- |
| **Green** | `#57F287` | Clean success (`SUCCESS`, `All upgrades installed`, or `Unattended upgrade returned: True`) with no reboot or warning needed. |
| **Yellow** | `#FEE75C` | Attention needed: a reboot is required (e.g. `[reboot required]` or `/run/reboot-required`), packages were kept back, or the subject contains a warning. |
| **Red** | `#ED4245` | Upgrade or job failure (`FAILURE`, `Some errors were encountered`, `Unattended upgrade returned: False`, `dpkg: error`, or failure/error subject keywords). Always overrides yellow and green. |
| **Blurple** | `#5865F2` | General informational emails without a success, warning, or failure status. |

### Embed Layout

- **Header & Footer**: Shows the effective server name and detected OS (`Server: <host> • <OS>`).
- **Description**: Clean plain-text excerpt (`MAX_BODY_CHARS`, default `1500`). For `unattended-upgrades`, packages are formatted vertically with `old → new` versions (falling back to local `/var/log/unattended-upgrades/unattended-upgrades-dpkg.log` or `/var/log/dpkg.log` when the email omits versions), and trailing verbose logs are stripped unless errors occurred.
- **Fields**: `Server`, `OS`, `Reboot` (`🟢 Not needed` or `⚠️ Required` with up to 2 package names + count), `Date` (when present), and `Server Logs` (with copy-paste log-inspection and `sudo reboot` commands). Raw `From` and `To` headers are intentionally omitted to keep alerts concise.

---

## Quick Start Installation

On a supported Debian or Ubuntu server, run:

```bash
curl -fsSL https://raw.githubusercontent.com/heliophane/mail2hook/main/install.sh | sudo bash
```

The installer:
1. Detects the OS and installs minimal dependencies (`python3`, `ca-certificates`, `curl`, and `unattended-upgrades`, plus `lsb-release` on Debian 12/13).
2. Installs `/usr/local/bin/mail2hook` (zipapp pinned to `/usr/bin/python3 -sE`) and either configures a dedicated Postfix pipe (`mail2hook@localhost`, leaving `/usr/sbin/sendmail` untouched) or links `/usr/sbin/sendmail` via `update-alternatives` on hosts without an MTA.
3. Prompts securely for your Discord webhook URL via `/dev/tty` (hidden input) and writes `/etc/mail2hook/env` (defaulting `FILTER_SUBJECT_KEYWORDS=unattended-upgrades` so non-upgrade mail such as `cron` or `apt-listchanges` is dropped silently with exit `0`).
4. Configures conservative, nightly **security-only** `unattended-upgrades` (`Mail "mail2hook@localhost"` with Postfix or `Mail "root"` with the `sendmail` shim, `MailReport "on-change"`, `MailOnlyOnError "false"`, `Automatic-Reboot "false"`, `Remove-Unused-Dependencies "false"`, `Remove-Unused-Kernel-Packages "false"`) and systemd timers (`01:00–02:00` download, `03:00–04:00` upgrade local time). No `mailx` package is needed because `unattended-upgrades` invokes `/usr/sbin/sendmail` directly.
5. Sends a real-path end-to-end test message through `/usr/sbin/sendmail` (confirming delivery to Discord and exiting non-zero with remediation steps if delivery cannot be confirmed) and reports all changes and backups (saved in `/var/backups/mail2hook/`).

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

- **Path**: `/etc/mail2hook/env` (when absent and running as non-root for local development, `./.env` may be used as a fallback; `./.env` is ignored when running as `root` or when `/etc/mail2hook/env` exists)
- **Ownership & Mode**: `root:mail` (`0640`), inside `/etc/mail2hook` (`root:mail`, `0750`)

This permission model allows `root` callers (`unattended-upgrades`, `apt-listchanges`, root `cron`) and any local service account explicitly added to the standard `mail` group (`sudo usermod -aG mail <user>`) to invoke `sendmail`, while keeping the webhook secret unreadable by unprivileged local users.

Supported variables in `/etc/mail2hook/env` (process environment variables take precedence over values in the env file):

```ini
# Required: Discord webhook URL (discord.com, discordapp.com, ptb.discord.com, or canary.discord.com)
# Aliases accepted: DISCORD_WEBHOOK, WEBHOOK_URL
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/123456789012345678/example-token-placeholder

# Optional: Friendly server name shown in Discord
# Precedence: SERVER_NAME (or HOSTNAME_OVERRIDE) > hostname parsed from email > socket.gethostname
# SERVER_NAME=web01

# Optional: Comma-separated sender addresses, domains, usernames, or display names to match (unset = match all)
# FILTER_SENDERS=

# Optional: Comma-separated subject keywords to match, case-insensitive.
# install.sh defaults this to unattended-upgrades; set empty to match all subjects.
# When both FILTER_SENDERS and FILTER_SUBJECT_KEYWORDS are set, both must match.
FILTER_SUBJECT_KEYWORDS=unattended-upgrades

# Optional: Maximum body characters in the Discord embed excerpt (default: 1500, positive integer)
# MAX_BODY_CHARS=1500

# Optional: Bot display name in Discord (default: mail2hook; max 80 chars, cannot contain
# 'discord', 'clyde', or backticks, and cannot be 'everyone' or 'here')
# DISCORD_USERNAME=mail2hook
```

Never pass the webhook URL on a command line.

---

## Testing & Diagnostics

### Run built-in diagnostics (`--doctor`)

Inspect `sendmail` ownership, Postfix pipe configuration, env file permissions, redacted webhook ID, APT settings, and run a sample dry-run check:

```bash
sudo mail2hook --doctor
```

To also send a clearly labeled diagnostic test embed to Discord:

```bash
sudo mail2hook --doctor --send-test
```

### Send a test message through `sendmail`

```bash
printf 'To: root\nFrom: root\nSubject: unattended-upgrades test on %s\n\nHello from mail2hook\n' "$(hostname)" | sudo sendmail -oi -t
```

On Postfix hosts configured with the `mail2hook` pipe transport, address tests to `mail2hook@localhost`:

```bash
printf 'To: mail2hook@localhost\nFrom: root\nSubject: unattended-upgrades test on %s\n\nHello via Postfix\n' "$(hostname)" | sudo sendmail -oi -t
```

Or test a simulated `unattended-upgrades` message (use `To: mail2hook@localhost` on Postfix hosts):

```bash
printf 'To: root\nFrom: root\nSubject: unattended-upgrades result for %s: SUCCESS\n\nAll upgrades installed.\nPackages that were upgraded:\n libssl3 openssl\n' "$(hostname)" | sudo sendmail -t -oi
```

### Dry-run locally & inspect logs

Print the Discord JSON payload without sending (prints a notice to `stderr` if filtered out; `-v` logs filter drops too):

```bash
printf 'Subject: unattended-upgrades test\nFrom: root\n\nHello\n' | sudo mail2hook --dry-run
```

`mail2hook` logs delivery outcomes (`posted`, `filtered`, `delivery_failed`, `parse_error`, `config_error`) to syslog `LOG_MAIL` without logging secrets or email bodies:

```bash
sudo journalctl -t mail2hook -n 50 --no-pager
```

---

## CLI Flags & Exit Codes

### `mail2hook` Flags

- `--file PATH`: Read raw email from a file instead of `stdin` (max 10 MiB).
- `--dry-run`: Print the Discord JSON payload to `stdout` without sending (or print a filter-drop notice to `stderr`).
- `--env-file PATH`: Load configuration from a custom env file (fails with exit code `78` if missing or unreadable).
- `--print-apt-config`: Print the recommended `/etc/apt/apt.conf.d/52mail2hook` snippet and exit.
- `--doctor`: Print installation diagnostics (sendmail owner, Postfix route, env readability, redacted webhook ID, APT mail settings, installed MTAs, and sample dry-run) without exposing secrets.
- `--send-test`: With `--doctor`, post a clearly labeled diagnostic test message to Discord.
- `--version`, `-V`: Show version and exit.
- Sendmail compatibility flags: `-t`, `-i`, `-oi`, `-bm`, `-bp`, `-bv`, `-bs`, `-f`, `-r`, `-F`, `-B`, `-v`, `-o...`, and positional recipient arguments.

### Exit Codes (`sysexits.h`)

| Exit Code | Name | Meaning |
| :--- | :--- | :--- |
| `0` | `EX_OK` | Message delivered or silently filtered out (`-bp`/`mailq`, `-bv`, `--print-apt-config`, `--doctor` OK). |
| `65` | `EX_DATAERR` | Empty/unparseable email, missing `--file`, message over 10 MiB, or Discord `400` rejection. |
| `75` | `EX_TEMPFAIL` | Discord `429` rate limit exhausted, `408`/`5xx` error, Cloudflare HTML `403`, unexpected `3xx` redirect, or network failure. |
| `78` | `EX_CONFIG` | Missing/invalid config, invalid CLI usage (`--send-test` without `--doctor`), or unauthorized/deleted Discord webhook (`401`, JSON `403`, `404`). |

---

## Checking `unattended-upgrades` Logs & Pending Reboots

### View `unattended-upgrades` logs

```bash
sudo tail -n 50 /var/log/unattended-upgrades/unattended-upgrades.log
sudo tail -n 50 /var/log/unattended-upgrades/unattended-upgrades-dpkg.log
```

### Check if a reboot is pending

`Automatic-Reboot` is set to `"false"` by default so servers never reboot unexpectedly. When an update requires a restart, `mail2hook` turns the embed bar **yellow** and highlights `Reboot: ⚠️ Required` with a `sudo reboot` reminder in Discord. Check manually on the server with:

```bash
if [ -f /var/run/reboot-required ]; then
  cat /var/run/reboot-required
  cat /var/run/reboot-required.pkgs 2>/dev/null || true
fi
```

---

## Updating & Uninstalling

### Update

When piped via `curl`, `install.sh` fetches the latest GitHub release tag (falling back to `main` if no release tag is available); when run from a local git clone, it packages that checkout directly. Re-running it is idempotent and preserves your existing `/etc/mail2hook/env` configuration:

```bash
curl -fsSL https://raw.githubusercontent.com/heliophane/mail2hook/main/install.sh | sudo bash -s -- --yes
```

### Uninstall

Run directly from a checkout or remotely via `curl`:

```bash
sudo ./install.sh --uninstall
# or remotely:
curl -fsSL https://raw.githubusercontent.com/heliophane/mail2hook/main/install.sh | sudo bash -s -- --uninstall
```

This removes `/usr/local/bin/mail2hook`, cleanly removes any Postfix pipe/transport additions (`main.cf`, `master.cf`, `/etc/postfix/mail2hook_transport*`), restores the original `Unattended-Upgrade::Mail` target in `/etc/apt/apt.conf.d/52-unattended-upgrades-local`, removes the `mta` alternative if linked, and prompts before deleting `/etc/mail2hook`. Backups of modified configs are preserved under `/var/backups/mail2hook/`. Pass `--purge-upgrades` as well if you also want to remove the `unattended-upgrades` drop-in files and timer overrides.

---

## Development

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

ruff check .
ruff format --check .
pytest
shellcheck install.sh
```

## License

MIT
