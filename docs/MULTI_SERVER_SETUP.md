# Multi-Server Setup Guide: Ubuntu 24.04/26.04 & Debian 12/13

This guide covers deploying `mail2hook` across Debian and Ubuntu servers so system alerts (`unattended-upgrades`, `cron`, `apt-listchanges`) are delivered to Discord through the local `sendmail` interface without running a mail daemon.

---

## Architecture Overview

```text
unattended-upgrades / cron / apt-listchanges
                  │
                  ▼
     /usr/sbin/sendmail -t -oi
                  │  (update-alternatives: mta)
                  ▼
     /usr/local/bin/mail2hook
                  │  reads /etc/mail2hook/env (root:mail, 0640)
                  ▼
       HTTPS POST -> Discord Webhook API
```

`mail2hook` runs only when `sendmail` is invoked, posts the embed, and exits.

---

## Automated Setup (Recommended)

Run the installer on each server:

```bash
curl -fsSL https://raw.githubusercontent.com/heliophane/mail2hook/main/install.sh | sudo bash
```

To customize the server name shown in Discord embeds, set `SERVER_NAME` in `/etc/mail2hook/env`:

```ini
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/123456789012345678/example-token-placeholder
SERVER_NAME=prod-db01
```

---

## Existing MTAs

- **Postfix (e.g. Proxmox VE / mail-enabled servers)**: `install.sh` **never replaces `/usr/sbin/sendmail`**. It preserves Postfix's own alerts (`proxmox-mail-forward`, `root` mail) and configures `mail2hook` alongside Postfix so **only `unattended-upgrades` reports** go to Discord:
  - Pipe transport in `/etc/postfix/master.cf` (`flags=R user=nobody:mail argv=/usr/local/bin/mail2hook -oi -f ${sender} -- ${recipient}`)
  - Dedicated transport map `/etc/postfix/mail2hook_transport` (`mail2hook@localhost mail2hook:`) plus `mail2hook_destination_recipient_limit = 1` in `/etc/postfix/main.cf` (other `root` mail is never diverted)
  - Replaces `Unattended-Upgrade::Mail` with `"mail2hook@localhost"` (backing up the prior target and restoring it on `--uninstall`), with `FILTER_SUBJECT_KEYWORDS=unattended-upgrades` as defense in depth
  - Backups of any edited files stored in `/var/backups/mail2hook/` (never in `/etc/apt/apt.conf.d/`)
- **Other MTAs (`exim4`, `msmtp-mta`, `nullmailer`, `dma`)**: `install.sh` detects them, warns, and leaves `/usr/sbin/sendmail` unchanged. Either purge the existing MTA (`sudo apt purge 'exim4*'`) and re-run `install.sh`, or configure that MTA to pipe mail to `/usr/local/bin/mail2hook`.

---

## Manual Installation

1. **Install Python 3.11+**:
   ```bash
   sudo apt update && sudo apt install -y --no-install-recommends python3 ca-certificates curl unattended-upgrades
   ```
   *(On Debian 12/13, also include `lsb-release` for `unattended-upgrades`.)*

2. **Install `/usr/local/bin/mail2hook`**:
   Package `src/mail2hook` as a standalone zipapp executable (`0755`, `root:root`):
   ```bash
   python3 -m zipapp src -p "/usr/bin/python3 -sE" -m "mail2hook.cli:main" -o mail2hook.pyz
   sudo install -m 0755 -o root -g root mail2hook.pyz /usr/local/bin/mail2hook
   ```

3. **Connect `sendmail` or Postfix**:
   - **No existing MTA**: Register `sendmail` via `update-alternatives`:
     ```bash
     sudo update-alternatives --install /usr/sbin/sendmail mta /usr/local/bin/mail2hook 10 \
       --slave /usr/bin/mailq mta-mailq /usr/local/bin/mail2hook
     ```
   - **Existing Postfix**: Leave `/usr/sbin/sendmail` untouched and follow the Postfix pipe steps in [README.md](../README.md#interaction-with-an-existing-mta).

4. **Create `/etc/mail2hook/env`**:
   ```bash
   sudo install -d -m 0750 -o root -g mail /etc/mail2hook
   sudo install -m 0640 -o root -g mail /dev/null /etc/mail2hook/env
   ```
   Add `DISCORD_WEBHOOK_URL=...`, `FILTER_SUBJECT_KEYWORDS=unattended-upgrades` (to forward only upgrade reports), and optional `SERVER_NAME=...` to `/etc/mail2hook/env`.

5. **Configure `unattended-upgrades` and verify**:
   ```bash
   /usr/local/bin/mail2hook --print-apt-config | sudo tee /etc/apt/apt.conf.d/52-unattended-upgrades-local >/dev/null
   printf 'To: root\nFrom: root\nSubject: unattended-upgrades test\n\nHello\n' | sudo sendmail -oi -t
   sudo unattended-upgrade --dry-run --debug
   ```
   *(On Postfix hosts, set `Unattended-Upgrade::Mail "mail2hook@localhost";` and send the test to `To: mail2hook@localhost`.)*

---

## Checking Logs & Pending Reboots

- **Upgrade logs**:
  ```bash
  sudo tail -n 50 /var/log/unattended-upgrades/unattended-upgrades.log
  sudo tail -n 50 /var/log/unattended-upgrades/unattended-upgrades-dpkg.log
  ```
- **Pending reboot**:
  ```bash
  if [ -f /var/run/reboot-required ]; then
    cat /var/run/reboot-required
    cat /var/run/reboot-required.pkgs 2>/dev/null || true
  fi
  ```
