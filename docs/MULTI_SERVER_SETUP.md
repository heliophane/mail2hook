# Multi-Server Setup Guide: Ubuntu 24, Ubuntu 26, Debian 12, Debian 13

This guide provides step-by-step instructions for deploying `mail2hook` across Ubuntu and Debian servers to receive system alerts (including `unattended-upgrades`, `cron`, and `apt-listchanges`) directly in Discord without running Postfix, Exim, or any other traditional Mail Transfer Agent (MTA).

---

## Architecture Overview

```
                      ┌────────────────────────────────────────┐
                      │ Ubuntu / Debian Server                 │
                      │                                        │
                      │  unattended-upgrades / root cron       │
                      │               │                        │
                      │               ▼                        │
                      │      /usr/sbin/sendmail -t -oi         │
                      │               │ (symlink)              │
                      │               ▼                        │
                      │   /opt/mail2hook/venv/bin/mail2hook    │
                      │               │                        │
                      │               ▼ Reads /etc/mail2hook/env
                      │          HTTPS POST                    │
                      └───────────────┼────────────────────────┘
                                      │
                                      ▼
                           Discord Webhook API
```

---

## Distribution Quick Reference

| Distribution | Python | Pre-installed / Conflicting MTA | Clean Up Command |
| :--- | :--- | :--- | :--- |
| **Ubuntu 24.04 LTS** | Python 3.12 | `postfix`, `bsd-mailx` (if present) | `sudo apt purge postfix bsd-mailx -y` |
| **Ubuntu 26.04 LTS** | Python 3.13+ | `postfix`, `bsd-mailx` (if present) | `sudo apt purge postfix bsd-mailx -y` |
| **Debian 12 (Bookworm)** | Python 3.11 | `exim4`, `exim4-base`, `exim4-daemon-light` | `sudo apt purge 'exim4*' postfix bsd-mailx -y` |
| **Debian 13 (Trixie)** | Python 3.13 | `exim4`, `exim4-base`, `exim4-daemon-light` | `sudo apt purge 'exim4*' postfix bsd-mailx -y` |

> [!NOTE]
> Debian installs `exim4` by default, which occupies `/usr/sbin/sendmail` via Debian alternatives. Purging `exim4*` frees `/usr/sbin/sendmail` cleanly so `mail2hook` can replace it.

---

## Step-by-Step Installation

### Step 1: Install System Prerequisites & Remove Existing MTAs

Run on the target server:

#### On Ubuntu 24.04 LTS / Ubuntu 26.04 LTS:
```bash
sudo apt update
sudo apt install -y python3 python3-venv git unattended-upgrades

# Stop and purge any existing Postfix or mailx packages
sudo systemctl stop postfix 2>/dev/null || true
sudo apt purge postfix bsd-mailx -y
```

#### On Debian 12 / Debian 13:
```bash
sudo apt update
sudo apt install -y python3 python3-venv git unattended-upgrades

# Debian ships with exim4 by default; purge it and any postfix remnants
sudo systemctl stop exim4 2>/dev/null || true
sudo systemctl stop postfix 2>/dev/null || true
sudo apt purge 'exim4*' postfix bsd-mailx -y
```

---

### Step 2: Set Up Virtual Environment and Install `mail2hook`

Identical on Ubuntu 24, Ubuntu 26, Debian 12, and Debian 13:

```bash
# 1. Create directories
sudo mkdir -p /opt/mail2hook /etc/mail2hook

# 2. Create isolated virtual environment
sudo python3 -m venv /opt/mail2hook/venv

# 3. Upgrade pip and install mail2hook from GitHub
sudo /opt/mail2hook/venv/bin/pip install --upgrade pip
sudo /opt/mail2hook/venv/bin/pip install --upgrade git+https://github.com/heliophane/mail2hook.git

# 4. Symlink mail2hook CLI into PATH
sudo ln -sf /opt/mail2hook/venv/bin/mail2hook /usr/local/bin/mail2hook

# 5. Symlink mail2hook as the system sendmail and mailq binaries
sudo ln -sf /opt/mail2hook/venv/bin/mail2hook /usr/sbin/sendmail
sudo ln -sf /opt/mail2hook/venv/bin/mail2hook /usr/bin/mailq
```

---

### Step 3: Configure Environment (`/etc/mail2hook/env`)

Create and lock down the configuration file:

```bash
sudo touch /etc/mail2hook/env
sudo chmod 0600 /etc/mail2hook/env
```

