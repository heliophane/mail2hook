"""Diagnostic checks (--doctor) for mail2hook installations."""

from __future__ import annotations

import grp
import json
import os
import platform
import pwd
import re
import socket
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TextIO

from mail2hook import __version__
from mail2hook.config import (
    DEFAULT_SYSTEM_ENV_PATH,
    Config,
    ConfigError,
    _is_running_as_root,
    load_config,
)
from mail2hook.discord import (
    DiscordPermanentError,
    DiscordTemporaryError,
    build_payload,
    extract_webhook_id,
    post_to_discord,
    redact_secrets,
)
from mail2hook.filters import should_process_message
from mail2hook.parse import parse_email

EXIT_OK = 0
EXIT_DATAERR = 65
EXIT_TEMPFAIL = 75
EXIT_CONFIG = 78

DEFAULT_SENDMAIL_PATH = Path("/usr/sbin/sendmail")
DEFAULT_POSTFIX_MAIN_CF = Path("/etc/postfix/main.cf")
DEFAULT_POSTFIX_MASTER_CF = Path("/etc/postfix/master.cf")
DEFAULT_POSTFIX_TRANSPORT = Path("/etc/postfix/mail2hook_transport")
DEFAULT_POSTFIX_RECIPIENT = "mail2hook@localhost"
DEFAULT_POSTFIX_TRANSPORT_NAME = "mail2hook"

MTA_PACKAGE_NAMES: tuple[str, ...] = (
    "postfix",
    "exim4",
    "exim4-daemon-light",
    "exim4-daemon-heavy",
    "msmtp-mta",
    "ssmtp",
    "nullmailer",
    "dma",
    "opensmtpd",
    "sendmail-bin",
)

_APT_CONFIG_PREFIXES: tuple[str, ...] = (
    "unattended-upgrade::mail",
    "unattended-upgrade::sender",
    "unattended-upgrade::automatic-reboot",
    "apt::periodic::update-package-lists",
    "apt::periodic::unattended-upgrade",
    "apt::periodic::autocleaninterval",
)

_DOCTOR_SAMPLE_EMAIL = (
    b"From: unattended-upgrades <root@localhost>\n"
    b"To: mail2hook@localhost\n"
    b"Subject: unattended-upgrades result for server01.example.com: SUCCESS\n"
    b"Content-Type: text/plain; charset=utf-8\n"
    b"\n"
    b"Unattended upgrade returned: True\n"
    b"Packages that were upgraded:\n"
    b"  openssl\n"
)

_DPKG_MTA_LABELS: tuple[tuple[str, str], ...] = (
    ("postfix", "Postfix"),
    ("exim", "Exim"),
    ("msmtp", "msmtp"),
    ("nullmailer", "nullmailer"),
    ("ssmtp", "ssmtp"),
    ("opensmtpd", "OpenSMTPD"),
    ("dma", "dma"),
    ("sendmail", "Sendmail"),
)


