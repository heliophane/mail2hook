"""Shared test fixtures isolating tests from host state and environment."""

from __future__ import annotations

import syslog
from pathlib import Path

import pytest

_MAIL2HOOK_ENV_VARS = (
    "DISCORD_WEBHOOK_URL",
    "DISCORD_WEBHOOK",
    "WEBHOOK_URL",
    "SERVER_NAME",
    "HOSTNAME_OVERRIDE",
    "FILTER_SENDERS",
    "FILTER_SUBJECT_KEYWORDS",
    "MAX_BODY_CHARS",
    "DISCORD_USERNAME",
)


@pytest.fixture(autouse=True)
def isolate_host_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Prevent host env files, env vars, dpkg logs, and reboot flags from leaking into tests."""
    monkeypatch.setattr("mail2hook.config.DEFAULT_SYSTEM_ENV_PATH", tmp_path / "nonexistent_env")
    monkeypatch.chdir(tmp_path)

    for var in _MAIL2HOOK_ENV_VARS:
        monkeypatch.delenv(var, raising=False)

    monkeypatch.setattr(
        "mail2hook.parse.DEFAULT_REBOOT_FLAG_PATHS",
        (tmp_path / "nonexistent-reboot-required",),
    )
    monkeypatch.setattr(
        "mail2hook.parse.DEFAULT_REBOOT_PKGS_PATHS",
        (tmp_path / "nonexistent-reboot-required.pkgs",),
    )
    monkeypatch.setattr(
        "mail2hook.parse.DEFAULT_DPKG_LOG_PATHS",
        (tmp_path / "nonexistent-dpkg.log",),
    )
    monkeypatch.setattr(syslog, "openlog", lambda *args, **kwargs: None)
    monkeypatch.setattr(syslog, "syslog", lambda *args, **kwargs: None)
