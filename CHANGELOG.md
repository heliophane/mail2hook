# Changelog

All notable changes to `mail2hook` are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.6.0] - 2026-11-25

### Added

- Yellow warning embed bar (`#FEE75C`) in Discord when a server reboot is required, packages are kept back, or the email subject indicates a warning, with strict color precedence: Red (failure) > Yellow (reboot required / warning) > Green (clean success) > Blurple (info).
- Expanded `detect_status()` matching for `Unattended upgrade returned: False` / `True`, `Packages with upgradable origin but kept back:`, and warning subjects while avoiding false positives on phrases like `0 warnings` or `No warnings`.
- **Discord Notifications** section in [README.md](README.md) documenting the embed layout and status color rules, plus this `CHANGELOG.md`.

### Changed

- Removed the `From` and `To` fields from Discord webhook embeds to reduce visual noise (`From` and `To` headers are still parsed internally for sender filtering and hostname extraction).
- Synced `mail2hook --print-apt-config` comments with `examples/52mail2hook`, including Postfix `mail2hook@localhost` guidance and yellow reboot alert notes.
- Updated package version metadata in `pyproject.toml` and `mail2hook.__version__` to `0.6.0`.
- Refreshed and verified documentation across `README.md`, `docs/MULTI_SERVER_SETUP.md`, `.env.example`, `examples/`, and `AGENTS.md`.

## [0.5.0] - 2026-11-20

### Added

- Postfix-aware pipe integration (`mail2hook@localhost` transport) in `install.sh` so existing `/usr/sbin/sendmail` and root/admin mail forwarding remain untouched on mail-enabled hosts.
- Built-in diagnostics (`mail2hook --doctor` and `mail2hook --doctor --send-test`) to inspect `sendmail` ownership, Postfix routing, `/etc/mail2hook/env` permissions, redacted webhook ID, APT mail settings, and sample dry-runs.
- Real-path end-to-end delivery verification in `install.sh` using a temporary confirmation marker (`/run/mail2hook/e2e-confirm`) so installation fails loudly if delivery through `sendmail` or Postfix cannot be confirmed.
- Syslog `LOG_MAIL` delivery telemetry (secret-redacted, no email bodies) and filter-drop visibility under `-v` and `--dry-run`.
- Vertical `old → new` package version formatting for `unattended-upgrades` reports (with `/var/log/unattended-upgrades/unattended-upgrades-dpkg.log` and `/var/log/dpkg.log` fallback), OS detection, `SERVER_NAME` override, and reboot-required detection (`Reboot: ⚠️ Required`).
- Automated, idempotent `install.sh` for Debian 12/13 and Ubuntu 24.04/26.04 with security-only `unattended-upgrades` configuration, systemd timer windows, `--skip-upgrades`, `--yes`, `--uninstall`, `--purge-upgrades`, and safe backups under `/var/backups/mail2hook/`.

### Changed

- Refactored `mail2hook` into a lightweight, zero-dependency Python 3.11+ zipapp (`sendmail` shim via `update-alternatives`) with no background daemon, listening port, or dedicated service user.
- Hardened HTTP webhook delivery (disabled 3xx redirects, `429` `retry_after` deadline/jitter handling, `408`/Cloudflare `403` retries, bounded error reads, and `redact_webhook_url` / `redact_secrets` sanitization).
- Hardened configuration loading (ignored CWD `.env` when running as `root` or when `/etc/mail2hook/env` exists, surfaced `EACCES` directory errors, failed on missing `--env-file`, and validated `DISCORD_USERNAME`).

[0.6.0]: https://github.com/heliophane/mail2hook/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/heliophane/mail2hook/releases/tag/v0.5.0
