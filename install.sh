#!/usr/bin/env bash
# install.sh - Automated installer for mail2hook and conservative unattended-upgrades
#
# Supported platforms:
#   - Debian 12 (bookworm)
#   - Debian 13 (trixie)
#   - Ubuntu 24.04 LTS (noble)
#   - Ubuntu 26.04 LTS (resolute)
#
# Note on security upgrades:
#   Security upgrades to mail2hook's runtime dependencies (such as system python3
#   or OpenSSL libraries) take effect for the mail2hook.service SMTP daemon on the
#   next service restart (`sudo systemctl restart mail2hook.service`). Direct
#   invocations via `/usr/sbin/sendmail` spawn a fresh process on each run.

set -euo pipefail

readonly REPO_URL="https://github.com/heliophane/mail2hook.git"
readonly API_RELEASES_URL="https://api.github.com/repos/heliophane/mail2hook/releases/latest"
readonly INSTALL_DIR="/opt/mail2hook"
readonly VENV_DIR="/opt/mail2hook/venv"
readonly CONFIG_DIR="/etc/mail2hook"
readonly ENV_FILE="/etc/mail2hook/env"
readonly SERVICE_USER="mail2hook"
readonly SERVICE_GROUP="mail2hook"
readonly SERVICE_FILE="/etc/systemd/system/mail2hook.service"

readonly APT_AUTO_UPGRADES="/etc/apt/apt.conf.d/20auto-upgrades"
readonly APT_LOCAL_UPGRADES="/etc/apt/apt.conf.d/52-unattended-upgrades-local"
readonly APT_LEGACY_MAIL2HOOK="/etc/apt/apt.conf.d/52mail2hook"
readonly TIMER_DAILY_DIR="/etc/systemd/system/apt-daily.timer.d"
readonly TIMER_DAILY_CONF="/etc/systemd/system/apt-daily.timer.d/override.conf"
readonly TIMER_UPGRADE_DIR="/etc/systemd/system/apt-daily-upgrade.timer.d"
readonly TIMER_UPGRADE_CONF="/etc/systemd/system/apt-daily-upgrade.timer.d/override.conf"

# Match Discord webhook URLs (consistent with src/mail2hook/config.py)
readonly WEBHOOK_REGEX='^https://((ptb|canary)\.)?discord(app)?\.com/api/webhooks/[0-9]+/[A-Za-z0-9_-]+/?$'

# CLI flag state
FLAG_WEBHOOK_URL=""
FLAG_SKIP_UPGRADES=0
FLAG_UNINSTALL=0
FLAG_PURGE_UPGRADES=0
FLAG_YES=0

# Detected OS variables
OS_ID=""
OS_VERSION_ID=""
OS_CODENAME=""
OS_PRETTY=""

log_info() {
    printf '[INFO] %s\n' "$*"
}

log_warn() {
    printf '[WARN] %s\n' "$*" >&2
}

log_error() {
    printf '[ERROR] %s\n' "$*" >&2
}

usage() {
    cat <<'EOF'
Usage: install.sh [OPTIONS]

Install and configure mail2hook along with conservative, security-only
unattended-upgrades on Debian 12/13 and Ubuntu 24.04/26.04.

Options:
  --webhook-url <url>   Set Discord webhook URL (or pass DISCORD_WEBHOOK_URL env var)
  --skip-upgrades       Skip installing and configuring unattended-upgrades
  --uninstall           Remove mail2hook service, user, symlinks, and install directory
  --purge-upgrades      With --uninstall, also remove unattended-upgrades drop-ins and timer overrides
  --yes, -y             Non-interactive mode; accept defaults and do not prompt
  --help, -h            Show this help message and exit

Examples:
  curl -fsSL https://raw.githubusercontent.com/heliophane/mail2hook/main/install.sh | sudo bash
  sudo ./install.sh --webhook-url "https://discord.com/api/webhooks/..." --yes
  sudo ./install.sh --uninstall
EOF
}

parse_args() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --webhook-url)
                if [[ $# -lt 2 || -z "${2:-}" ]]; then
                    log_error "--webhook-url requires a non-empty URL argument."
                    exit 1
                fi
                FLAG_WEBHOOK_URL="$2"
                shift 2
                ;;
            --webhook-url=*)
                FLAG_WEBHOOK_URL="${1#*=}"
                if [[ -z "$FLAG_WEBHOOK_URL" ]]; then
                    log_error "--webhook-url requires a non-empty URL argument."
                    exit 1
                fi
                shift
                ;;
            --skip-upgrades)
                FLAG_SKIP_UPGRADES=1
                shift
                ;;
            --uninstall)
                FLAG_UNINSTALL=1
                shift
                ;;
            --purge-upgrades)
                FLAG_PURGE_UPGRADES=1
                shift
                ;;
            --yes|-y)
                FLAG_YES=1
                shift
                ;;
            --help|-h)
                usage
                exit 0
                ;;
            *)
                log_error "Unknown argument: $1"
                usage >&2
                exit 1
                ;;
        esac
    done
}

