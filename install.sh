#!/usr/bin/env bash
# install.sh - Automated installer for mail2hook and conservative unattended-upgrades
#
# Supported platforms:
#   - Debian 12 (bookworm)
#   - Debian 13 (trixie)
#   - Ubuntu 24.04 LTS (noble)
#   - Ubuntu 26.04 LTS (resolute)

set -euo pipefail
umask 022

readonly REPO_URL="https://github.com/heliophane/mail2hook"
readonly API_RELEASES_URL="https://api.github.com/repos/heliophane/mail2hook/releases/latest"
readonly BIN_PATH="/usr/local/bin/mail2hook"
readonly SENDMAIL_PATH="/usr/sbin/sendmail"
readonly MAILQ_PATH="/usr/bin/mailq"
readonly CONFIG_DIR="/etc/mail2hook"
readonly ENV_FILE="/etc/mail2hook/env"
readonly BACKUP_DIR="/var/backups/mail2hook"

# Postfix pipe integration paths and identifiers
readonly POSTFIX_MAIN_CF="/etc/postfix/main.cf"
readonly POSTFIX_MASTER_CF="/etc/postfix/master.cf"
readonly POSTFIX_TRANSPORT_FILE="/etc/postfix/mail2hook_transport"
readonly POSTFIX_TRANSPORT_NAME="mail2hook"
readonly POSTFIX_RECIPIENT="mail2hook@localhost"
readonly POSTFIX_PIPE_USER="nobody"
readonly POSTFIX_MASTER_BEGIN="# BEGIN mail2hook managed block"
readonly POSTFIX_MASTER_END="# END mail2hook managed block"

# Legacy layout paths from earlier service-based versions (used for clean migration)
readonly LEGACY_INSTALL_DIR="/opt/mail2hook"
readonly LEGACY_VENV_DIR="/opt/mail2hook/venv"
readonly LEGACY_SERVICE_USER="mail2hook"
readonly LEGACY_SERVICE_GROUP="mail2hook"
readonly LEGACY_SERVICE_FILE="/etc/systemd/system/mail2hook.service"
readonly LEGACY_SERVICE_WANT="/etc/systemd/system/multi-user.target.wants/mail2hook.service"

readonly APT_AUTO_UPGRADES="/etc/apt/apt.conf.d/20auto-upgrades"
readonly APT_LOCAL_UPGRADES="/etc/apt/apt.conf.d/52-unattended-upgrades-local"
readonly APT_LEGACY_MAIL2HOOK="/etc/apt/apt.conf.d/52mail2hook"
readonly APT_MAIL_ORIG_BACKUP="${BACKUP_DIR}/apt_mail_target.orig"
readonly DEFAULT_SUBJECT_FILTER="unattended-upgrades"
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

# Runtime state
OS_ID=""
OS_VERSION_ID=""
OS_CODENAME=""
OS_PRETTY=""
ENV_GROUP="mail"
RESOLVED_WEBHOOK_URL=""
SENDMAIL_LINKED=0
INSTALL_MODE=""
DETECTED_MTAS=""
UPGRADE_MAIL_TARGET="root"
PREVIOUS_UPGRADE_MAIL_TARGET=""
ACTIVE_SUBJECT_FILTER=""
E2E_STATUS="not run"

declare -a CLEANUP_PATHS=()
declare -a CHANGES_MADE=()
declare -a BACKUPS_CREATED=()

register_cleanup_path() {
    CLEANUP_PATHS+=("$1")
}

cleanup_temp_files() {
    local p
    for p in "${CLEANUP_PATHS[@]+"${CLEANUP_PATHS[@]}"}"; do
        if [[ -n "$p" && -e "$p" ]]; then
            rm -rf "$p" 2>/dev/null || true
        fi
    done
}

trap cleanup_temp_files EXIT INT TERM

record_change() {
    CHANGES_MADE+=("$1")
}

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

Install mail2hook and configure conservative, security-only unattended-upgrades
on Debian 12/13 and Ubuntu 24.04/26.04. When Postfix is installed, mail2hook
integrates via a dedicated pipe transport without replacing /usr/sbin/sendmail.

Options:
  --skip-upgrades       Skip installing and configuring unattended-upgrades
  --uninstall           Remove mail2hook, Postfix integration, and sendmail alternative
  --purge-upgrades      With --uninstall, also remove unattended-upgrades drop-ins and timer overrides
  --yes, -y             Non-interactive mode; accept defaults and do not prompt
  --help, -h            Show this help message and exit

Environment:
  DISCORD_WEBHOOK_URL   Discord webhook URL for non-interactive setup (avoids CLI secrets)

Examples:
  curl -fsSL https://raw.githubusercontent.com/heliophane/mail2hook/main/install.sh | sudo bash
  export DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..."
  sudo --preserve-env=DISCORD_WEBHOOK_URL bash ./install.sh --yes
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
                log_warn "--webhook-url is deprecated and exposes secrets in process listings (ps); pass DISCORD_WEBHOOK_URL via environment or pre-create $ENV_FILE instead."
                FLAG_WEBHOOK_URL="$2"
                shift 2
                ;;
            --webhook-url=*)
                FLAG_WEBHOOK_URL="${1#*=}"
                if [[ -z "$FLAG_WEBHOOK_URL" ]]; then
                    log_error "--webhook-url requires a non-empty URL argument."
                    exit 1
                fi
                log_warn "--webhook-url is deprecated and exposes secrets in process listings (ps); pass DISCORD_WEBHOOK_URL via environment or pre-create $ENV_FILE instead."
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

    if command -v sudo >/dev/null 2>&1 \
        && [[ -n "${BASH_SOURCE[0]:-}" && "${BASH_SOURCE[0]}" != "bash" && -f "${BASH_SOURCE[0]}" ]]; then
        log_info "Elevating privileges with sudo..."
        if [[ -n "${DISCORD_WEBHOOK_URL:-}" || -n "${DISCORD_WEBHOOK:-}" || -n "${WEBHOOK_URL:-}" ]]; then
            exec sudo --preserve-env=DISCORD_WEBHOOK_URL,DISCORD_WEBHOOK,WEBHOOK_URL bash "${BASH_SOURCE[0]}" "$@"
        fi
        exec sudo bash "${BASH_SOURCE[0]}" "$@"
    fi

    log_error "This script must be run as root (e.g. via sudo)."
    exit 1
}

resolve_env_group() {
    if getent group mail >/dev/null 2>&1; then
        ENV_GROUP="mail"
    elif getent group mail2hook >/dev/null 2>&1; then
        ENV_GROUP="mail2hook"
    elif [[ "$FLAG_UNINSTALL" -eq 0 ]] && command -v groupadd >/dev/null 2>&1; then
        groupadd --system mail2hook
        ENV_GROUP="mail2hook"
        record_change "Created system group 'mail2hook' for $ENV_FILE access"
    else
        ENV_GROUP="root"
    fi
}

