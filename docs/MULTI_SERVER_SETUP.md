# Multi-Server Setup Guide: Ubuntu 24, Ubuntu 25, Debian 12, Debian 13

This guide provides step-by-step instructions for deploying `mail2hook` across any Ubuntu or Debian server to receive system alerts (including `unattended-upgrades`, `cron`, and `apt-listchanges`) directly in Discord without running Postfix, Exim, or any other heavy Mail Transfer Agent (MTA).

---

## Architecture Overview

```
                      ┌────────────────────────────────────────┐
                      │ Debian / Ubuntu Server                 │
                      │                                        │
                      │  unattended-upgrades / cron            │
                      │               │                        │
                      │               ▼                        │
                      │      /usr/sbin/sendmail -t -oi         │
                      │               │ (symlink)              │
                      │               ▼                        │
                      │   /opt/mail2hook/venv/bin/mail2hook   │
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

| Distribution | Default Python | Pre-installed / Conflicting MTA | Clean Up Command |
| :--- | :--- | :--- | :--- |
| **Ubuntu 24.04 LTS** (Noble) | Python 3.12 | `postfix`, `bsd-mailx` (optional) | `sudo apt purge postfix bsd-mailx -y` |
| **Ubuntu 25.04** (Plucky) / 24.10 | Python 3.12 / 3.13 | `postfix`, `bsd-mailx` (optional) | `sudo apt purge postfix bsd-mailx -y` |
| **Debian 12** (Bookworm) | Python 3.11 | `exim4`, `exim4-base`, `exim4-daemon-light` | `sudo apt purge 'exim4*' postfix bsd-mailx -y` |
| **Debian 13** (Trixie) | Python 3.12 / 3.13 | `exim4`, `exim4-base`, `exim4-daemon-light` | `sudo apt purge 'exim4*' postfix bsd-mailx -y` |

> [!NOTE]
> Debian installs `exim4` by default, which occupies `/usr/sbin/sendmail` via Debian alternatives. Purging `exim4*` frees `/usr/sbin/sendmail` cleanly so `mail2hook` can take over.

---

## Step-by-Step Installation

### Step 1: Install System Prerequisites & Remove MTAs

Run on the target server:

#### For Ubuntu 24.04 / 25.04:
```bash
sudo apt update
sudo apt install -y python3 python3-venv git unattended-upgrades

# Stop and purge any existing Postfix or mailx packages
sudo systemctl stop postfix 2>/dev/null || true
sudo apt purge postfix bsd-mailx -y
```

#### For Debian 12 / 13:
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

On any distribution:

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

Create the configuration file:

```bash
sudo touch /etc/mail2hook/env
sudo chmod 0600 /etc/mail2hook/env
```

Edit `/etc/mail2hook/env` (`sudo nano /etc/mail2hook/env`):

```ini
# Required: Discord Webhook URL
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/YOUR_WEBHOOK_ID/YOUR_WEBHOOK_TOKEN

# Optional: Friendly Server Name shown in Discord author header (e.g. database, web01, bookstack)
# If left commented out, mail2hook automatically detects the system hostname.
SERVER_NAME=my-server-name
```

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

If you only want automatic upgrades for security patches and want obsolete kernels auto-removed, edit `/etc/apt/apt.conf.d/50unattended-upgrades` or install the provided example:

```bash
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
```bash
printf "Subject: unattended-upgrades result for $(hostname): SUCCESS\nFrom: root <root@$(hostname)>\n\nUnattended upgrade result: All upgrades installed\n\nPackages that were upgraded:\n libssl3 openssl\n" | sendmail -t -oi root
```
Check your Discord channel — the rich embed with your server name and OS distribution should appear immediately!

#### 2. Test `mailq`
```bash
mailq
# Output: Mail queue is empty
```

#### 3. Test `unattended-upgrades` Dry Run
```bash
sudo unattended-upgrades --dry-run --debug
```

---

## Managing Multiple Servers in One Discord Channel

When managing multiple servers posting into the same Discord channel:

1. **Server Badging**: Each server's Discord embed displays:
   - **Embed Author**: `Server: <SERVER_NAME>` (or system hostname)
   - **Embed Footer**: `mail2hook • <OS Distribution> (e.g. Ubuntu 24.04 or Debian 12)`
2. **Distinct Server Identifiers**: Set `SERVER_NAME` in `/etc/mail2hook/env` on each machine (e.g. `SERVER_NAME=prod-db01`, `SERVER_NAME=ingress-proxy`).
3. **Reboot Required Indicators**: If a security kernel or system library update requires a reboot, the embed prominently displays:
   - `Reboot: ⚠️ Required`
   - Server log hint: `Run 'sudo reboot' to apply kernel updates.`

---

## Upgrading `mail2hook` in the Future

To upgrade `mail2hook` to the latest release on any server:

```bash
sudo /opt/mail2hook/venv/bin/pip install --upgrade git+https://github.com/heliophane/mail2hook.git
```
No service restarts or configuration changes are needed.