Edit `/etc/mail2hook/env` (`sudo nano /etc/mail2hook/env`):

```ini
# Required: Discord Webhook URL
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/YOUR_WEBHOOK_ID/YOUR_WEBHOOK_TOKEN

# Optional: Friendly server name shown in Discord (defaults to system hostname)
# Examples: web01, prod-db, bookstack
SERVER_NAME=my-server-name
```

> [!TIP]
> **`/etc/mail2hook/env` Permissions**:
> Mode `0600` (owned by `root:root`) keeps your Discord webhook secret unreadable by unprivileged local users while allowing `root` processes (`unattended-upgrades`, root `cron`, `sudo sendmail`) to send alerts.
> If unprivileged local users or services also need to send mail, run `mail2hook`'s built-in SMTP daemon (`mail2hook --print-systemd-service`) as `root` on `127.0.0.1:25` so local apps can submit mail over localhost without needing read access to `/etc/mail2hook/env`.

---

### Step 4: Configure `unattended-upgrades`

Generate and install the drop-in configuration file:

```bash
mail2hook --print-apt-config | sudo tee /etc/apt/apt.conf.d/52mail2hook
```

This installs `/etc/apt/apt.conf.d/52mail2hook`:
```apt
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
APT::Periodic::AutocleanInterval "7";
Unattended-Upgrade::Mail "root";
Unattended-Upgrade::MailReport "always";
Unattended-Upgrade::Sender "unattended-upgrades";
```

#### Optional: Security Updates Only

If you only want automatic upgrades for security patches and want obsolete kernels auto-removed, configure `/etc/apt/apt.conf.d/50unattended-upgrades` based on [examples/50unattended-upgrades-security-only.conf](../examples/50unattended-upgrades-security-only.conf):

```apt
// /etc/apt/apt.conf.d/50unattended-upgrades
Unattended-Upgrade::Allowed-Origins {
    "${distro_id}:${distro_codename}-security";
};
Unattended-Upgrade::Remove-Unused-Dependencies "true";
Unattended-Upgrade::Remove-Unused-Kernel-Packages "true";
```

---

### Step 5: Test & Verify

#### 1. Test Sendmail Drop-in Directly
Run as `root` (or with `sudo`) using single quotes so bash does not expand `!`:
```bash
printf 'Subject: unattended-upgrades result for %s: SUCCESS\nFrom: root <root@%s>\n\nUnattended upgrade result: All upgrades installed\n\nPackages that were upgraded:\n libssl3 openssl\n' "$(hostname)" "$(hostname)" | sudo /usr/sbin/sendmail -t -oi root
```
Check your Discord channel — the embed with your server name and OS distribution should appear immediately.

#### 2. Test `mailq`
```bash
mailq
# Output: Mail queue is empty
```

#### 3. Test `unattended-upgrades` Dry Run
```bash
sudo unattended-upgrades --dry-run --debug
```
*(Note: `unattended-upgrades` only invokes `sendmail` when packages are actually upgraded or an error occurs, not when 0 upgrades are available.)*

---

## Managing Multiple Servers in One Discord Channel

When multiple servers post to the same Discord channel, `mail2hook` automatically identifies each host:

1. **Server Badging**:
   - **Embed Author**: `Server: <SERVER_NAME> • <OS Distribution>`
   - **Embed Fields**: `Server`, `OS`, `Reboot`, `From`, `Date`, `Server Logs`
   - **Embed Footer**: `mail2hook • <SERVER_NAME> (<OS Distribution>)`
2. **Distinct Server Identifiers**: Set `SERVER_NAME` in `/etc/mail2hook/env` on each machine (e.g., `SERVER_NAME=prod-db01`, `SERVER_NAME=ingress-proxy`).
3. **Reboot Required Indicators**: When a kernel or core library update requires a reboot (`/run/reboot-required` exists or email indicates reboot), the embed displays:
   - `Reboot`: `⚠️ Required` (along with affected packages if listed)
   - `Server Logs`: includes `sudo reboot` reminder

---

## Upgrading `mail2hook`

To upgrade `mail2hook` to the latest release on any server:

```bash
sudo /opt/mail2hook/venv/bin/pip install --upgrade git+https://github.com/heliophane/mail2hook.git
```
No configuration changes are needed (if running the optional `mail2hook.service` SMTP daemon, restart it with `sudo systemctl restart mail2hook`).