ensure_root() {
    if [[ "${EUID:-$(id -u)}" -eq 0 ]]; then
        return 0
    fi

    if command -v sudo >/dev/null 2>&1 && [[ -f "$0" ]]; then
        log_info "Elevating privileges with sudo..."
        exec sudo -E bash "$0" "$@"
    fi

    log_error "This script must be run as root (e.g. via sudo)."
    exit 1
}

detect_os() {
    local os_release_file="/etc/os-release"
    if [[ ! -f "$os_release_file" ]]; then
        log_error "Cannot detect operating system: $os_release_file not found."
        exit 1
    fi

    # Read ID, VERSION_ID, VERSION_CODENAME, PRETTY_NAME safely
    # shellcheck disable=SC1090
    . "$os_release_file"

    OS_ID="${ID:-}"
    OS_VERSION_ID="${VERSION_ID:-}"
    OS_CODENAME="${VERSION_CODENAME:-}"
    OS_PRETTY="${PRETTY_NAME:-$OS_ID $OS_VERSION_ID}"

    case "$OS_ID:$OS_VERSION_ID" in
        debian:12)
            OS_CODENAME="${OS_CODENAME:-bookworm}"
            ;;
        debian:13)
            OS_CODENAME="${OS_CODENAME:-trixie}"
            ;;
        ubuntu:24.04)
            OS_CODENAME="${OS_CODENAME:-noble}"
            ;;
        ubuntu:26.04)
            OS_CODENAME="${OS_CODENAME:-resolute}"
            ;;
        *)
            log_error "Unsupported operating system: ${OS_PRETTY} (ID=${OS_ID}, VERSION_ID=${OS_VERSION_ID}, CODENAME=${OS_CODENAME})."
            log_error "Supported distributions: Debian 12 (bookworm), Debian 13 (trixie), Ubuntu 24.04 (noble), Ubuntu 26.04 (resolute)."
            exit 1
            ;;
    esac

    log_info "Detected supported OS: ${OS_PRETTY} (${OS_ID} ${OS_VERSION_ID}, codename: ${OS_CODENAME})"
}

is_systemd_running() {
    [[ -d /run/systemd/system ]] && command -v systemctl >/dev/null 2>&1
}

backup_if_Modified_or_existing() {
    local target="$1"
    local marker="${2:-Managed by mail2hook install.sh}"

    if [[ -f "$target" ]]; then
        if ! grep -q "$marker" "$target" 2>/dev/null; then
            local backup
            backup="${target}.bak.$(date +%Y%m%d%H%M%S)"
            cp -a "$target" "$backup"
            log_info "Backed up existing $target to $backup"
        fi
    fi
}

