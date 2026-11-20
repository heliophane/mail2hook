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


def test_append_and_strip_mail_recipient(tmp_path: Path) -> None:
    apt_file = tmp_path / "52-unattended-upgrades-local"
    backup_dir = tmp_path / "backups"
    apt_file.write_text(
        'Unattended-Upgrade::Mail "root, mail2hook@localhost";\n'
        'Unattended-Upgrade::MailReport "on-change";\n',
        encoding="utf-8",
    )

    res = _run_install_fn(
        "\n".join(
            [
                'echo "a=$(append_mail_recipient "" "mail2hook@localhost")"',
                'echo "b=$(append_mail_recipient "root" "mail2hook@localhost")"',
                'echo "c=$(append_mail_recipient "admin@example.com" "mail2hook@localhost")"',
                (
                    'echo "d=$(append_mail_recipient "root, mail2hook@localhost" '
                    '"mail2hook@localhost")"'
                ),
                (
                    'echo "e=$(strip_mail_recipient "root, mail2hook@localhost" '
                    '"mail2hook@localhost")"'
                ),
                'echo "f=$(strip_mail_recipient "mail2hook@localhost" "mail2hook@localhost")"',
                f'remove_apt_mail_recipient "{apt_file}" "mail2hook@localhost" "{backup_dir}"',
            ]
        )
    )
    assert res.returncode == 0, res.stderr
    lines = res.stdout.splitlines()
    assert lines[:6] == [
        "a=root, mail2hook@localhost",
        "b=root, mail2hook@localhost",
        "c=admin@example.com, mail2hook@localhost",
        "d=root, mail2hook@localhost",
        "e=root",
        "f=root",
    ]
    assert 'Unattended-Upgrade::Mail "root";' in apt_file.read_text(encoding="utf-8")
    assert len(list(backup_dir.glob("*.bak"))) == 1


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
