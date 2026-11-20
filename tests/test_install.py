"""Tests for install.sh Postfix-aware helpers and security hardening."""

from __future__ import annotations

import subprocess
from pathlib import Path

INSTALL_SH = Path(__file__).resolve().parent.parent / "install.sh"


def _run_install_fn(
    bash_snippet: str, *, cwd: Path | None = None
) -> subprocess.CompletedProcess[str]:
    """Source install.sh in bash and execute a snippet."""
    script = f'source "{INSTALL_SH}"\n{bash_snippet}'
    return subprocess.run(
        ["bash", "-euo", "pipefail", "-c", script],
        cwd=cwd,
        text=True,
        capture_output=True,
        check=False,
    )


def test_install_sh_syntax_and_hardening_markers() -> None:
    res = subprocess.run(
        ["bash", "-n", str(INSTALL_SH)],
        text=True,
        capture_output=True,
        check=False,
    )
    assert res.returncode == 0, res.stderr

    content = INSTALL_SH.read_text(encoding="utf-8")
    assert '-p "/usr/bin/python3 -sE"' in content
    assert 'Unattended-Upgrade::MailOnlyOnError "false";' in content
    assert "destination_recipient_limit" in content
    assert "trap cleanup_temp_files EXIT" in content


def test_install_sh_help_omits_webhook_url_flag() -> None:
    res = subprocess.run(
        ["bash", str(INSTALL_SH), "--help"], text=True, capture_output=True, check=False
    )
    assert res.returncode == 0
    assert "--webhook-url" not in res.stdout
    assert "DISCORD_WEBHOOK_URL" in res.stdout


