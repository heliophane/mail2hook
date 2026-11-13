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

If `postfix`, `exim4`, `msmtp-mta`, or `nullmailer` is already installed on a server:

- `install.sh` detects it, warns, and leaves `/usr/sbin/sendmail` unchanged.
- If you do not need local SMTP mail routing, remove the existing MTA (`sudo apt purge 'exim4*'` or `sudo apt purge postfix`) and re-run `install.sh` so `mail2hook` can register the `mta` alternative.
- If you need to keep Postfix, configure a pipe transport in `/etc/postfix/master.cf` pointing to `/usr/local/bin/mail2hook`.

---

## Manual Installation

1. **Install Python 3.11+**:
   ```bash
   sudo apt update && sudo apt install -y --no-install-recommends python3 ca-certificates curl unattended-upgrades
   ```
   *(On Debian 12/13, also include `lsb-release` for `unattended-upgrades`.)*

2. **Install `/usr/local/bin/mail2hook`**:
   Package `src/mail2hook` into `/usr/local/bin/mail2hook` (`0755`, `root:root`) or install with `pipx install --global .`.

3. **Register `sendmail` via `update-alternatives`** (when no other MTA owns `/usr/sbin/sendmail`):
   ```bash
   sudo update-alternatives --install /usr/sbin/sendmail mta /usr/local/bin/mail2hook 10 \
     --slave /usr/bin/mailq mta-mailq /usr/local/bin/mail2hook
   ```

4. **Create `/etc/mail2hook/env`**:
   ```bash
   sudo install -d -m 0750 -o root -g mail /etc/mail2hook
   sudo install -m 0640 -o root -g mail /dev/null /etc/mail2hook/env
   ```
   Add `DISCORD_WEBHOOK_URL=...` (and optional `SERVER_NAME=...`) to `/etc/mail2hook/env`.

5. **Verify**:
   ```bash
   echo "Subject: test" | sudo sendmail root
   sudo unattended-upgrade --dry-run --debug
   ```

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