detect_os() {
    local os_release_file="/etc/os-release"
    if [[ ! -f "$os_release_file" ]]; then
        log_error "Cannot detect operating system: $os_release_file not found."
        exit 1
    fi

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

backup_file_to_dir() {
    local target="$1"
    local backup_dir="${2:-$BACKUP_DIR}"
    if [[ ! -f "$target" ]]; then
        return 0
    fi

    install -d -m 0750 -o root -g root "$backup_dir" 2>/dev/null || mkdir -p "$backup_dir"
    local safe_name ts backup_path
    safe_name="${target#/}"
    safe_name="${safe_name//\//_}"
    ts="$(date +%Y%m%d%H%M%S)"
    backup_path="${backup_dir}/${safe_name}.${ts}.bak"
    if [[ -e "$backup_path" ]]; then
        local suffix=1
        while [[ -e "${backup_dir}/${safe_name}.${ts}.${suffix}.bak" ]]; do
            suffix=$((suffix + 1))
        done
        backup_path="${backup_dir}/${safe_name}.${ts}.${suffix}.bak"
    fi
    cp -a "$target" "$backup_path"
    BACKUPS_CREATED+=("$backup_path")
    log_info "Backed up $target to $backup_path"
}

backup_if_unmanaged() {
    local target="$1"
    local marker="${2:-Managed by mail2hook install.sh}"
    local backup_dir="${3:-$BACKUP_DIR}"

    if [[ -f "$target" ]]; then
        if ! grep -Fq "$marker" "$target" 2>/dev/null; then
            backup_file_to_dir "$target" "$backup_dir"
        fi
    fi
}

validate_webhook() {
    local url="${1:-}"
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
        [[ "$line" =~ ^[[:space:]]*# ]] && continue
        [[ "$line" =~ ^[[:space:]]*$ ]] && continue
        if [[ "$line" =~ ^[[:space:]]*(export[[:space:]]+)?(DISCORD_WEBHOOK_URL|DISCORD_WEBHOOK|WEBHOOK_URL)[[:space:]]*=(.*)$ ]]; then
            key="${BASH_REMATCH[2]}"
            val="${BASH_REMATCH[3]}"
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
        elif [[ "$line" =~ ^[[:space:]]*(https://[^[:space:]]+)$ ]]; then
            val="${BASH_REMATCH[1]}"
            if validate_webhook "$val"; then
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
        if [[ "$default_ans" =~ ^[Yy] ]]; then
            return 0
        fi
        return 1
    fi

    if [[ ! -e /dev/tty ]] || ! (: </dev/tty) 2>/dev/null; then
        if [[ "$default_ans" =~ ^[Yy] ]]; then
            return 0
        fi
        return 1
    fi

    local hint="[y/N]"
    if [[ "$default_ans" =~ ^[Yy] ]]; then
        hint="[Y/n]"
    fi

    while true; do
        printf '%s %s: ' "$prompt_msg" "$hint" >/dev/tty
        if ! IFS= read -r reply </dev/tty; then
            printf '\n' >/dev/tty
            if [[ "$default_ans" =~ ^[Yy] ]]; then
                return 0
            fi
            return 1
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

    # 2. Keep an existing valid webhook in /etc/mail2hook/env unless told to replace it
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

    # 3. Interactive prompt via /dev/tty with hidden input
    if [[ "$FLAG_YES" -eq 1 ]]; then
        log_error "Non-interactive mode (--yes) set, but no Discord webhook URL was provided via DISCORD_WEBHOOK_URL or $ENV_FILE."
        exit 1
    fi

    if [[ ! -e /dev/tty ]] || ! (: </dev/tty) 2>/dev/null; then
        log_error "No controlling terminal (/dev/tty) available to prompt for Discord webhook URL."
        log_error "Set DISCORD_WEBHOOK_URL in the environment (e.g. sudo --preserve-env=DISCORD_WEBHOOK_URL ...) or pre-create $ENV_FILE."
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

    log_info "Installing minimal required packages: ${pkgs[*]}"
    apt-get install -y -qq --no-install-recommends "${pkgs[@]}"

    if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
        local py_ver
        py_ver="$(python3 -c 'import platform; print(platform.python_version())' 2>/dev/null || echo 'unknown')"
        log_error "Python >= 3.11 is required by mail2hook, but found Python ${py_ver}."
        exit 1
    fi
}

migrate_legacy_service() {
    local found_legacy=0

    # 1. Stop, disable, and delete legacy systemd service unit if present
    if is_systemd_running; then
        if systemctl list-unit-files --no-legend mail2hook.service 2>/dev/null | grep -q 'mail2hook\.service' \
            || systemctl is-active --quiet mail2hook.service 2>/dev/null \
            || systemctl is-enabled --quiet mail2hook.service 2>/dev/null; then
            found_legacy=1
            systemctl stop mail2hook.service 2>/dev/null || true
            systemctl disable mail2hook.service 2>/dev/null || true
            log_info "Stopped and disabled legacy mail2hook.service."
        fi
    fi

    local unit_path
    for unit_path in "$LEGACY_SERVICE_FILE" "$LEGACY_SERVICE_WANT" "/lib/systemd/system/mail2hook.service"; do
        if [[ -e "$unit_path" || -L "$unit_path" ]]; then
            found_legacy=1
            rm -f "$unit_path"
            log_info "Removed legacy systemd unit file/symlink: $unit_path"
        fi
    done

    if [[ "$found_legacy" -eq 1 ]] && is_systemd_running; then
        systemctl daemon-reload
        log_info "Reloaded systemd daemon."
    fi

    # 2. Remove legacy mta alternative or symlinks pointing into /opt/mail2hook
    if command -v update-alternatives >/dev/null 2>&1; then
        if update-alternatives --query mta 2>/dev/null | grep -Fq "${LEGACY_VENV_DIR}/bin/mail2hook"; then
            update-alternatives --remove mta "${LEGACY_VENV_DIR}/bin/mail2hook" 2>/dev/null || true
            log_info "Removed legacy mta alternative for ${LEGACY_VENV_DIR}/bin/mail2hook"
        fi
    fi

    local link target
    for link in "$BIN_PATH" "$SENDMAIL_PATH" "$MAILQ_PATH"; do
        if [[ -L "$link" ]]; then
            target="$(readlink "$link" || true)"
            if [[ "$target" == "${LEGACY_INSTALL_DIR}"* ]]; then
                rm -f "$link"
                log_info "Removed legacy symlink $link -> $target"
            fi
        fi
    done

    # 3. Remove legacy /opt/mail2hook venv and directory only if nothing else uses it
    if [[ -d "$LEGACY_VENV_DIR" ]]; then
        rm -rf "$LEGACY_VENV_DIR"
        log_info "Removed legacy virtualenv directory $LEGACY_VENV_DIR"
    fi

    if [[ -d "$LEGACY_INSTALL_DIR" ]]; then
        if [[ -z "$(ls -A "$LEGACY_INSTALL_DIR" 2>/dev/null)" ]]; then
            rmdir "$LEGACY_INSTALL_DIR"
            log_info "Removed legacy install directory $LEGACY_INSTALL_DIR"
        else
            log_warn "Skipped removing $LEGACY_INSTALL_DIR because unexpected files remain inside."
        fi
    fi

    # 4. Reassign /etc/mail2hook ownership before removing the old service user/group
    if [[ -d "$CONFIG_DIR" ]]; then
        chown -R "root:${ENV_GROUP}" "$CONFIG_DIR"
    fi

    # 5. Remove legacy mail2hook service user/group only if created by mail2hook and unused
    if id -u "$LEGACY_SERVICE_USER" >/dev/null 2>&1; then
        local passwd_entry user_home user_shell user_gecos
        passwd_entry="$(getent passwd "$LEGACY_SERVICE_USER" || true)"
        user_gecos="$(printf '%s' "$passwd_entry" | cut -d: -f5)"
        user_home="$(printf '%s' "$passwd_entry" | cut -d: -f6)"
        user_shell="$(printf '%s' "$passwd_entry" | cut -d: -f7)"

        if [[ "$user_home" == "$LEGACY_INSTALL_DIR" || "$user_gecos" == *"mail2hook"* ]] \
            && [[ "$user_shell" == *"nologin"* || "$user_shell" == *"false"* ]]; then
            if command -v pgrep >/dev/null 2>&1 && pgrep -u "$LEGACY_SERVICE_USER" >/dev/null 2>&1; then
                log_warn "Skipped removing user '$LEGACY_SERVICE_USER' because processes are still running as that user."
            else
                userdel "$LEGACY_SERVICE_USER" 2>/dev/null || true
                log_info "Removed legacy system user '$LEGACY_SERVICE_USER'"
            fi
        else
            log_warn "Skipped removing user '$LEGACY_SERVICE_USER' (does not match mail2hook service user signature)."
        fi
    fi

    if [[ "$ENV_GROUP" != "$LEGACY_SERVICE_GROUP" ]] && getent group "$LEGACY_SERVICE_GROUP" >/dev/null 2>&1; then
        local group_members
        group_members="$(getent group "$LEGACY_SERVICE_GROUP" | cut -d: -f4)"
        if [[ -z "$group_members" ]] && ! id -u "$LEGACY_SERVICE_USER" >/dev/null 2>&1; then
            groupdel "$LEGACY_SERVICE_GROUP" 2>/dev/null || true
            log_info "Removed legacy system group '$LEGACY_SERVICE_GROUP'"
        elif [[ -n "$group_members" ]]; then
            log_warn "Skipped removing group '$LEGACY_SERVICE_GROUP' (still has members: $group_members)."
        fi
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

    return 1
}

resolve_github_tarball_url() {
    local tag=""
    if command -v curl >/dev/null 2>&1; then
        tag="$(curl -fsSL --connect-timeout 5 --max-time 10 "$API_RELEASES_URL" 2>/dev/null \
            | sed -n 's/.*"tag_name":[[:space:]]*"\([^"]*\)".*/\1/p' \
            | head -n 1 || true)"
    fi

    if [[ -n "$tag" ]]; then
        printf '%s/archive/refs/tags/%s.tar.gz' "$REPO_URL" "$tag"
    else
        printf '%s/archive/refs/heads/main.tar.gz' "$REPO_URL"
    fi
}

install_mail2hook_binary() {
    local work_dir
    work_dir="$(mktemp -d)"
    register_cleanup_path "$work_dir"

    local src_root=""
    local local_checkout=""
    if local_checkout="$(detect_local_checkout)"; then
        log_info "Packaging mail2hook from local checkout ($local_checkout)..."
        src_root="$local_checkout/src"
    else
        local tarball_url
        tarball_url="$(resolve_github_tarball_url)"
        log_info "Downloading mail2hook source from $tarball_url..."
        local tarball_path="$work_dir/mail2hook.tar.gz"
        if ! curl -fsSL --connect-timeout 10 --max-time 60 "$tarball_url" -o "$tarball_path"; then
            log_error "Failed to download mail2hook source archive."
            exit 1
        fi
        tar --no-same-owner --no-same-permissions -xzf "$tarball_path" -C "$work_dir"
        src_root="$(find "$work_dir" -maxdepth 2 -type d -name src | head -n 1 || true)"
        if [[ -z "$src_root" || ! -d "$src_root/mail2hook" ]]; then
            log_error "Downloaded archive did not contain src/mail2hook."
            exit 1
        fi
    fi

    local stage_dir="$work_dir/stage"
    mkdir -p "$stage_dir"
    cp -a "$src_root/mail2hook" "$stage_dir/"
    find "$stage_dir" -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
    find "$stage_dir" -type l -delete 2>/dev/null || true

    local tmp_bin="$work_dir/mail2hook"
    python3 -m zipapp "$stage_dir" \
        -p "/usr/bin/python3 -sE" \
        -m "mail2hook.cli:main" \
        -o "$tmp_bin"

    install -d -m 0755 /usr/local/bin
    install -m 0755 -o root -g root "$tmp_bin" "$BIN_PATH"
    rm -rf "$work_dir"

    record_change "Installed mail2hook executable at $BIN_PATH"
    log_info "Installed executable at $BIN_PATH"
}

detect_conflicting_mta() {
    local -a mta_pkgs=(
        postfix
        exim4
        exim4-base
        exim4-daemon-light
        exim4-daemon-heavy
        msmtp-mta
        nullmailer
        sendmail-bin
        dma
        opensmtpd
        ssmtp
    )
    local -a installed=()
    local pkg
    for pkg in "${mta_pkgs[@]}"; do
        if dpkg-query -W -f='${Status}' "$pkg" 2>/dev/null | grep -q "install ok installed"; then
            installed+=("$pkg")
        fi
    done

    if [[ "${#installed[@]}" -gt 0 ]]; then
        printf '%s' "${installed[*]}"
        return 0
    fi
    return 1
}

configure_sendmail_link() {
    SENDMAIL_LINKED=0
    install -d -m 0755 /usr/sbin /usr/bin

    local existing_mtas=""
    if existing_mtas="$(detect_conflicting_mta)"; then
        log_warn "Detected existing MTA package(s): ${existing_mtas}."
        log_warn "Will NOT overwrite $SENDMAIL_PATH or purge your existing MTA."
        log_warn "Either remove the existing MTA and re-run this installer, or configure it to pipe mail to $BIN_PATH."
        return 0
    fi

    # Check if /usr/sbin/sendmail is already occupied by something other than mail2hook
    if [[ -e "$SENDMAIL_PATH" || -L "$SENDMAIL_PATH" ]]; then
        local resolved_sendmail=""
        resolved_sendmail="$(readlink -f "$SENDMAIL_PATH" 2>/dev/null || true)"

        if [[ "$resolved_sendmail" == "$BIN_PATH" ]]; then
            log_info "$SENDMAIL_PATH already points to $BIN_PATH."
        elif command -v update-alternatives >/dev/null 2>&1 \
            && [[ -L "$SENDMAIL_PATH" ]] \
            && [[ "$(readlink "$SENDMAIL_PATH" 2>/dev/null || true)" == "/etc/alternatives/mta" ]]; then
            local current_mta=""
            current_mta="$(update-alternatives --query mta 2>/dev/null | sed -n 's/^Value: //p' | head -n 1 || true)"
            if [[ -n "$current_mta" && "$current_mta" != "$BIN_PATH" ]]; then
                log_warn "System 'mta' alternative currently points to $current_mta."
                log_warn "Will NOT overwrite $SENDMAIL_PATH automatically."
                log_warn "To switch to mail2hook manually, register $BIN_PATH with update-alternatives or remove the other MTA."
                return 0
            fi
        else
            local owner_pkg=""
            owner_pkg="$(dpkg-query -S "$SENDMAIL_PATH" 2>/dev/null | head -n 1 || true)"
            log_warn "Existing $SENDMAIL_PATH found (${owner_pkg:-unmanaged file/symlink -> ${resolved_sendmail:-unknown}})."
            log_warn "Will NOT overwrite $SENDMAIL_PATH so your existing mail setup is not clobbered."
            return 0
        fi
    fi

    if command -v update-alternatives >/dev/null 2>&1; then
        local can_slave_mailq=0
        if [[ ! -e "$MAILQ_PATH" && ! -L "$MAILQ_PATH" ]]; then
            can_slave_mailq=1
        elif [[ "$(readlink -f "$MAILQ_PATH" 2>/dev/null || true)" == "$BIN_PATH" ]]; then
            can_slave_mailq=1
        elif [[ -L "$MAILQ_PATH" && "$(readlink "$MAILQ_PATH" 2>/dev/null || true)" == "/etc/alternatives/mta-mailq" ]]; then
            can_slave_mailq=1
        fi

        if [[ "$can_slave_mailq" -eq 1 ]]; then
            update-alternatives --install "$SENDMAIL_PATH" mta "$BIN_PATH" 10 \
                --slave "$MAILQ_PATH" mta-mailq "$BIN_PATH"
            log_info "Configured sendmail and mailq via update-alternatives (mta -> $BIN_PATH)"
        else
            update-alternatives --install "$SENDMAIL_PATH" mta "$BIN_PATH" 10
            log_info "Configured sendmail via update-alternatives (mta -> $BIN_PATH)"
        fi
    else
        ln -sf "$BIN_PATH" "$SENDMAIL_PATH"
        if [[ ! -e "$MAILQ_PATH" || "$(readlink -f "$MAILQ_PATH" 2>/dev/null || true)" == "$BIN_PATH" ]]; then
            ln -sf "$BIN_PATH" "$MAILQ_PATH"
        fi
        log_info "Linked $SENDMAIL_PATH -> $BIN_PATH"
    fi

    if [[ "$(readlink -f "$SENDMAIL_PATH" 2>/dev/null || true)" == "$BIN_PATH" ]]; then
        SENDMAIL_LINKED=1
        record_change "Linked $SENDMAIL_PATH -> $BIN_PATH via update-alternatives"
    else
        log_warn "$SENDMAIL_PATH does not resolve to $BIN_PATH after alternative registration."
    fi
}

lowercase() {
    printf '%s' "$1" | tr '[:upper:]' '[:lower:]'
}

trim_ws() {
    local val="$1"
    val="${val#"${val%%[![:space:]]*}"}"
    val="${val%"${val##*[![:space:]]}"}"
    printf '%s' "$val"
}

is_postfix_installed() {
    if dpkg-query -W -f='${Status}' postfix 2>/dev/null | grep -q "install ok installed"; then
        return 0
    fi
    if command -v postconf >/dev/null 2>&1 \
        && [[ -f "$POSTFIX_MAIN_CF" && -f "$POSTFIX_MASTER_CF" ]]; then
        return 0
    fi
    return 1
}

detect_postfix_db_type() {
    local default_type="" supported_types=""
    default_type="$(postconf -h default_database_type 2>/dev/null | tr -d '[:space:]' || true)"
    supported_types="$(postconf -m 2>/dev/null || true)"

    if [[ -n "$default_type" ]]; then
        if [[ -z "$supported_types" ]] \
            || printf '%s\n' "$supported_types" | grep -Fxq "$default_type"; then
            printf '%s' "$default_type"
            return 0
        fi
    fi

    local candidate
    for candidate in hash lmdb cdb btree; do
        if printf '%s\n' "$supported_types" | grep -Fxq "$candidate"; then
            printf '%s' "$candidate"
            return 0
        fi
    done

    printf '%s' "hash"
}

emit_master_cf_block() {
    local pipe_user="$1"
    local env_group="$2"
    local bin_path="$3"

    printf '%s\n' "$POSTFIX_MASTER_BEGIN"
    printf 'mail2hook unix  -       n       n       -       -       pipe\n'
    printf "  flags=R user=%s:%s argv=%s -oi -f \${sender} -- \${recipient}\n" \
        "$pipe_user" "$env_group" "$bin_path"
    printf '%s\n' "$POSTFIX_MASTER_END"
}

update_master_cf_block() {
    local master_cf_path="$1"
    local pipe_user="$2"
    local env_group="$3"
    local bin_path="$4"
    local backup_dir="${5:-}"

    local has_begin=0 has_end=0
    if [[ -f "$master_cf_path" ]]; then
        grep -Fq "$POSTFIX_MASTER_BEGIN" "$master_cf_path" && has_begin=1 || true
        grep -Fq "$POSTFIX_MASTER_END" "$master_cf_path" && has_end=1 || true
    fi

    if [[ "$has_begin" -ne "$has_end" ]]; then
        log_warn "Found incomplete mail2hook marker block in $master_cf_path; skipping automatic edit."
        return 1
    fi

    if [[ "$has_begin" -eq 0 && -f "$master_cf_path" ]]; then
        if grep -Eq '^[[:space:]]*mail2hook[[:space:]]+unix([[:space:]]|$)' "$master_cf_path"; then
            log_warn "Found unmanaged 'mail2hook' transport in $master_cf_path; leaving existing entry untouched."
            return 1
        fi
    fi

    local tmp_file
    tmp_file="$(mktemp "${master_cf_path}.tmp.XXXXXX")"
    register_cleanup_path "$tmp_file"

    local in_block=0 replaced=0 last_line="" line_count=0 line
    if [[ -f "$master_cf_path" ]]; then
        while IFS= read -r line || [[ -n "$line" ]]; do
            line_count=$((line_count + 1))
            if [[ "$line" == "$POSTFIX_MASTER_BEGIN" ]]; then
                emit_master_cf_block "$pipe_user" "$env_group" "$bin_path" >> "$tmp_file"
                in_block=1
                replaced=1
                continue
            fi
            if [[ "$in_block" -eq 1 ]]; then
                if [[ "$line" == "$POSTFIX_MASTER_END" ]]; then
                    in_block=0
                fi
                continue
            fi
            printf '%s\n' "$line" >> "$tmp_file"
            last_line="$line"
        done < "$master_cf_path"
    fi

    if [[ "$replaced" -eq 0 ]]; then
        if [[ "$line_count" -gt 0 && -n "$last_line" ]]; then
            printf '\n' >> "$tmp_file"
        fi
        emit_master_cf_block "$pipe_user" "$env_group" "$bin_path" >> "$tmp_file"
    fi

    if [[ -f "$master_cf_path" ]] && cmp -s "$master_cf_path" "$tmp_file"; then
        rm -f "$tmp_file"
        return 0
    fi

    if [[ -n "$backup_dir" && -f "$master_cf_path" ]]; then
        backup_file_to_dir "$master_cf_path" "$backup_dir"
    fi
    chmod 0644 "$tmp_file"
    mv -f "$tmp_file" "$master_cf_path"
    record_change "Configured mail2hook pipe transport in $master_cf_path"
    return 0
}

remove_master_cf_block() {
    local master_cf_path="$1"
    local backup_dir="${2:-}"

    if [[ ! -f "$master_cf_path" ]]; then
        return 0
    fi
    if ! grep -Fq "$POSTFIX_MASTER_BEGIN" "$master_cf_path" \
        || ! grep -Fq "$POSTFIX_MASTER_END" "$master_cf_path"; then
        return 0
    fi

    local tmp_file
    tmp_file="$(mktemp "${master_cf_path}.tmp.XXXXXX")"
    register_cleanup_path "$tmp_file"

    local in_block=0 had_blank_before=0 removed_at_eof=1 line
    local -a kept_lines=()
    while IFS= read -r line || [[ -n "$line" ]]; do
        if [[ "$line" == "$POSTFIX_MASTER_BEGIN" ]]; then
            in_block=1
            if [[ "${#kept_lines[@]}" -gt 0 ]]; then
                local last_idx=$((${#kept_lines[@]} - 1))
                if [[ -z "${kept_lines[$last_idx]}" ]]; then
                    had_blank_before=1
                fi
            fi
            continue
        fi
        if [[ "$in_block" -eq 1 ]]; then
            if [[ "$line" == "$POSTFIX_MASTER_END" ]]; then
                in_block=0
            fi
            continue
        fi
        if [[ "$had_blank_before" -eq 1 ]]; then
            removed_at_eof=0
            if [[ -z "$line" ]]; then
                unset "kept_lines[$(( ${#kept_lines[@]} - 1 ))]"
            fi
            had_blank_before=0
        fi
        kept_lines+=("$line")
    done < "$master_cf_path"

    if [[ "$had_blank_before" -eq 1 && "$removed_at_eof" -eq 1 && "${#kept_lines[@]}" -gt 0 ]]; then
        if [[ -z "${kept_lines[$(( ${#kept_lines[@]} - 1 ))]}" ]]; then
            unset "kept_lines[$(( ${#kept_lines[@]} - 1 ))]"
        fi
    fi

    local out_line
    for out_line in "${kept_lines[@]+"${kept_lines[@]}"}"; do
        printf '%s\n' "$out_line" >> "$tmp_file"
    done

    if cmp -s "$master_cf_path" "$tmp_file"; then
        rm -f "$tmp_file"
        return 0
    fi

    if [[ -n "$backup_dir" ]]; then
        backup_file_to_dir "$master_cf_path" "$backup_dir"
    fi
    chmod 0644 "$tmp_file"
    mv -f "$tmp_file" "$master_cf_path"
    record_change "Removed mail2hook pipe transport from $master_cf_path"
    return 0
}

prepend_transport_map_value() {
    local existing="${1:-}"
    local map_spec="$2"
    local transport_file="$3"
    local normalized token
    local -a out=("$map_spec")

    normalized="${existing//,/ }"
    for token in $normalized; do
        [[ -z "$token" ]] && continue
        if [[ "$token" == "$transport_file" || "$token" == *":${transport_file}" ]]; then
            continue
        fi
        out+=("$token")
    done

    local joined="" i
    for i in "${!out[@]}"; do
        if [[ "$i" -eq 0 ]]; then
            joined="${out[$i]}"
        else
            joined="${joined}, ${out[$i]}"
        fi
    done
    printf '%s' "$joined"
}

strip_transport_map_value() {
    local existing="${1:-}"
    local transport_file="$2"
    local normalized token
    local -a out=()

    normalized="${existing//,/ }"
    for token in $normalized; do
        [[ -z "$token" ]] && continue
        if [[ "$token" == "$transport_file" || "$token" == *":${transport_file}" ]]; then
            continue
        fi
        out+=("$token")
    done

    local joined="" i
    for i in "${!out[@]}"; do
        if [[ "$i" -eq 0 ]]; then
            joined="${out[$i]}"
        else
            joined="${joined}, ${out[$i]}"
        fi
    done
    printf '%s' "$joined"
}

save_previous_apt_mail_target() {
    local current_target="${1:-}"
    local orig_file="${2:-$APT_MAIL_ORIG_BACKUP}"
    local backup_dir="${3:-$BACKUP_DIR}"
    local trimmed lower_cur

    trimmed="$(trim_ws "$current_target")"
    lower_cur="$(lowercase "$trimmed")"

    if [[ -f "$orig_file" ]]; then
        cat "$orig_file" 2>/dev/null || true
        return 0
    fi

    if [[ "$lower_cur" == "$(lowercase "$POSTFIX_RECIPIENT")" ]]; then
        printf '%s' ""
        return 0
    fi

    install -d -m 0750 -o root -g root "$backup_dir" 2>/dev/null || mkdir -p "$backup_dir"
    printf '%s' "$trimmed" > "$orig_file"
    chmod 0640 "$orig_file"
    printf '%s' "$trimmed"
}

restore_apt_mail_target() {
    local apt_file="$1"
    local orig_file="${2:-$APT_MAIL_ORIG_BACKUP}"
    local backup_dir="${3:-$BACKUP_DIR}"

    if [[ ! -f "$apt_file" ]] || ! grep -Fq "$POSTFIX_RECIPIENT" "$apt_file" 2>/dev/null; then
        return 0
    fi

    local has_orig=0 orig_val=""
    if [[ -f "$orig_file" ]]; then
        has_orig=1
        orig_val="$(cat "$orig_file" 2>/dev/null || true)"
        orig_val="$(trim_ws "$orig_val")"
    fi

    local tmp_file
    tmp_file="$(mktemp "${apt_file}.tmp.XXXXXX")"
    register_cleanup_path "$tmp_file"

    local line
    while IFS= read -r line || [[ -n "$line" ]]; do
        if [[ "$line" =~ ^[[:space:]]*Unattended-Upgrade::Mail[[:space:]]+\"(.*)\"[[:space:]]*\;[[:space:]]*$ ]]; then
            if [[ -n "$orig_val" ]]; then
                printf 'Unattended-Upgrade::Mail "%s";\n' "$orig_val" >> "$tmp_file"
            else
                printf '// Unattended-Upgrade::Mail ""; // Restored by mail2hook uninstall\n' >> "$tmp_file"
            fi
        else
            printf '%s\n' "$line" >> "$tmp_file"
        fi
    done < "$apt_file"

    if ! cmp -s "$apt_file" "$tmp_file"; then
        if [[ -n "$backup_dir" ]]; then
            backup_file_to_dir "$apt_file" "$backup_dir"
        fi
        chmod 0644 "$tmp_file"
        mv -f "$tmp_file" "$apt_file"
        if [[ -n "$orig_val" ]]; then
            record_change "Restored Unattended-Upgrade::Mail to \"${orig_val}\" in $apt_file"
        else
            record_change "Restored Unattended-Upgrade::Mail to unset in $apt_file"
        fi
    else
        rm -f "$tmp_file"
    fi

    if [[ "$has_orig" -eq 1 ]]; then
        rm -f "$orig_file"
    fi
}

ensure_env_filter_default() {
    local env_path="$1"
    local default_keywords="${2:-$DEFAULT_SUBJECT_FILTER}"

    if [[ ! -f "$env_path" ]]; then
        return 0
    fi

    if grep -Eq '^[[:space:]]*(export[[:space:]]+)?FILTER_SUBJECT_KEYWORDS[[:space:]]*=' "$env_path" 2>/dev/null; then
        return 0
    fi

    local tmp_filter
    tmp_filter="$(mktemp "${env_path}.tmp.XXXXXX")"
    register_cleanup_path "$tmp_filter"

    local line inserted=0
    while IFS= read -r line || [[ -n "$line" ]]; do
        if [[ "$inserted" -eq 0 && "$line" =~ ^[[:space:]]*#[[:space:]]*FILTER_SUBJECT_KEYWORDS[[:space:]]*= ]]; then
            printf 'FILTER_SUBJECT_KEYWORDS=%s\n' "$default_keywords" >> "$tmp_filter"
            inserted=1
        else
            printf '%s\n' "$line" >> "$tmp_filter"
        fi
    done < "$env_path"

    if [[ "$inserted" -eq 0 ]]; then
        printf '\n# Restrict Discord alerts to unattended-upgrades mail by default\n' >> "$tmp_filter"
        printf 'FILTER_SUBJECT_KEYWORDS=%s\n' "$default_keywords" >> "$tmp_filter"
    fi

    mv -f "$tmp_filter" "$env_path"
}

read_env_var() {
    local env_path="$1"
    local var_name="$2"

    if [[ ! -f "$env_path" ]]; then
        return 0
    fi

    local line val found_val=""
    while IFS= read -r line || [[ -n "$line" ]]; do
        if [[ "$line" =~ ^[[:space:]]*(export[[:space:]]+)?${var_name}[[:space:]]*=(.*)$ ]]; then
            val="$(trim_ws "${BASH_REMATCH[2]}")"
            if [[ "$val" =~ ^\"(.*)\"[[:space:]]*(#.*)?$ ]]; then
                val="${BASH_REMATCH[1]}"
            elif [[ "$val" =~ ^\'(.*)\'[[:space:]]*(#.*)?$ ]]; then
                val="${BASH_REMATCH[1]}"
            else
                val="${val%% #*}"
                val="$(trim_ws "$val")"
            fi
            found_val="$val"
        fi
    done < "$env_path"

    printf '%s' "$found_val"
}

reload_postfix() {
    if is_systemd_running && systemctl is-active --quiet postfix 2>/dev/null; then
        systemctl reload postfix
    elif command -v postfix >/dev/null 2>&1; then
        postfix reload 2>/dev/null || true
    fi
}

configure_postfix_pipe() {
    if ! id -u "$POSTFIX_PIPE_USER" >/dev/null 2>&1; then
        log_error "Postfix pipe user '$POSTFIX_PIPE_USER' does not exist on this system."
        exit 1
    fi
    if [[ "$ENV_GROUP" == "root" ]]; then
        log_error "No dedicated group ('mail' or 'mail2hook') available for $ENV_FILE; user '$POSTFIX_PIPE_USER' would not be able to read it."
        exit 1
    fi
    if ! command -v postconf >/dev/null 2>&1 || ! command -v postmap >/dev/null 2>&1; then
        log_error "Postfix is installed, but postconf/postmap commands are unavailable."
        exit 1
    fi
    if [[ ! -f "$POSTFIX_MAIN_CF" || ! -f "$POSTFIX_MASTER_CF" ]]; then
        log_error "Postfix configuration files ($POSTFIX_MAIN_CF / $POSTFIX_MASTER_CF) not found."
        exit 1
    fi

    local db_type map_spec mydomain
    db_type="$(detect_postfix_db_type)"
    map_spec="${db_type}:${POSTFIX_TRANSPORT_FILE}"
    mydomain="$(postconf -h mydomain 2>/dev/null | tr -d '[:space:]' || true)"

    local tmp_transport
    tmp_transport="$(mktemp "${POSTFIX_TRANSPORT_FILE}.tmp.XXXXXX")"
    register_cleanup_path "$tmp_transport"
    {
        printf '# Managed by mail2hook install.sh\n'
        printf '%s %s:\n' "$POSTFIX_RECIPIENT" "$POSTFIX_TRANSPORT_NAME"
        printf 'mail2hook@localhost.localdomain %s:\n' "$POSTFIX_TRANSPORT_NAME"
        if [[ -n "$mydomain" && "$mydomain" != "localhost" && "$mydomain" != "localdomain" ]]; then
            printf 'mail2hook@localhost.%s %s:\n' "$mydomain" "$POSTFIX_TRANSPORT_NAME"
        fi
    } > "$tmp_transport"

    local need_postmap=0
    if [[ ! -f "$POSTFIX_TRANSPORT_FILE" ]] || ! cmp -s "$POSTFIX_TRANSPORT_FILE" "$tmp_transport"; then
        backup_if_unmanaged "$POSTFIX_TRANSPORT_FILE" "Managed by mail2hook install.sh"
        chmod 0644 "$tmp_transport"
        mv -f "$tmp_transport" "$POSTFIX_TRANSPORT_FILE"
        need_postmap=1
        record_change "Wrote Postfix transport map at $POSTFIX_TRANSPORT_FILE"
    else
        rm -f "$tmp_transport"
    fi

    if [[ "$need_postmap" -eq 1 ]] || [[ ! -e "${POSTFIX_TRANSPORT_FILE}.db" && ! -e "${POSTFIX_TRANSPORT_FILE}.lmdb" && ! -e "${POSTFIX_TRANSPORT_FILE}.cdb" ]]; then
        postmap "$map_spec"
        record_change "Compiled Postfix transport table (${map_spec})"
    fi

    if ! update_master_cf_block "$POSTFIX_MASTER_CF" "$POSTFIX_PIPE_USER" "$ENV_GROUP" "$BIN_PATH" "$BACKUP_DIR"; then
        log_error "Could not safely update $POSTFIX_MASTER_CF. Resolve the existing 'mail2hook' entry and re-run."
        exit 1
    fi

    local cur_maps cur_limit new_maps limit_param
    cur_maps="$(postconf -h transport_maps 2>/dev/null || true)"
    limit_param="${POSTFIX_TRANSPORT_NAME}_destination_recipient_limit"
    cur_limit="$(postconf -h "$limit_param" 2>/dev/null | tr -d '[:space:]' || true)"
    new_maps="$(prepend_transport_map_value "$cur_maps" "$map_spec" "$POSTFIX_TRANSPORT_FILE")"

    if [[ "$cur_maps" != "$new_maps" || "$cur_limit" != "1" ]]; then
        backup_file_to_dir "$POSTFIX_MAIN_CF" "$BACKUP_DIR"
        if [[ "$cur_maps" != "$new_maps" ]]; then
            postconf -e "transport_maps = ${new_maps}"
            record_change "Added ${map_spec} to transport_maps in $POSTFIX_MAIN_CF"
        fi
        if [[ "$cur_limit" != "1" ]]; then
            postconf -e "${limit_param} = 1"
            record_change "Set ${limit_param} = 1 in $POSTFIX_MAIN_CF"
        fi
    fi

    if ! postfix check; then
        log_error "postfix check failed after updating Postfix configuration. Backups are saved in $BACKUP_DIR."
        exit 1
    fi

    reload_postfix
    log_info "Configured Postfix pipe transport ($POSTFIX_RECIPIENT -> $BIN_PATH as ${POSTFIX_PIPE_USER}:${ENV_GROUP})"
}

configure_mail_integration() {
    SENDMAIL_LINKED=0

    if is_postfix_installed; then
        INSTALL_MODE="postfix-pipe"
        DETECTED_MTAS="postfix"
        log_info "Detected Postfix; leaving $SENDMAIL_PATH untouched and configuring pipe transport alongside it."
        configure_postfix_pipe
        return 0
    fi

    configure_sendmail_link
    if [[ "$SENDMAIL_LINKED" -eq 1 ]]; then
        INSTALL_MODE="sendmail-shim"
    else
        INSTALL_MODE="manual-mta"
        DETECTED_MTAS="$(detect_conflicting_mta || true)"
    fi
}

write_env_file() {
    install -d -m 0750 -o root -g "$ENV_GROUP" "$CONFIG_DIR"

    local tmp_env
    tmp_env="$(mktemp "${CONFIG_DIR}/.env.tmp.XXXXXX")"
    register_cleanup_path "$tmp_env"
    chown root:root "$tmp_env"
    chmod 0600 "$tmp_env"

    if [[ -f "$ENV_FILE" ]]; then
        local updated=0
        local line
        while IFS= read -r line || [[ -n "$line" ]]; do
            if [[ "$line" =~ ^[[:space:]]*(export[[:space:]]+)?(DISCORD_WEBHOOK_URL|DISCORD_WEBHOOK|WEBHOOK_URL)[[:space:]]*= ]]; then
                if [[ "$updated" -eq 0 ]]; then
                    printf 'DISCORD_WEBHOOK_URL=%s\n' "$RESOLVED_WEBHOOK_URL" >> "$tmp_env"
                    updated=1
                fi
            elif [[ "$line" =~ ^[[:space:]]*https://((ptb|canary)\.)?discord(app)?\.com/api/webhooks/ ]]; then
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

        ensure_env_filter_default "$tmp_env" "$DEFAULT_SUBJECT_FILTER"
    else
        cat > "$tmp_env" <<EOF
# /etc/mail2hook/env - Configuration for mail2hook
# Owned by root:${ENV_GROUP} (mode 0640): readable by root callers (cron,
# unattended-upgrades) and users in the '${ENV_GROUP}' group, while keeping the
# Discord webhook secret hidden from unprivileged users.

# Required: Discord webhook URL
DISCORD_WEBHOOK_URL=${RESOLVED_WEBHOOK_URL}

# Optional: Friendly server name display in Discord (defaults to system hostname)
# SERVER_NAME=

# Optional: Comma-separated sender addresses, domains, or usernames to match. If unset, all senders match.
# FILTER_SENDERS=

# Comma-separated subject keywords to match (case-insensitive).
# Defaults to unattended-upgrades so cron/other local mail is dropped silently (exit 0).
# Set empty (FILTER_SUBJECT_KEYWORDS=) to forward all mail subjects.
FILTER_SUBJECT_KEYWORDS=${DEFAULT_SUBJECT_FILTER}

# Optional: Maximum body characters to include in the Discord embed excerpt (default: 1500)
# MAX_BODY_CHARS=1500

# Optional: Bot display name in Discord (default: mail2hook)
# DISCORD_USERNAME=mail2hook
EOF
    fi

    chown "root:${ENV_GROUP}" "$tmp_env"
    chmod 0640 "$tmp_env"
    mv -f "$tmp_env" "$ENV_FILE"
    ACTIVE_SUBJECT_FILTER="$(read_env_var "$ENV_FILE" "FILTER_SUBJECT_KEYWORDS")"
    record_change "Configured $ENV_FILE (root:${ENV_GROUP}, mode 0640; FILTER_SUBJECT_KEYWORDS=${ACTIVE_SUBJECT_FILTER:-<all>})"
    log_info "Secured configuration file at $ENV_FILE (root:${ENV_GROUP}, mode 0640)"
}

ubuntu_esm_security_present() {
    local f
    for f in /etc/apt/sources.list /etc/apt/sources.list.d/*.list /etc/apt/sources.list.d/*.sources; do
        [[ -f "$f" ]] || continue
        if grep -Eq '^[[:space:]]*[^#].*esm\.ubuntu\.com' "$f" 2>/dev/null; then
            return 0
        fi
    done
    return 1
}

read_apt_mail_target() {
    local raw=""
    if command -v apt-config >/dev/null 2>&1; then
        raw="$(apt-config dump Unattended-Upgrade::Mail 2>/dev/null \
            | sed -n 's/^Unattended-Upgrade::Mail[[:space:]]*"\(.*\)";[[:space:]]*$/\1/p' \
            | head -n 1 || true)"
    fi
    printf '%s' "$raw"
}

configure_unattended_upgrades() {
    if [[ "$FLAG_SKIP_UPGRADES" -eq 1 ]]; then
        log_info "Skipping unattended-upgrades configuration (--skip-upgrades)."
        return 0
    fi

    log_info "Configuring conservative security-only unattended-upgrades..."

    local existing_mail=""
    existing_mail="$(read_apt_mail_target)"
    if [[ "$INSTALL_MODE" == "postfix-pipe" ]]; then
        PREVIOUS_UPGRADE_MAIL_TARGET="$(save_previous_apt_mail_target "$existing_mail" "$APT_MAIL_ORIG_BACKUP" "$BACKUP_DIR")"
        UPGRADE_MAIL_TARGET="$POSTFIX_RECIPIENT"
    else
        UPGRADE_MAIL_TARGET="root"
    fi

    if [[ -f "$APT_LEGACY_MAIL2HOOK" ]]; then
        backup_if_unmanaged "$APT_LEGACY_MAIL2HOOK" "Default unattended-upgrades configuration for mail2hook"
        rm -f "$APT_LEGACY_MAIL2HOOK"
    fi

    backup_if_unmanaged "$APT_AUTO_UPGRADES" "Managed by mail2hook install.sh"
    cat > "$APT_AUTO_UPGRADES" <<'EOF'
// Managed by mail2hook install.sh
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
APT::Periodic::AutocleanInterval "7";
EOF
    chmod 0644 "$APT_AUTO_UPGRADES"

    backup_if_unmanaged "$APT_LOCAL_UPGRADES" "Managed by mail2hook install.sh"

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

${origins_block}

// Deliver upgrade notifications via /usr/sbin/sendmail (no mailx needed).
Unattended-Upgrade::Mail "${UPGRADE_MAIL_TARGET}";

// Notify only when packages are upgraded/kept-back/removed or an error occurs,
// keeping Discord quiet on uneventful zero-change nightly runs.
// Valid options: "on-change" (recommended), "always", "only-on-error".
Unattended-Upgrade::MailReport "on-change";
Unattended-Upgrade::MailOnlyOnError "false";
Unattended-Upgrade::Sender "unattended-upgrades";

// Never reboot automatically; mail2hook reports pending reboots in Discord
Unattended-Upgrade::Automatic-Reboot "false";
Unattended-Upgrade::Automatic-Reboot-WithUsers "false";

// Keep unused dependencies and kernel packages untouched
Unattended-Upgrade::Remove-Unused-Dependencies "false";
Unattended-Upgrade::Remove-Unused-Kernel-Packages "false";
EOF
    chmod 0644 "$APT_LOCAL_UPGRADES"
    record_change "Configured unattended-upgrades drop-ins ($APT_AUTO_UPGRADES, $APT_LOCAL_UPGRADES; Mail=\"${UPGRADE_MAIL_TARGET}\")"
    log_info "Wrote APT periodic and security-only drop-ins ($APT_AUTO_UPGRADES, $APT_LOCAL_UPGRADES)"

    install -d -m 0755 "$TIMER_DAILY_DIR" "$TIMER_UPGRADE_DIR"
    backup_if_unmanaged "$TIMER_DAILY_CONF" "Managed by mail2hook install.sh"
    backup_if_unmanaged "$TIMER_UPGRADE_CONF" "Managed by mail2hook install.sh"

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
    record_change "Configured APT systemd timer overrides ($TIMER_DAILY_CONF, $TIMER_UPGRADE_CONF)"
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

run_sendmail_e2e_test() {
    local host_name
    host_name="$(hostname 2>/dev/null || echo 'server')"

    if [[ "$INSTALL_MODE" == "postfix-pipe" ]]; then
        log_info "Submitting test message through $SENDMAIL_PATH -> $POSTFIX_RECIPIENT..."
        local rc=0
        if printf 'To: %s\nFrom: root\nSubject: mail2hook Postfix route test on %s\n\nmail2hook Postfix pipe test submitted via %s.\n' \
            "$POSTFIX_RECIPIENT" "$host_name" "$SENDMAIL_PATH" \
            | "$SENDMAIL_PATH" -oi -t; then
            E2E_STATUS="submitted (via $SENDMAIL_PATH -> $POSTFIX_RECIPIENT)"
            log_info "Submitted test message through Postfix ($POSTFIX_RECIPIENT)."
        else
            rc=$?
            E2E_STATUS="failed (exit code ${rc})"
            log_warn "Postfix sendmail test submission failed with exit code ${rc}."
        fi
    elif [[ "$SENDMAIL_LINKED" -eq 1 ]]; then
        log_info "Sending end-to-end test message through $SENDMAIL_PATH..."
        local rc=0
        if printf 'To: root\nFrom: root\nSubject: mail2hook install test on %s\n\nmail2hook sendmail shim verification succeeded.\n' "$host_name" \
            | "$SENDMAIL_PATH" -oi -t; then
            E2E_STATUS="passed (via $SENDMAIL_PATH)"
            log_info "End-to-end sendmail test succeeded."
        else
            rc=$?
            E2E_STATUS="failed (exit code ${rc})"
            log_warn "End-to-end sendmail test failed with exit code ${rc}."
            log_warn "Verify network access and the webhook URL in $ENV_FILE, then re-test with:"
            log_warn "  printf 'Subject: test\\nFrom: root\\nTo: root\\n\\nTest message.\\n' | sudo $SENDMAIL_PATH -oi -t"
        fi
    else
        log_warn "Skipping $SENDMAIL_PATH end-to-end test because another MTA owns $SENDMAIL_PATH."
        log_info "Testing $BIN_PATH directly instead..."
        local rc=0
        if printf 'To: root\nFrom: root\nSubject: mail2hook install test on %s\n\nmail2hook direct invocation verification (sendmail NOT linked to mail2hook).\n' "$host_name" \
            | "$BIN_PATH" -oi -t; then
            E2E_STATUS="direct only (sendmail NOT linked — existing MTA active)"
            log_warn "Direct $BIN_PATH test succeeded, but $SENDMAIL_PATH is still owned by another MTA."
        else
            rc=$?
            E2E_STATUS="failed (direct $BIN_PATH exit code ${rc})"
            log_warn "Direct $BIN_PATH test failed with exit code ${rc}."
        fi
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
    local sendmail_summary="not linked (existing MTA preserved)"
    if [[ "$INSTALL_MODE" == "postfix-pipe" ]]; then
        sendmail_summary="$SENDMAIL_PATH untouched (Postfix preserved)"
    elif [[ "$SENDMAIL_LINKED" -eq 1 ]]; then
        sendmail_summary="$SENDMAIL_PATH -> $BIN_PATH"
    fi

    printf '\n============================================================\n'
    printf 'mail2hook Installation Summary\n'
    printf '============================================================\n'
    printf '  OS Detected      : %s (%s)\n' "$OS_PRETTY" "$OS_CODENAME"
    printf '  Install Mode     : %s\n' "${INSTALL_MODE:-unknown}"
    printf '  Server Timezone  : %s\n' "$tz_info"
    printf '  Executable       : %s\n' "$BIN_PATH"
    printf '  Sendmail Path    : %s\n' "$sendmail_summary"
    if [[ "$INSTALL_MODE" == "postfix-pipe" ]]; then
        printf '  Postfix Route    : %s -> %s pipe (user=%s:%s)\n' \
            "$POSTFIX_RECIPIENT" "$POSTFIX_TRANSPORT_NAME" "$POSTFIX_PIPE_USER" "$ENV_GROUP"
    fi
    printf '  Config File      : %s (root:%s, 0640)\n' "$ENV_FILE" "$ENV_GROUP"
    printf '  Subject Filter   : %s\n' "${ACTIVE_SUBJECT_FILTER:-<all subjects>}"
    printf '  End-to-End Test  : %s\n' "$E2E_STATUS"
    if [[ "$FLAG_SKIP_UPGRADES" -eq 0 ]]; then
        printf '  Auto-Upgrades    : Security updates only (Automatic-Reboot: false)\n'
        if [[ "$INSTALL_MODE" == "postfix-pipe" ]]; then
            printf '  Upgrade Mail To  : %s (replaced previous value: %s)\n' \
                "$UPGRADE_MAIL_TARGET" "${PREVIOUS_UPGRADE_MAIL_TARGET:-<unset>}"
            printf '  Root Mail Policy : Other root/admin mail untouched (only %s enters pipe)\n' \
                "$POSTFIX_RECIPIENT"
        else
            printf '  Upgrade Mail To  : %s\n' "$UPGRADE_MAIL_TARGET"
        fi
        printf '  Download Window  : 01:00 - 02:00 local time (%s)\n' "$tz_info"
        printf '  Upgrade Window   : 03:00 - 04:00 local time (%s)\n' "$tz_info"
    else
        printf '  Auto-Upgrades    : Skipped (--skip-upgrades)\n'
    fi
    if [[ "${#CHANGES_MADE[@]}" -gt 0 ]]; then
        printf '\nChanges Applied:\n'
        local chg
        for chg in "${CHANGES_MADE[@]}"; do
            printf '  - %s\n' "$chg"
        done
    fi
    if [[ "${#BACKUPS_CREATED[@]}" -gt 0 ]]; then
        printf '\nBackups Saved (%s):\n' "$BACKUP_DIR"
        local bak
        for bak in "${BACKUPS_CREATED[@]}"; do
            printf '  - %s\n' "$bak"
        done
    fi
    if [[ "$INSTALL_MODE" == "manual-mta" ]]; then
        printf '\n[ACTION REQUIRED] Existing MTA (%s) still owns %s.\n' \
            "${DETECTED_MTAS:-unknown}" "$SENDMAIL_PATH"
        printf 'System mail will NOT reach mail2hook until you configure your MTA to route to %s\n' "$BIN_PATH"
        printf 'or purge the conflicting MTA and re-run install.sh.\n'
    fi
    printf '\nUseful Commands:\n'
    printf '  Upgrade logs     : sudo tail -n 50 /var/log/unattended-upgrades/unattended-upgrades.log\n'
    if [[ "$INSTALL_MODE" == "postfix-pipe" ]]; then
        printf '  Test Postfix     : printf "To: %s\\nFrom: root\\nSubject: unattended-upgrades test\\n\\nHello\\n" | sudo sendmail -oi -t\n' "$POSTFIX_RECIPIENT"
    else
        printf '  Test sendmail    : printf "To: root\\nFrom: root\\nSubject: unattended-upgrades test\\n\\nHello\\n" | sudo sendmail -oi -t\n'
    fi
    printf '  Test dry-run     : printf "Subject: unattended-upgrades test\\nFrom: root\\n\\nHello\\n" | sudo mail2hook --dry-run\n'
    printf '============================================================\n'

    if [[ -f /var/run/reboot-required || -f /run/reboot-required ]]; then
        printf '[NOTICE] A system reboot is currently required (/var/run/reboot-required exists).\n'
        printf '         This script will NOT reboot automatically. Run "sudo reboot" when ready.\n'
    fi
}

remove_postfix_pipe() {
    local postfix_touched=0

    if [[ -f "$POSTFIX_MASTER_CF" ]] && grep -Fq "$POSTFIX_MASTER_BEGIN" "$POSTFIX_MASTER_CF" 2>/dev/null; then
        remove_master_cf_block "$POSTFIX_MASTER_CF" "$BACKUP_DIR"
        postfix_touched=1
    fi

    if command -v postconf >/dev/null 2>&1 && [[ -f "$POSTFIX_MAIN_CF" ]]; then
        local cur_maps stripped_maps limit_param cur_limit
        cur_maps="$(postconf -h transport_maps 2>/dev/null || true)"
        stripped_maps="$(strip_transport_map_value "$cur_maps" "$POSTFIX_TRANSPORT_FILE")"
        limit_param="${POSTFIX_TRANSPORT_NAME}_destination_recipient_limit"
        cur_limit="$(postconf -h "$limit_param" 2>/dev/null | tr -d '[:space:]' || true)"

        if [[ "$cur_maps" != "$stripped_maps" || -n "$cur_limit" ]]; then
            backup_file_to_dir "$POSTFIX_MAIN_CF" "$BACKUP_DIR"
            if [[ "$cur_maps" != "$stripped_maps" ]]; then
                if [[ -n "$stripped_maps" ]]; then
                    postconf -e "transport_maps = ${stripped_maps}"
                else
                    postconf -X transport_maps
                fi
                record_change "Removed $POSTFIX_TRANSPORT_FILE from transport_maps in $POSTFIX_MAIN_CF"
            fi
            if [[ -n "$cur_limit" ]]; then
                postconf -X "$limit_param"
                record_change "Removed $limit_param from $POSTFIX_MAIN_CF"
            fi
            postfix_touched=1
        fi
    fi

    local ext removed_transport=0
    for ext in "" ".db" ".lmdb" ".cdb" ".pag" ".dir"; do
        if [[ -e "${POSTFIX_TRANSPORT_FILE}${ext}" ]]; then
            rm -f "${POSTFIX_TRANSPORT_FILE}${ext}"
            removed_transport=1
        fi
    done
    if [[ "$removed_transport" -eq 1 ]]; then
        record_change "Removed Postfix transport files ${POSTFIX_TRANSPORT_FILE}*"
        postfix_touched=1
    fi

    if [[ "$postfix_touched" -eq 1 ]] && command -v postfix >/dev/null 2>&1; then
        if postfix check 2>/dev/null; then
            reload_postfix
        else
            log_warn "postfix check reported an error during uninstall; check $BACKUP_DIR."
        fi
    fi
}

run_uninstall() {
    log_info "Uninstalling mail2hook..."

    migrate_legacy_service
    remove_postfix_pipe

    if command -v update-alternatives >/dev/null 2>&1; then
        if update-alternatives --query mta 2>/dev/null | grep -Fq "$BIN_PATH"; then
            update-alternatives --remove mta "$BIN_PATH"
            record_change "Removed mail2hook from update-alternatives (mta)"
            log_info "Removed mail2hook from update-alternatives (mta)"
        fi
    fi

    local link target
    for link in "$SENDMAIL_PATH" "$MAILQ_PATH"; do
        if [[ -L "$link" ]]; then
            target="$(readlink "$link" || true)"
            if [[ "$target" == "$BIN_PATH" ]]; then
                rm -f "$link"
                record_change "Removed symlink $link"
                log_info "Removed symlink $link"
            fi
        fi
    done

    if [[ -e "$BIN_PATH" || -L "$BIN_PATH" ]]; then
        rm -f "$BIN_PATH"
        record_change "Removed executable $BIN_PATH"
        log_info "Removed executable $BIN_PATH"
    fi

    if [[ -d "$CONFIG_DIR" || -f "$ENV_FILE" ]]; then
        if prompt_yes_no "Delete configuration and secret webhook file in $CONFIG_DIR?" "n"; then
            rm -rf "$CONFIG_DIR"
            record_change "Removed configuration directory $CONFIG_DIR"
            log_info "Removed configuration directory $CONFIG_DIR"
        else
            log_info "Kept configuration directory $CONFIG_DIR"
        fi
    fi

    if [[ "$FLAG_PURGE_UPGRADES" -eq 1 ]]; then
        log_info "Purging unattended-upgrades drop-ins and timer overrides (--purge-upgrades)..."
        rm -f "$APT_LOCAL_UPGRADES" "$APT_AUTO_UPGRADES" "$APT_LEGACY_MAIL2HOOK" "$APT_MAIL_ORIG_BACKUP"
        rm -f "$TIMER_DAILY_CONF" "$TIMER_UPGRADE_CONF"
        rmdir --ignore-fail-on-non-empty "$TIMER_DAILY_DIR" "$TIMER_UPGRADE_DIR" 2>/dev/null || true
        if is_systemd_running; then
            systemctl daemon-reload
            systemctl restart apt-daily.timer apt-daily-upgrade.timer 2>/dev/null || true
        fi
        record_change "Purged unattended-upgrades drop-ins and timer overrides"
        log_info "Removed unattended-upgrades local drop-ins and timer overrides."
    else
        restore_apt_mail_target "$APT_LOCAL_UPGRADES" "$APT_MAIL_ORIG_BACKUP" "$BACKUP_DIR"
        log_info "Left unattended-upgrades configuration in place (pass --purge-upgrades to remove)."
    fi

    if [[ "${#CHANGES_MADE[@]}" -gt 0 ]]; then
        printf '\nUninstall Changes:\n'
        local chg
        for chg in "${CHANGES_MADE[@]}"; do
            printf '  - %s\n' "$chg"
        done
    fi
    if [[ "${#BACKUPS_CREATED[@]}" -gt 0 ]]; then
        printf '\nBackups Saved (%s):\n' "$BACKUP_DIR"
        local bak
        for bak in "${BACKUPS_CREATED[@]}"; do
            printf '  - %s\n' "$bak"
        done
    fi

    log_info "Uninstall complete."
}

main() {
    parse_args "$@"
    ensure_root "$@"
    resolve_env_group

    if [[ "$FLAG_UNINSTALL" -eq 1 ]]; then
        run_uninstall
        return 0
    fi

    detect_os
    migrate_legacy_service
    resolve_webhook_url
    install_system_packages
    install_mail2hook_binary
    write_env_file
    configure_mail_integration
    configure_unattended_upgrades
    run_sendmail_e2e_test
    print_summary
}

if [[ -z "${BASH_SOURCE[0]:-}" || "${BASH_SOURCE[0]}" == "${0}" ]]; then
    main "$@"
fi