validate_webhook() {
    local url="${1:-}"
    # Trim leading and trailing whitespace
    url="${url#"${url%%[![:space:]]*}"}"
    url="${url%"${url##*[![:space:]]}"}"
    [[ -n "$url" && "$url" =~ $WEBHOOK_REGEX ]]
}

read_existing_webhook() {
    if [[ ! -f "$ENV_FILE" ]]; then
        return 1
    fi

    local line key val
    while IFS= read -r line || [[ -n "$line" ]]; do
        # Skip comments and empty lines
        [[ "$line" =~ ^[[:space:]]*# ]] && continue
        [[ "$line" =~ ^[[:space:]]*$ ]] && continue
        if [[ "$line" =~ ^[[:space:]]*(DISCORD_WEBHOOK_URL|DISCORD_WEBHOOK|WEBHOOK_URL)[[:space:]]*=(.*)$ ]]; then
            key="${BASH_REMATCH[1]}"
            val="${BASH_REMATCH[2]}"
            # Strip surrounding quotes and whitespace
            val="${val#"${val%%[![:space:]]*}"}"
            val="${val%"${val##*[![:space:]]}"}"
            val="${val#\"}"
            val="${val%\"}"
            val="${val#\'}"
            val="${val%\'}"
            if [[ -n "$key" ]] && validate_webhook "$val"; then
                printf '%s' "$val"
                return 0
            fi
        fi
    done < "$ENV_FILE"

    return 1
}

prompt_yes_no() {
    local prompt_msg="$1"
    local default_ans="${2:-y}"
    local reply=""

    if [[ "$FLAG_YES" -eq 1 ]]; then
        [[ "$default_ans" =~ ^[Yy] ]] && return 0 || return 1
    fi

    if [[ ! -e /dev/tty ]] || ! (: </dev/tty) 2>/dev/null; then
        [[ "$default_ans" =~ ^[Yy] ]] && return 0 || return 1
    fi

    local hint="[y/N]"
    if [[ "$default_ans" =~ ^[Yy] ]]; then
        hint="[Y/n]"
    fi

    while true; do
        printf '%s %s: ' "$prompt_msg" "$hint" >/dev/tty
        if ! IFS= read -r reply </dev/tty; then
            printf '\n' >/dev/tty
            [[ "$default_ans" =~ ^[Yy] ]] && return 0 || return 1
        fi
        reply="${reply#"${reply%%[![:space:]]*}"}"
        reply="${reply%"${reply##*[![:space:]]}"}"
        if [[ -z "$reply" ]]; then
            reply="$default_ans"
        fi
        case "$reply" in
            [Yy]|[Yy][Ee][Ss]) return 0 ;;
            [Nn]|[Nn][Oo]) return 1 ;;
            *) printf 'Please answer y or n.\n' >/dev/tty ;;
        esac
    done
}

resolve_webhook_url() {
    local candidate=""

    # 1. Explicit CLI flag or environment variable
    if [[ -n "$FLAG_WEBHOOK_URL" ]]; then
        candidate="$FLAG_WEBHOOK_URL"
    elif [[ -n "${DISCORD_WEBHOOK_URL:-}" ]]; then
        candidate="$DISCORD_WEBHOOK_URL"
    elif [[ -n "${DISCORD_WEBHOOK:-}" ]]; then
        candidate="$DISCORD_WEBHOOK"
    elif [[ -n "${WEBHOOK_URL:-}" ]]; then
        candidate="$WEBHOOK_URL"
    fi

    if [[ -n "$candidate" ]]; then
        candidate="${candidate#"${candidate%%[![:space:]]*}"}"
        candidate="${candidate%"${candidate##*[![:space:]]}"}"
        if ! validate_webhook "$candidate"; then
            log_error "Provided Discord webhook URL is invalid."
            log_error "Expected format: https://discord.com/api/webhooks/<id>/<token>"
            exit 1
        fi
        RESOLVED_WEBHOOK_URL="$candidate"
        log_info "Validated Discord webhook URL from flag/environment."
        return 0
    fi

    # 2. Check if a valid webhook already exists in /etc/mail2hook/env
    local existing_url=""
    if existing_url="$(read_existing_webhook)"; then
        if [[ "$FLAG_YES" -eq 1 ]]; then
            RESOLVED_WEBHOOK_URL="$existing_url"
            log_info "Keeping existing valid Discord webhook URL in $ENV_FILE (--yes)."
            return 0
        fi

        if prompt_yes_no "A valid Discord webhook URL already exists in $ENV_FILE. Keep existing webhook?" "y"; then
            RESOLVED_WEBHOOK_URL="$existing_url"
            log_info "Keeping existing Discord webhook URL."
            return 0
        fi
    fi

    # 3. Interactive prompt via /dev/tty
    if [[ "$FLAG_YES" -eq 1 ]]; then
        log_error "Non-interactive mode (--yes) set, but no Discord webhook URL was provided via --webhook-url, DISCORD_WEBHOOK_URL, or $ENV_FILE."
        exit 1
    fi

    if [[ ! -e /dev/tty ]] || ! (: </dev/tty) 2>/dev/null; then
        log_error "No controlling terminal (/dev/tty) available to prompt for Discord webhook URL."
        log_error "Pass --webhook-url <url> or set DISCORD_WEBHOOK_URL in the environment."
        exit 1
    fi

    local input_url=""
    while true; do
        printf 'Enter Discord Webhook URL (input is hidden): ' >/dev/tty
        if ! IFS= read -r -s input_url </dev/tty; then
            printf '\n' >/dev/tty
            log_error "Failed to read Discord webhook URL from /dev/tty."
            exit 1
        fi
        printf '\n' >/dev/tty

        input_url="${input_url#"${input_url%%[![:space:]]*}"}"
        input_url="${input_url%"${input_url##*[![:space:]]}"}"

        if validate_webhook "$input_url"; then
            RESOLVED_WEBHOOK_URL="$input_url"
            log_info "Discord webhook URL validated."
            return 0
        fi

        printf '[WARN] Invalid Discord webhook format. Expected: https://discord.com/api/webhooks/<id>/<token>\n' >/dev/tty
    done
}

install_system_packages() {
    export DEBIAN_FRONTEND=noninteractive

    log_info "Updating APT package lists..."
    apt-get update -qq

    local -a pkgs=(
        python3
        python3-venv
        git
        ca-certificates
        curl
    )
    if [[ "$FLAG_SKIP_UPGRADES" -eq 0 ]]; then
        pkgs+=(unattended-upgrades)
        # Debian's unattended-upgrade invokes lsb_release at import time
        if [[ "$OS_ID" == "debian" ]]; then
            pkgs+=(lsb-release)
        fi
    fi

    log_info "Installing required packages: ${pkgs[*]}"
    apt-get install -y -qq --no-install-recommends "${pkgs[@]}"

    # Verify Python >= 3.11 (required by pyproject.toml requires-python = ">=3.11")
    if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
        local py_ver
        py_ver="$(python3 -c 'import platform; print(platform.python_version())' 2>/dev/null || echo 'unknown')"
        log_error "Python >= 3.11 is required by mail2hook, but found Python ${py_ver}."
        exit 1
    fi

    # Remove conflicting MTAs so /usr/sbin/sendmail and port 25 are free
    local -a purge_pkgs=()
    local -a candidate_mtas=(postfix bsd-mailx)
    if [[ "$OS_ID" == "debian" ]]; then
        candidate_mtas+=(exim4 exim4-base exim4-config exim4-daemon-light exim4-daemon-heavy)
    fi

    local pkg
    for pkg in "${candidate_mtas[@]}"; do
        if dpkg-query -W -f='${Status}' "$pkg" 2>/dev/null | grep -q "install ok installed"; then
            purge_pkgs+=("$pkg")
        fi
    done

    if [[ "${#purge_pkgs[@]}" -gt 0 ]]; then
        log_info "Stopping and purging conflicting MTA packages: ${purge_pkgs[*]}"
        if is_systemd_running; then
            systemctl stop exim4 postfix 2>/dev/null || true
        fi
        apt-get purge -y -qq "${purge_pkgs[@]}" || true
    fi
}

ensure_service_user() {
    if ! getent group "$SERVICE_GROUP" >/dev/null 2>&1; then
        log_info "Creating system group '$SERVICE_GROUP'..."
        groupadd --system "$SERVICE_GROUP"
    fi

    if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
        local nologin_shell="/usr/sbin/nologin"
        [[ ! -x "$nologin_shell" && -x "/sbin/nologin" ]] && nologin_shell="/sbin/nologin"
        log_info "Creating unprivileged system user '$SERVICE_USER'..."
        useradd --system \
            --gid "$SERVICE_GROUP" \
            --home-dir "$INSTALL_DIR" \
            --no-create-home \
            --shell "$nologin_shell" \
            --comment "mail2hook service user" \
            "$SERVICE_USER"
    fi
}

detect_local_checkout() {
    local script_dir=""
    if [[ -n "${BASH_SOURCE[0]:-}" && -f "${BASH_SOURCE[0]}" ]]; then
        script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    fi

    if [[ -n "$script_dir" && -f "$script_dir/pyproject.toml" && -d "$script_dir/src/mail2hook" ]]; then
        if grep -q 'name = "mail2hook"' "$script_dir/pyproject.toml" 2>/dev/null; then
            printf '%s' "$script_dir"
            return 0
        fi
    fi

    if [[ -f "./pyproject.toml" && -d "./src/mail2hook" ]]; then
        if grep -q 'name = "mail2hook"' "./pyproject.toml" 2>/dev/null; then
            pwd
            return 0
        fi
    fi

    return 1
}

resolve_github_source() {
    # Prefer a tagged release if the project has releases on GitHub; otherwise use default branch
    local tag=""
    if command -v curl >/dev/null 2>&1; then
        tag="$(curl -fsSL --connect-timeout 5 --max-time 10 "$API_RELEASES_URL" 2>/dev/null \
            | sed -n 's/.*"tag_name":[[:space:]]*"\([^"]*\)".*/\1/p' \
            | head -n 1 || true)"
    fi

    if [[ -n "$tag" ]]; then
        printf 'git+%s@%s' "$REPO_URL" "$tag"
    else
        printf 'git+%s' "$REPO_URL"
    fi
}

install_mail2hook_venv() {
    install -d -m 0755 -o root -g root "$INSTALL_DIR"

    if [[ ! -x "$VENV_DIR/bin/python" ]]; then
        log_info "Creating Python virtual environment at $VENV_DIR..."
        python3 -m venv "$VENV_DIR"
    fi

    log_info "Upgrading pip in virtual environment..."
    "$VENV_DIR/bin/pip" install --quiet --upgrade pip

    local local_checkout=""
    if local_checkout="$(detect_local_checkout)"; then
        log_info "Installing mail2hook from local checkout ($local_checkout)..."
        local build_tmp
        build_tmp="$(mktemp -d)"
        cp -a "$local_checkout/pyproject.toml" "$local_checkout/README.md" "$local_checkout/src" "$build_tmp/"
        rm -rf "$build_tmp/src/mail2hook.egg-info"
        "$VENV_DIR/bin/pip" install --quiet --upgrade "$build_tmp"
        rm -rf "$build_tmp"
    else
        local remote_spec
        remote_spec="$(resolve_github_source)"
        log_info "Installing mail2hook from $remote_spec..."
        "$VENV_DIR/bin/pip" install --quiet --upgrade "$remote_spec"
    fi

    chown -R root:root "$INSTALL_DIR"

    # Create standard CLI and sendmail/mailq drop-in symlinks
    install -d -m 0755 /usr/local/bin /usr/sbin /usr/bin
    ln -sf "$VENV_DIR/bin/mail2hook" /usr/local/bin/mail2hook
    ln -sf "$VENV_DIR/bin/mail2hook" /usr/sbin/sendmail
    ln -sf "$VENV_DIR/bin/mail2hook" /usr/bin/mailq
    log_info "Linked mail2hook to /usr/local/bin/mail2hook, /usr/sbin/sendmail, and /usr/bin/mailq"
}

write_env_file() {
    install -d -m 0750 -o root -g "$SERVICE_GROUP" "$CONFIG_DIR"

    local tmp_env
    tmp_env="$(mktemp "${CONFIG_DIR}/.env.tmp.XXXXXX")"
    chown root:"$SERVICE_GROUP" "$tmp_env"
    chmod 0640 "$tmp_env"

    if [[ -f "$ENV_FILE" ]]; then
        # Preserve existing non-webhook settings and comments, updating DISCORD_WEBHOOK_URL in place
        local updated=0
        local line
        while IFS= read -r line || [[ -n "$line" ]]; do
            if [[ "$line" =~ ^[[:space:]]*(DISCORD_WEBHOOK_URL|DISCORD_WEBHOOK|WEBHOOK_URL)[[:space:]]*= ]]; then
                if [[ "$updated" -eq 0 ]]; then
                    printf 'DISCORD_WEBHOOK_URL=%s\n' "$RESOLVED_WEBHOOK_URL" >> "$tmp_env"
                    updated=1
                fi
            else
                printf '%s\n' "$line" >> "$tmp_env"
            fi
        done < "$ENV_FILE"

        if [[ "$updated" -eq 0 ]]; then
            printf '\nDISCORD_WEBHOOK_URL=%s\n' "$RESOLVED_WEBHOOK_URL" >> "$tmp_env"
        fi
    else
        cat > "$tmp_env" <<EOF
# /etc/mail2hook/env - Configuration for mail2hook
# Owned by root:${SERVICE_GROUP} (mode 0640) so both root sendmail invocations
# and the unprivileged ${SERVICE_USER} systemd service can read it.

# Required: Discord webhook URL
DISCORD_WEBHOOK_URL=${RESOLVED_WEBHOOK_URL}

# Optional: Friendly server name display in Discord (defaults to system hostname)
# SERVER_NAME=

# Optional: Comma-separated sender addresses or domains to match. If unset, all senders match.
# FILTER_SENDERS=

# Optional: Comma-separated subject keywords to match (case-insensitive). If unset, all subjects match.
# FILTER_SUBJECT_KEYWORDS=

# Optional: Maximum body characters to include in the Discord embed excerpt (default: 1500)
# MAX_BODY_CHARS=1500

# Optional: Bot display name in Discord (default: mail2hook)
# DISCORD_USERNAME=mail2hook
EOF
    fi

    chown root:"$SERVICE_GROUP" "$tmp_env"
    chmod 0640 "$tmp_env"
    mv -f "$tmp_env" "$ENV_FILE"
    log_info "Secured configuration file at $ENV_FILE (root:${SERVICE_GROUP}, mode 0640)"
}

install_systemd_service() {
    backup_if_Modified_or_existing "$SERVICE_FILE" "Managed by mail2hook install.sh"

    cat > "$SERVICE_FILE" <<EOF
# Managed by mail2hook install.sh
# Note: Security upgrades to mail2hook's runtime dependencies (such as python3 or libssl)
# take effect for this daemon on the next service restart:
#   sudo systemctl restart mail2hook.service
[Unit]
Description=mail2hook Lightweight SMTP Daemon
Documentation=https://github.com/heliophane/mail2hook
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${SERVICE_USER}
Group=${SERVICE_GROUP}
ExecStart=${VENV_DIR}/bin/mail2hook --smtp-listen 127.0.0.1:25
EnvironmentFile=${ENV_FILE}
Restart=on-failure
RestartSec=5s

# Bind privileged port 25 without running as root
AmbientCapabilities=CAP_NET_BIND_SERVICE
CapabilityBoundingSet=CAP_NET_BIND_SERVICE

# Hardening options
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
PrivateDevices=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictSUIDSGID=true
LockPersonality=true
UMask=0077

[Install]
WantedBy=multi-user.target
EOF

    chmod 0644 "$SERVICE_FILE"
    log_info "Wrote systemd unit file to $SERVICE_FILE"

    if is_systemd_running; then
        systemctl daemon-reload
        systemctl enable mail2hook.service
        systemctl restart mail2hook.service
        log_info "Enabled and started mail2hook.service"
    else
        log_warn "systemd is not running as PID 1 (container environment); skipped starting mail2hook.service."
    fi
}

ubuntu_esm_security_present() {
    # Include Ubuntu ESM security origins only if Ubuntu Pro / ESM sources are present on this machine
    local f
    for f in /etc/apt/sources.list /etc/apt/sources.list.d/*.list /etc/apt/sources.list.d/*.sources; do
        [[ -f "$f" ]] || continue
        if grep -Eq '^[[:space:]]*[^#].*esm\.ubuntu\.com' "$f" 2>/dev/null; then
            return 0
        fi
    done
    return 1
}

configure_unattended_upgrades() {
    if [[ "$FLAG_SKIP_UPGRADES" -eq 1 ]]; then
        log_info "Skipping unattended-upgrades configuration (--skip-upgrades)."
        return 0
    fi

    log_info "Configuring conservative security-only unattended-upgrades..."

    # Remove legacy 52mail2hook if present so settings are consolidated cleanly
    if [[ -f "$APT_LEGACY_MAIL2HOOK" ]]; then
        backup_if_Modified_or_existing "$APT_LEGACY_MAIL2HOOK" "Default unattended-upgrades configuration for mail2hook"
        rm -f "$APT_LEGACY_MAIL2HOOK"
    fi

    backup_if_Modified_or_existing "$APT_AUTO_UPGRADES" "Managed by mail2hook install.sh"
    cat > "$APT_AUTO_UPGRADES" <<'EOF'
// Managed by mail2hook install.sh
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
APT::Periodic::AutocleanInterval "7";
EOF
    chmod 0644 "$APT_AUTO_UPGRADES"

    backup_if_Modified_or_existing "$APT_LOCAL_UPGRADES" "Managed by mail2hook install.sh"

    # Build distro-specific security-only origin blocks.
    # Both Origins-Pattern and Allowed-Origins are cleared first with #clear so
    # non-security origins from 50unattended-upgrades (e.g. o=Debian,n=<codename>,l=Debian
    # or Ubuntu:<codename>) are replaced rather than appended to.
    local origins_block=""
    if [[ "$OS_ID" == "debian" ]]; then
        origins_block=$(cat <<'EOF'
#clear Unattended-Upgrade::Origins-Pattern;
#clear Unattended-Upgrade::Allowed-Origins;
Unattended-Upgrade::Origins-Pattern {
    "origin=Debian,codename=${distro_codename}-security,label=Debian-Security";
    "origin=Debian,codename=${distro_codename},label=Debian-Security";
};
EOF
)
    elif [[ "$OS_ID" == "ubuntu" ]]; then
        local esm_lines=""
        if ubuntu_esm_security_present; then
            log_info "Detected active Ubuntu ESM sources; including security-only ESM origins."
            esm_lines=$(cat <<'EOF'

    "${distro_id}ESMApps:${distro_codename}-apps-security";
    "${distro_id}ESM:${distro_codename}-infra-security";
EOF
)
        fi
        origins_block=$(cat <<EOF
#clear Unattended-Upgrade::Allowed-Origins;
#clear Unattended-Upgrade::Origins-Pattern;
Unattended-Upgrade::Allowed-Origins {
    "\${distro_id}:\${distro_codename}-security";${esm_lines}
};
EOF
)
    fi

    cat > "$APT_LOCAL_UPGRADES" <<EOF
// Managed by mail2hook install.sh
// Security-only unattended-upgrades drop-in configuration.
// Note: Security upgrades to mail2hook's dependencies take effect for the
// mail2hook.service daemon on the next service restart.

${origins_block}

// Deliver upgrade notifications to mail2hook via /usr/sbin/sendmail
Unattended-Upgrade::Mail "root";
Unattended-Upgrade::MailReport "always";
Unattended-Upgrade::Sender "unattended-upgrades";

// Never reboot automatically under any circumstance; mail2hook reports pending reboots in Discord
Unattended-Upgrade::Automatic-Reboot "false";
Unattended-Upgrade::Automatic-Reboot-WithUsers "false";

// Conservative "no surprises" removal policy:
// Left set to "false" so an unattended security run never auto-removes packages
// or running/fallback kernels that local scripts or administrators might still rely on.
Unattended-Upgrade::Remove-Unused-Dependencies "false";
Unattended-Upgrade::Remove-Unused-Kernel-Packages "false";
EOF
    chmod 0644 "$APT_LOCAL_UPGRADES"
    log_info "Wrote APT periodic and security-only drop-ins ($APT_AUTO_UPGRADES, $APT_LOCAL_UPGRADES)"

    # Configure nightly timer overrides in /etc/systemd/system/<unit>.d/override.conf
    install -d -m 0755 "$TIMER_DAILY_DIR" "$TIMER_UPGRADE_DIR"
    backup_if_Modified_or_existing "$TIMER_DAILY_CONF" "Managed by mail2hook install.sh"
    backup_if_Modified_or_existing "$TIMER_UPGRADE_CONF" "Managed by mail2hook install.sh"

    cat > "$TIMER_DAILY_CONF" <<'EOF'
# Managed by mail2hook install.sh
# Run package list updates and downloads nightly between 01:00 and 02:00 local time
[Timer]
OnCalendar=
OnCalendar=*-*-* 01:00
RandomizedDelaySec=60m
Persistent=true
EOF
    chmod 0644 "$TIMER_DAILY_CONF"

    cat > "$TIMER_UPGRADE_CONF" <<'EOF'
# Managed by mail2hook install.sh
# Install security upgrades nightly between 03:00 and 04:00 local time
[Timer]
OnCalendar=
OnCalendar=*-*-* 03:00
RandomizedDelaySec=60m
Persistent=true
EOF
    chmod 0644 "$TIMER_UPGRADE_CONF"
    log_info "Wrote systemd timer overrides ($TIMER_DAILY_CONF, $TIMER_UPGRADE_CONF)"

    if is_systemd_running; then
        systemctl daemon-reload
        systemctl enable apt-daily.timer apt-daily-upgrade.timer >/dev/null 2>&1 || true
        systemctl restart apt-daily.timer apt-daily-upgrade.timer
        log_info "Reloaded systemd and restarted apt-daily.timer & apt-daily-upgrade.timer"
    fi

    verify_unattended_upgrades
}

verify_unattended_upgrades() {
    log_info "Verifying unattended-upgrades configuration (--dry-run --debug)..."
    local dry_output=""
    if dry_output="$(unattended-upgrade --dry-run --debug 2>&1)"; then
        local origins_line=""
        origins_line="$(printf '%s\n' "$dry_output" | grep -i "Allowed origins are:" | head -n 1 || true)"
        if [[ -n "$origins_line" ]]; then
            log_info "Effective origins -> $origins_line"
        else
            log_info "unattended-upgrade dry-run completed."
        fi
    else
        log_warn "unattended-upgrade --dry-run --debug returned non-zero (can occur in minimal container environments):"
        printf '%s\n' "$dry_output" | grep -i "Allowed origins are:" || true
    fi

    if is_systemd_running; then
        log_info "Active APT systemd timer schedule:"
        systemctl list-timers --no-pager apt-daily.timer apt-daily-upgrade.timer || true
    else
        log_info "Configured timer schedules (systemd not active in container):"
        log_info "  apt-daily.timer:         OnCalendar=*-*-* 01:00 (RandomizedDelaySec=60m, Persistent=true)"
        log_info "  apt-daily-upgrade.timer: OnCalendar=*-*-* 03:00 (RandomizedDelaySec=60m, Persistent=true)"
    fi
}

detect_timezone() {
    local tz=""
    if command -v timedatectl >/dev/null 2>&1 && is_systemd_running; then
        tz="$(timedatectl show -p Timezone --value 2>/dev/null || true)"
    fi
    if [[ -z "$tz" && -f /etc/timezone ]]; then
        tz="$(tr -d '[:space:]' < /etc/timezone || true)"
    fi
    if [[ -z "$tz" && -L /etc/localtime ]]; then
        local link_target
        link_target="$(readlink /etc/localtime || true)"
        tz="${link_target#*zoneinfo/}"
    fi
    local abbr
    abbr="$(date +%Z 2>/dev/null || echo 'UTC')"
    if [[ -n "$tz" ]]; then
        printf '%s (%s)' "$tz" "$abbr"
    else
        printf '%s' "$abbr"
    fi
}

print_summary() {
    local tz_info
    tz_info="$(detect_timezone)"
    local svc_state="inactive (systemd not running)"
    if is_systemd_running; then
        svc_state="$(systemctl is-active mail2hook.service 2>/dev/null || echo 'unknown')"
    fi

    printf '\n============================================================\n'
    printf 'mail2hook Installation Summary\n'
    printf '============================================================\n'
    printf '  OS Detected      : %s (%s)\n' "$OS_PRETTY" "$OS_CODENAME"
    printf '  Server Timezone  : %s\n' "$tz_info"
    printf '  Install Path     : %s\n' "$VENV_DIR"
    printf '  Sendmail Drop-in : /usr/sbin/sendmail -> %s/bin/mail2hook\n' "$VENV_DIR"
    printf '  Config File      : %s (root:%s, 0640)\n' "$ENV_FILE" "$SERVICE_GROUP"
    printf '  SMTP Service     : mail2hook.service (%s, listening on 127.0.0.1:25)\n' "$svc_state"
    if [[ "$FLAG_SKIP_UPGRADES" -eq 0 ]]; then
        printf '  Auto-Upgrades    : Security updates only (Automatic-Reboot: false)\n'
        printf '  Download Window  : 01:00 - 02:00 local time (%s)\n' "$tz_info"
        printf '  Upgrade Window   : 03:00 - 04:00 local time (%s)\n' "$tz_info"
    else
        printf '  Auto-Upgrades    : Skipped (--skip-upgrades)\n'
    fi
    printf '\nUseful Commands:\n'
    printf '  Service status   : sudo systemctl status mail2hook.service\n'
    printf '  Service logs     : sudo journalctl -u mail2hook.service -f\n'
    printf '  Upgrade logs     : sudo tail -n 50 /var/log/unattended-upgrades/unattended-upgrades.log\n'
    printf '  Test dry-run     : printf "Subject: Test\\nFrom: root\\n\\nHello\\n" | sudo mail2hook --dry-run\n'
    printf '============================================================\n'

    if [[ -f /var/run/reboot-required || -f /run/reboot-required ]]; then
        printf '[NOTICE] A system reboot is currently required (/var/run/reboot-required exists).\n'
        printf '         This script will NOT reboot automatically. Run "sudo reboot" when ready.\n'
    fi
}

run_uninstall() {
    log_info "Uninstalling mail2hook..."

    if is_systemd_running; then
        systemctl stop mail2hook.service 2>/dev/null || true
        systemctl disable mail2hook.service 2>/dev/null || true
    fi

    if [[ -f "$SERVICE_FILE" ]]; then
        rm -f "$SERVICE_FILE"
        log_info "Removed $SERVICE_FILE"
        if is_systemd_running; then
            systemctl daemon-reload
        fi
    fi

    # Remove symlinks only if they point into /opt/mail2hook
    local link target
    for link in /usr/local/bin/mail2hook /usr/sbin/sendmail /usr/bin/mailq; do
        if [[ -L "$link" ]]; then
            target="$(readlink "$link" || true)"
            if [[ "$target" == "$INSTALL_DIR"* ]]; then
                rm -f "$link"
                log_info "Removed symlink $link"
            fi
        fi
    done

    if [[ -d "$INSTALL_DIR" ]]; then
        rm -rf "$INSTALL_DIR"
        log_info "Removed installation directory $INSTALL_DIR"
    fi

    if id -u "$SERVICE_USER" >/dev/null 2>&1; then
        userdel "$SERVICE_USER" 2>/dev/null || true
        log_info "Removed system user '$SERVICE_USER'"
    fi

    if getent group "$SERVICE_GROUP" >/dev/null 2>&1; then
        groupdel "$SERVICE_GROUP" 2>/dev/null || true
        log_info "Removed system group '$SERVICE_GROUP'"
    fi

    if [[ -d "$CONFIG_DIR" || -f "$ENV_FILE" ]]; then
        if prompt_yes_no "Delete configuration and secret webhook file in $CONFIG_DIR?" "n"; then
            rm -rf "$CONFIG_DIR"
            log_info "Removed configuration directory $CONFIG_DIR"
        else
            log_info "Kept configuration directory $CONFIG_DIR"
        fi
    fi

    if [[ "$FLAG_PURGE_UPGRADES" -eq 1 ]]; then
        log_info "Purging unattended-upgrades drop-ins and timer overrides (--purge-upgrades)..."
        rm -f "$APT_LOCAL_UPGRADES" "$APT_AUTO_UPGRADES" "$APT_LEGACY_MAIL2HOOK"
        rm -f "$TIMER_DAILY_CONF" "$TIMER_UPGRADE_CONF"
        rmdir --ignore-fail-on-non-empty "$TIMER_DAILY_DIR" "$TIMER_UPGRADE_DIR" 2>/dev/null || true
        if is_systemd_running; then
            systemctl daemon-reload
            systemctl restart apt-daily.timer apt-daily-upgrade.timer 2>/dev/null || true
        fi
        log_info "Removed unattended-upgrades local drop-ins and timer overrides."
    else
        log_info "Left unattended-upgrades configuration in place (pass --purge-upgrades to remove)."
    fi

    log_info "Uninstall complete."
}

main() {
    parse_args "$@"
    ensure_root "$@"
    detect_os

    if [[ "$FLAG_UNINSTALL" -eq 1 ]]; then
        run_uninstall
        return 0
    fi

    RESOLVED_WEBHOOK_URL=""
    resolve_webhook_url
    install_system_packages
    ensure_service_user
    install_mail2hook_venv
    write_env_file
    install_systemd_service
    configure_unattended_upgrades
    print_summary
}

main "$@"