def test_detect_local_checkout_ignores_cwd_when_piped(tmp_path: Path) -> None:
    fake_pkg = tmp_path / "src" / "mail2hook"
    fake_pkg.mkdir(parents=True)
    (fake_pkg / "__init__.py").write_text('__version__ = "9.9.9"\n', encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "mail2hook"\n', encoding="utf-8")

    # Simulate "curl ... | bash": stdin script with BASH_SOURCE[0] unset
    install_src = INSTALL_SH.read_text(encoding="utf-8")
    snippet = install_src.replace(
        'if [[ -z "${BASH_SOURCE[0]:-}" || "${BASH_SOURCE[0]}" == "${0}" ]]; then\n'
        '    main "$@"\n'
        "fi",
        "detect_local_checkout",
    )
    res = subprocess.run(
        ["bash", "-s"],
        input=snippet,
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
    )
    assert res.returncode != 0
    assert res.stdout.strip() == ""


def test_master_cf_block_insert_idempotent_and_remove(tmp_path: Path) -> None:
    master_cf = tmp_path / "master.cf"
    backup_dir = tmp_path / "backups"
    original = (
        "smtp      inet  n       -       y       -       -       smtpd\n"
        "local     unix  -       n       n       -       -       local\n"
    )
    master_cf.write_text(original, encoding="utf-8")

    res1 = _run_install_fn(
        f'update_master_cf_block "{master_cf}" "nobody" "mail" '
        f'"/usr/local/bin/mail2hook" "{backup_dir}"'
    )
    assert res1.returncode == 0, res1.stderr
    updated = master_cf.read_text(encoding="utf-8")
    assert "# BEGIN mail2hook managed block" in updated
    assert "mail2hook unix  -       n       n       -       -       pipe" in updated
    assert (
        "flags=R user=nobody:mail argv=/usr/local/bin/mail2hook -oi -f ${sender} -- ${recipient}"
        in updated
    )
    assert "# END mail2hook managed block" in updated
    assert len(list(backup_dir.glob("*.bak"))) == 1

    # Second run is idempotent and does not create an extra backup
    res2 = _run_install_fn(
        f'update_master_cf_block "{master_cf}" "nobody" "mail" '
        f'"/usr/local/bin/mail2hook" "{backup_dir}"'
    )
    assert res2.returncode == 0, res2.stderr
    assert master_cf.read_text(encoding="utf-8") == updated
    assert updated.count("# BEGIN mail2hook managed block") == 1
    assert len(list(backup_dir.glob("*.bak"))) == 1

    # Removal restores original master.cf without touching other entries
    res3 = _run_install_fn(f'remove_master_cf_block "{master_cf}" "{backup_dir}"')
    assert res3.returncode == 0, res3.stderr
    assert master_cf.read_text(encoding="utf-8") == original
    assert len(list(backup_dir.glob("*.bak"))) == 2


def test_transport_map_value_helpers(tmp_path: Path) -> None:
    tfile = "/etc/postfix/mail2hook_transport"
    spec = f"hash:{tfile}"

    res = _run_install_fn(
        "\n".join(
            [
                f'echo "1=$(prepend_transport_map_value "" "{spec}" "{tfile}")"',
                f'echo "2=$(prepend_transport_map_value "lmdb:/etc/postfix/transport" '
                f'"{spec}" "{tfile}")"',
                f'echo "3=$(prepend_transport_map_value "{spec}, lmdb:/etc/postfix/transport" '
                f'"{spec}" "{tfile}")"',
                f'echo "4=$(strip_transport_map_value "{spec}, lmdb:/etc/postfix/transport" '
                f'"{tfile}")"',
                f'echo "5=$(strip_transport_map_value "{spec}" "{tfile}")"',
            ]
        )
    )
    assert res.returncode == 0, res.stderr
    lines = res.stdout.strip().splitlines()
    assert lines == [
        f"1={spec}",
        f"2={spec}, lmdb:/etc/postfix/transport",
        f"3={spec}, lmdb:/etc/postfix/transport",
        "4=lmdb:/etc/postfix/transport",
        "5=",
    ]


def test_save_and_restore_apt_mail_target(tmp_path: Path) -> None:
    apt_file = tmp_path / "52-unattended-upgrades-local"
    backup_dir = tmp_path / "backups"
    orig_file = backup_dir / "apt_mail_target.orig"

    # 1. Save previous value (e.g. admin@example.com); keep original if mail2hook@localhost
    res1 = _run_install_fn(
        f'prev1="$(save_previous_apt_mail_target "admin@example.com" '
        f'"{orig_file}" "{backup_dir}")"\n'
        f'prev2="$(save_previous_apt_mail_target "mail2hook@localhost" '
        f'"{orig_file}" "{backup_dir}")"\n'
        'echo "prev1=$prev1"\n'
        'echo "prev2=$prev2"\n'
    )
    assert res1.returncode == 0, res1.stderr
    assert res1.stdout.splitlines() == ["prev1=admin@example.com", "prev2=admin@example.com"]
    assert orig_file.read_text(encoding="utf-8") == "admin@example.com"

    # 2. Restore restores previous non-empty target and removes orig_file
    apt_file.write_text(
        'Unattended-Upgrade::Mail "mail2hook@localhost";\n'
        'Unattended-Upgrade::MailReport "on-change";\n',
        encoding="utf-8",
    )
    res2 = _run_install_fn(f'restore_apt_mail_target "{apt_file}" "{orig_file}" "{backup_dir}"')
    assert res2.returncode == 0, res2.stderr
    assert 'Unattended-Upgrade::Mail "admin@example.com";' in apt_file.read_text(encoding="utf-8")
    assert not orig_file.exists()
    assert len(list(backup_dir.glob("*.bak"))) == 1

    # 3. Previously unset (empty) target restores to commented-out state
    res3 = _run_install_fn(
        f'prev_empty="$(save_previous_apt_mail_target "" "{orig_file}" "{backup_dir}")"\n'
        'echo "prev_empty=$prev_empty"\n'
    )
    assert res3.returncode == 0, res3.stderr
    assert res3.stdout.strip() == "prev_empty="
    apt_file.write_text(
        'Unattended-Upgrade::Mail "mail2hook@localhost";\n',
        encoding="utf-8",
    )
    res4 = _run_install_fn(f'restore_apt_mail_target "{apt_file}" "{orig_file}" "{backup_dir}"')
    assert res4.returncode == 0, res4.stderr
    restored_text = apt_file.read_text(encoding="utf-8")
    assert '// Unattended-Upgrade::Mail "";' in restored_text
    assert not orig_file.exists()


def test_ensure_env_filter_default(tmp_path: Path) -> None:
    # Case 1: commented placeholder becomes uncommented default
    env1 = tmp_path / "env1"
    env1.write_text(
        "DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/1/tok\n# FILTER_SUBJECT_KEYWORDS=\n",
        encoding="utf-8",
    )
    res1 = _run_install_fn(f'ensure_env_filter_default "{env1}" "unattended-upgrades"')
    assert res1.returncode == 0, res1.stderr
    assert "\nFILTER_SUBJECT_KEYWORDS=unattended-upgrades\n" in env1.read_text(encoding="utf-8")

    # Case 2: missing line gets appended
    env2 = tmp_path / "env2"
    env2.write_text(
        "DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/1/tok\n",
        encoding="utf-8",
    )
    res2 = _run_install_fn(f'ensure_env_filter_default "{env2}" "unattended-upgrades"')
    assert res2.returncode == 0, res2.stderr
    assert "FILTER_SUBJECT_KEYWORDS=unattended-upgrades\n" in env2.read_text(encoding="utf-8")

    # Case 3: explicit custom filter is preserved
    env3 = tmp_path / "env3"
    original3 = (
        "DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/1/tok\n"
        "FILTER_SUBJECT_KEYWORDS=security,kernel\n"
    )
    env3.write_text(original3, encoding="utf-8")
    res3 = _run_install_fn(f'ensure_env_filter_default "{env3}" "unattended-upgrades"')
    assert res3.returncode == 0, res3.stderr
    assert env3.read_text(encoding="utf-8") == original3

    # Case 4: explicit empty override (allow all) is preserved
    env4 = tmp_path / "env4"
    original4 = (
        "DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/1/tok\nFILTER_SUBJECT_KEYWORDS=\n"
    )
    env4.write_text(original4, encoding="utf-8")
    res4 = _run_install_fn(f'ensure_env_filter_default "{env4}" "unattended-upgrades"')
    assert res4.returncode == 0, res4.stderr
    assert env4.read_text(encoding="utf-8") == original4


def test_backup_file_to_dir_keeps_apt_conf_d_clean(tmp_path: Path) -> None:
    apt_dir = tmp_path / "apt.conf.d"
    apt_dir.mkdir()
    target = apt_dir / "20auto-upgrades"
    target.write_text('APT::Periodic::Update-Package-Lists "1";\n', encoding="utf-8")
    backup_dir = tmp_path / "var_backups_mail2hook"

    res = _run_install_fn(
        f'backup_if_unmanaged "{target}" "Managed by mail2hook install.sh" "{backup_dir}"'
    )
    assert res.returncode == 0, res.stderr
    assert [p.name for p in apt_dir.iterdir()] == ["20auto-upgrades"]
    backups = list(backup_dir.glob("*.bak"))
    assert len(backups) == 1
    assert backups[0].name.endswith(".bak")


def test_wait_for_e2e_marker_statuses(tmp_path: Path) -> None:
    token = "0123456789abcdef0123456789abcdef"
    marker = tmp_path / "e2e-confirm"

    # 1. Immediate ok marker
    marker.write_text(f"ok:{token}\n", encoding="utf-8")
    res_ok = _run_install_fn(f'wait_for_e2e_marker "{marker}" "{token}" 1 0.05')
    assert res_ok.returncode == 0
    assert res_ok.stdout == "ok"

    # 2. Delayed async write simulating Postfix pipe delivery
    marker.write_text("", encoding="utf-8")
    res_async = _run_install_fn(
        f'(sleep 0.15 && printf "ok:{token}\\n" > "{marker}") &\n'
        f'wait_for_e2e_marker "{marker}" "{token}" 2 0.05\n'
        "rc=$?\n"
        "wait\n"
        "exit $rc\n"
    )
    assert res_async.returncode == 0
    assert res_async.stdout == "ok"

    # 3. Filtered marker returns exit 2
    marker.write_text(f"filtered:{token}\n", encoding="utf-8")
    res_filtered = _run_install_fn(f'wait_for_e2e_marker "{marker}" "{token}" 1 0.05')
    assert res_filtered.returncode == 2
    assert res_filtered.stdout == "filtered"

    # 4. Failed marker returns exit 3 and preserves status code
    marker.write_text(f"failed:78:{token}\n", encoding="utf-8")
    res_failed = _run_install_fn(f'wait_for_e2e_marker "{marker}" "{token}" 1 0.05')
    assert res_failed.returncode == 3
    assert res_failed.stdout == "failed:78"

    # 5. Mismatched token times out cleanly with exit 1
    marker.write_text("ok:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n", encoding="utf-8")
    res_timeout = _run_install_fn(f'wait_for_e2e_marker "{marker}" "{token}" 1 0.1')
    assert res_timeout.returncode == 1
    assert res_timeout.stdout == "timeout"


def test_e2e_unlinked_mta_fails_loudly_and_exits_nonzero() -> None:
    content = INSTALL_SH.read_text(encoding="utf-8")
    assert "NOT LINKED" in content
    assert "[ACTION REQUIRED]" in content
    assert 'if [[ "$E2E_FAILED" -ne 0 ]]; then' in content
    # Ensure direct fallback never reports "passed"
    assert "passed (direct" not in content