def _run_cmd(cmd: Sequence[str], timeout: float = 5.0) -> tuple[int, str, str] | None:
    """Run a diagnostic subprocess without shell expansion, returning None if missing."""
    env = {**os.environ, "LC_ALL": "C"}
    try:
        proc = subprocess.run(
            list(cmd),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=env,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return None
    return proc.returncode, proc.stdout, proc.stderr


def _format_user_identity() -> str:
    """Return running user, uid, primary gid, and supplementary groups."""
    uid = os.getuid()
    gid = os.getgid()
    try:
        user_name = pwd.getpwuid(uid).pw_name
    except KeyError:
        user_name = str(uid)
    try:
        group_name = grp.getgrgid(gid).gr_name
    except KeyError:
        group_name = str(gid)

    supp_groups: list[str] = []
    try:
        for group_id in os.getgroups():
            try:
                supp_groups.append(grp.getgrgid(group_id).gr_name)
            except KeyError:
                supp_groups.append(str(group_id))
    except OSError:
        pass

    groups_str = ",".join(supp_groups) if supp_groups else group_name
    return f"{user_name} (uid={uid}, gid={gid}/{group_name}, groups={groups_str})"


def _classify_package_label(text: str) -> str | None:
    """Map dpkg package names or paths to a human-readable MTA name."""
    lower = text.lower()
    for needle, label in _DPKG_MTA_LABELS:
        if needle in lower:
            return label
    return None


def inspect_sendmail(sendmail_path: Path = DEFAULT_SENDMAIL_PATH) -> tuple[str, str]:
    """Inspect the sendmail binary/symlink path and classify which MTA owns it."""
    is_link = sendmail_path.is_symlink()
    if not is_link and not sendmail_path.exists():
        return str(sendmail_path), "missing"

    try:
        resolved = Path(os.path.realpath(sendmail_path))
    except OSError:
        resolved = sendmail_path

    path_display = f"{sendmail_path} -> {resolved}" if resolved != sendmail_path else str(resolved)
    if not resolved.exists():
        return path_display, "broken symlink"

    if resolved.name == "mail2hook" or "mail2hook" in str(resolved):
        return path_display, "mail2hook shim"

    try:
        head = resolved.read_bytes()[:8192]
    except OSError:
        head = b""

    if head and b"\x00" not in head:
        head_text = head.decode("utf-8", errors="ignore")
        if "mail2hook" in head_text:
            return path_display, "mail2hook shim"
        label = _classify_package_label(head_text)
        if label:
            return path_display, label

    for candidate in (str(resolved), str(sendmail_path)):
        dpkg_res = _run_cmd(["dpkg-query", "-S", candidate])
        if dpkg_res and dpkg_res[0] == 0 and dpkg_res[1].strip():
            pkg = dpkg_res[1].splitlines()[0].split(":", 1)[0].strip()
            label = _classify_package_label(pkg)
            if label:
                return path_display, label
            return path_display, f"package:{pkg}"

    name_label = _classify_package_label(resolved.name)
    if name_label:
        return path_display, name_label

    if head.startswith(b"\x7fELF") or head[:4] in (
        b"\xcf\xfa\xed\xfe",
        b"\xfe\xed\xfa\xcf",
        b"\xca\xfe\xba\xbe",
    ):
        return path_display, "unknown MTA binary"
    return path_display, "unknown executable"


def inspect_installed_mtas() -> list[str]:
    """Return installed Debian/Ubuntu MTA packages via dpkg-query."""
    res = _run_cmd(
        [
            "dpkg-query",
            "-W",
            "-f=${Package}\t${db:Status-Abbrev}\t${Version}\n",
            *MTA_PACKAGE_NAMES,
        ]
    )
    if res is None:
        return ["dpkg-query not available"]

    _, stdout, _ = res
    installed: list[str] = []
    for line in stdout.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and parts[1].startswith("ii"):
            version = parts[2].strip() if len(parts) > 2 else ""
            installed.append(f"{parts[0]} ({version})" if version else parts[0])
    return installed


def _read_text_status(path: Path) -> tuple[str | None, str | None]:
    """Read a text file, distinguishing missing from EACCES/other errors."""
    try:
        return path.read_text(encoding="utf-8", errors="replace"), None
    except FileNotFoundError:
        return None, "missing"
    except PermissionError:
        return None, "permission denied"
    except OSError as err:
        return None, str(err)


def _extract_main_cf_param(main_cf_text: str, param: str) -> str | None:
    """Extract the last assignment for a parameter in Postfix main.cf."""
    pattern = re.compile(rf"^\s*{re.escape(param)}\s*=\s*(.*)$")
    value: str | None = None
    for line in main_cf_text.splitlines():
        match = pattern.match(line)
        if match:
            value = match.group(1).strip()
    return value


def inspect_postfix_route(
    *,
    main_cf: Path = DEFAULT_POSTFIX_MAIN_CF,
    master_cf: Path = DEFAULT_POSTFIX_MASTER_CF,
    transport_file: Path = DEFAULT_POSTFIX_TRANSPORT,
    recipient: str = DEFAULT_POSTFIX_RECIPIENT,
    transport_name: str = DEFAULT_POSTFIX_TRANSPORT_NAME,
    use_postconf: bool | None = None,
) -> dict[str, str]:
    """Inspect whether the Postfix mail2hook pipe route is configured."""
    if use_postconf is None:
        use_postconf = main_cf == DEFAULT_POSTFIX_MAIN_CF

    master_text, master_err = _read_text_status(master_cf)
    master_ok = False
    if master_text is not None:
        master_re = re.compile(rf"^\s*{re.escape(transport_name)}\s+unix\b", re.MULTILINE)
        master_ok = bool(master_re.search(master_text))

    transport_maps_val: str | None = None
    recipient_limit_val: str | None = None
    main_text, main_err = _read_text_status(main_cf)
    if use_postconf:
        maps_res = _run_cmd(["postconf", "-h", "transport_maps"])
        if maps_res and maps_res[0] == 0:
            transport_maps_val = maps_res[1].strip()
        limit_res = _run_cmd(["postconf", "-h", f"{transport_name}_destination_recipient_limit"])
        if limit_res and limit_res[0] == 0:
            recipient_limit_val = limit_res[1].strip()
    if transport_maps_val is None and main_text is not None:
        transport_maps_val = _extract_main_cf_param(main_text, "transport_maps") or ""
    if recipient_limit_val is None and main_text is not None:
        recipient_limit_val = (
            _extract_main_cf_param(main_text, f"{transport_name}_destination_recipient_limit") or ""
        )

    transport_ref = f"hash:{transport_file}"
    main_maps_ok = bool(transport_maps_val and transport_ref in transport_maps_val)

    transport_text, transport_err = _read_text_status(transport_file)
    transport_file_ok = False
    if transport_text is not None:
        t_re = re.compile(
            rf"^\s*{re.escape(recipient)}\s+{re.escape(transport_name)}:",
            re.MULTILINE,
        )
        transport_file_ok = bool(t_re.search(transport_text))

    db_path = Path(f"{transport_file}.db")
    db_exists = db_path.exists()
    postmap_lookup = "not checked"
    if transport_file_ok:
        pm_res = _run_cmd(["postmap", "-q", recipient, f"hash:{transport_file}"])
        if pm_res is not None and pm_res[0] == 0 and transport_name in pm_res[1]:
            postmap_lookup = f"ok ({pm_res[1].strip()})"
        elif pm_res is not None:
            postmap_lookup = "missing or stale map"

    present = [
        master_ok,
        main_maps_ok,
        transport_file_ok,
        db_exists or postmap_lookup.startswith("ok"),
    ]
    if all(present):
        status = "configured"
    elif any(present):
        missing: list[str] = []
        if not master_ok:
            missing.append(f"master.cf ({master_err or 'missing entry'})")
        if not main_maps_ok:
            missing.append(f"main.cf transport_maps ({main_err or 'missing map'})")
        if not transport_file_ok:
            missing.append(f"transport file ({transport_err or 'missing route'})")
        if not (db_exists or postmap_lookup.startswith("ok")):
            missing.append("transport .db map")
        status = f"partially configured (missing: {', '.join(missing)})"
    else:
        status = "not configured"

    db_state = f"{'present' if db_exists else 'absent'} ({db_path}); postmap={postmap_lookup}"
    return {
        "status": status,
        "master_cf": f"{'present' if master_ok else (master_err or 'absent')} ({master_cf})",
        "transport_maps": transport_maps_val or (main_err or "(unset)"),
        "recipient_limit": recipient_limit_val or "(unset)",
        "transport_file": (
            f"{'present' if transport_file_ok else (transport_err or 'absent')} ({transport_file})"
        ),
        "transport_db": db_state,
    }


def _inspect_single_env_path(path: Path) -> dict[str, str]:
    """Check existence, mode, ownership, and readability for one env path."""
    parent_info = ""
    try:
        p_stat = os.stat(path.parent)
        parent_info = f"dir={oct(p_stat.st_mode & 0o777)}"
    except PermissionError:
        parent_info = "dir=EACCES"
    except OSError:
        parent_info = "dir=missing"

    try:
        st = os.stat(path)
    except PermissionError:
        return {
            "path": str(path),
            "exists": "unknown (permission denied traversing directory)",
            "readable": f"no (EACCES; {parent_info})",
            "permissions": parent_info,
        }
    except FileNotFoundError:
        return {
            "path": str(path),
            "exists": "no",
            "readable": "no (not found)",
            "permissions": parent_info,
        }
    except OSError as err:
        return {
            "path": str(path),
            "exists": f"error ({err})",
            "readable": "no",
            "permissions": parent_info,
        }

    try:
        owner = pwd.getpwuid(st.st_uid).pw_name
    except KeyError:
        owner = str(st.st_uid)
    try:
        group = grp.getgrgid(st.st_gid).gr_name
    except KeyError:
        group = str(st.st_gid)

    mode = oct(st.st_mode & 0o777)
    try:
        with path.open("rb") as handle:
            handle.read(1)
        readable = "yes"
    except PermissionError:
        readable = "no (permission denied)"
    except OSError as err:
        readable = f"no ({err})"

    return {
        "path": str(path),
        "exists": "yes",
        "readable": readable,
        "permissions": f"mode={mode} owner={owner}:{group} {parent_info}",
    }


def inspect_env_file(env_file: Path | None = None) -> dict[str, str]:
    """Inspect whichever env file mail2hook would examine without exposing secrets."""
    if env_file is not None:
        return _inspect_single_env_path(env_file)

    system_info = _inspect_single_env_path(DEFAULT_SYSTEM_ENV_PATH)
    if system_info["exists"] != "no":
        return system_info

    if not _is_running_as_root():
        local_env = Path(".env")
        local_info = _inspect_single_env_path(local_env)
        if local_info["exists"] != "no":
            return local_info

    return system_info


def inspect_apt_config() -> list[str]:
    """Read APT / unattended-upgrades mail settings via apt-config dump."""
    res = _run_cmd(["apt-config", "dump"])
    if res is None:
        return ["apt-config not available"]
    if res[0] != 0:
        return [f"apt-config exited {res[0]}"]

    lines: list[str] = []
    for raw_line in res[1].splitlines():
        stripped = raw_line.strip()
        lower = stripped.lower()
        if any(lower.startswith(prefix) for prefix in _APT_CONFIG_PREFIXES):
            lines.append(stripped)
    return lines or ["(no Unattended-Upgrade mail or periodic keys set)"]


def _run_sample_dry_run(config: Config) -> tuple[bool, int, str]:
    """Parse and evaluate the built-in sample message against config."""
    msg = parse_email(
        _DOCTOR_SAMPLE_EMAIL,
        max_body_chars=config.max_body_chars,
        check_system_reboot=False,
    )
    matched = should_process_message(msg, config)
    payload = build_payload(msg, config)
    serialized = json.dumps(payload)
    embed_title = ""
    if payload.get("embeds"):
        embed_title = str(payload["embeds"][0].get("title", ""))
    return matched, len(serialized.encode("utf-8")), embed_title


def _send_doctor_test_message(config: Config) -> int:
    """Post a clearly labeled test message to Discord for --doctor --send-test."""
    effective_host = config.server_name or socket.gethostname()
    raw = (
        f"From: mail2hook-doctor <mail2hook@localhost>\n"
        f"To: mail2hook@localhost\n"
        f"Subject: [mail2hook --doctor --send-test] "
        f"unattended-upgrades test from {effective_host}\n"
        f"Content-Type: text/plain; charset=utf-8\n"
        f"\n"
        f"Diagnostic test message sent by `mail2hook --doctor --send-test`.\n"
        f"Host: {effective_host}\n"
    ).encode()
    msg = parse_email(raw, max_body_chars=config.max_body_chars, check_system_reboot=False)
    payload = build_payload(msg, config)
    try:
        post_to_discord(config.discord_webhook_url, payload)
    except DiscordTemporaryError as err:
        safe_err = redact_secrets(str(err), config.discord_webhook_url)
        sys.stderr.write(f"mail2hook: --send-test temporary failure: {safe_err}\n")
        return EXIT_TEMPFAIL
    except DiscordPermanentError as err:
        safe_err = redact_secrets(str(err), config.discord_webhook_url)
        sys.stderr.write(f"mail2hook: --send-test permanent failure: {safe_err}\n")
        return EXIT_CONFIG if err.status_code in (401, 403, 404) else EXIT_DATAERR
    except Exception as err:
        safe_err = redact_secrets(str(err), config.discord_webhook_url)
        sys.stderr.write(f"mail2hook: --send-test unexpected failure: {safe_err}\n")
        return EXIT_TEMPFAIL
    return EXIT_OK


def run_doctor(
    *,
    env_file: Path | None = None,
    send_test: bool = False,
    out: TextIO | None = None,
    sendmail_path: Path = DEFAULT_SENDMAIL_PATH,
    main_cf: Path = DEFAULT_POSTFIX_MAIN_CF,
    master_cf: Path = DEFAULT_POSTFIX_MASTER_CF,
    transport_file: Path = DEFAULT_POSTFIX_TRANSPORT,
) -> int:
    """Run all diagnostic checks, print a secret-free report, and optionally send a test post."""
    stream = sys.stdout if out is None else out
    sm_display, sm_kind = inspect_sendmail(sendmail_path)
    installed_mtas = inspect_installed_mtas()
    postfix_info = inspect_postfix_route(
        main_cf=main_cf,
        master_cf=master_cf,
        transport_file=transport_file,
    )
    env_info = inspect_env_file(env_file)
    apt_lines = inspect_apt_config()

    config: Config | None = None
    config_err: str | None = None
    try:
        config = load_config(env_file=env_file)
    except ConfigError as err:
        config_err = redact_secrets(str(err))
    except Exception as err:
        config_err = redact_secrets(f"unexpected error: {err}")

    lines: list[str] = [
        f"mail2hook doctor (v{__version__})",
        "=" * 60,
        f"Version            : {__version__}",
        f"Python             : {sys.executable} ({platform.python_version()})",
        f"Hostname / FQDN    : {socket.gethostname()} / {socket.getfqdn()}",
        f"Running user       : {_format_user_identity()}",
        f"Sendmail path      : {sm_display}",
        f"Sendmail owner     : {sm_kind}",
        f"Installed MTAs     : {', '.join(installed_mtas) if installed_mtas else 'none detected'}",
        f"Postfix route      : {postfix_info['status']}",
        f"  master.cf        : {postfix_info['master_cf']}",
        f"  transport_maps   : {postfix_info['transport_maps']}",
        f"  recipient_limit  : {postfix_info['recipient_limit']}",
        f"  transport file   : {postfix_info['transport_file']}",
        f"  transport db     : {postfix_info['transport_db']}",
        f"Env file path      : {env_info['path']}",
        f"  exists           : {env_info['exists']}",
        f"  readable         : {env_info['readable']}",
        f"  permissions      : {env_info['permissions']}",
    ]

    if config is not None:
        webhook_id = extract_webhook_id(config.discord_webhook_url) or "unknown"
        effective_host = config.server_name or socket.gethostname()
        senders = ", ".join(config.filter_senders) if config.filter_senders else "(none)"
        keywords = (
            ", ".join(config.filter_subject_keywords)
            if config.filter_subject_keywords
            else "(none)"
        )
        matched, payload_bytes, embed_title = _run_sample_dry_run(config)
        match_desc = "matched (would post)" if matched else "filtered out by current filters"
        server_display = config.server_name or f"(unset; effective: {effective_host})"
        lines.extend(
            [
                "Config status      : OK",
                f"  Webhook ID       : {webhook_id} (token redacted)",
                f"  SERVER_NAME      : {server_display}",
                f"  FILTER_SENDERS   : {senders}",
                f"  FILTER_KEYWORDS  : {keywords}",
                f"  MAX_BODY_CHARS   : {config.max_body_chars}",
                f"  DISCORD_USERNAME : {config.discord_username}",
                f"Sample dry-run     : {match_desc} ({payload_bytes} bytes, title={embed_title!r})",
            ]
        )
    else:
        lines.extend(
            [
                f"Config status      : ERROR ({config_err})",
                "Sample dry-run     : skipped (valid config required)",
            ]
        )

    lines.append("APT mail config    :")
    for apt_line in apt_lines:
        lines.append(f"  {apt_line}")

    stream.write("\n".join(lines) + "\n")

    if config is None:
        return EXIT_CONFIG

    if send_test:
        test_exit = _send_doctor_test_message(config)
        if test_exit == EXIT_OK:
            webhook_id = extract_webhook_id(config.discord_webhook_url) or "unknown"
            stream.write(
                f"Send test          : OK (delivered to Discord webhook ID {webhook_id})\n"
            )
        return test_exit

    return EXIT_OK
