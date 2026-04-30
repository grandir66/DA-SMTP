#!/usr/bin/env bash
# Domarc SMTP Relay — Installer entry point.
#
# Esegue in sequenza gli script lib/ in modo idempotente. State file
# in /var/lib/domarc-installer/state.json traccia gli step completati,
# rerun salta quelli già OK (override con --force).
#
# Uso minimo:
#   sudo ./install.sh --domain relay.example.com --https-mode letsencrypt --email admin@example.com
#
# Sviluppo/intranet:
#   sudo ./install.sh --hostname relay-dev.local --https-mode selfsigned

set -euo pipefail

# === Defaults ===
DOMAIN=""
HOSTNAME_ARG=""
HTTPS_MODE="letsencrypt"
LETSENCRYPT_EMAIL=""
SMTP_PORT="25"
ADMIN_PORT="443"
REPO_URL="https://github.com/grandir66/DA-SMTP.git"
GIT_REF="main"
SKIP_WIZARD="0"
DRY_RUN="0"
FORCE="0"
CERTBOT_STAGING="0"

# === Color / logging ===
GRN="\033[0;32m"; YEL="\033[1;33m"; RED="\033[0;31m"; NC="\033[0m"
log()  { echo -e "${GRN}[install]${NC} $*"; }
warn() { echo -e "${YEL}[install]${NC} $*" >&2; }
err()  { echo -e "${RED}[install]${NC} $*" >&2; }
die()  { err "$*"; exit 1; }

# === Arg parsing ===
while [[ $# -gt 0 ]]; do
    case "$1" in
        --domain)            DOMAIN="$2"; shift 2;;
        --hostname)          HOSTNAME_ARG="$2"; shift 2;;
        --https-mode)        HTTPS_MODE="$2"; shift 2;;
        --email)             LETSENCRYPT_EMAIL="$2"; shift 2;;
        --smtp-port)         SMTP_PORT="$2"; shift 2;;
        --admin-port)        ADMIN_PORT="$2"; shift 2;;
        --repo-url)          REPO_URL="$2"; shift 2;;
        --git-ref)           GIT_REF="$2"; shift 2;;
        --skip-wizard)       SKIP_WIZARD="1"; shift;;
        --dry-run)           DRY_RUN="1"; shift;;
        --force)             FORCE="1"; shift;;
        --certbot-staging)   CERTBOT_STAGING="1"; shift;;
        --help|-h)
            grep -E "^# " "$0" | head -20 | sed 's/^# //'
            exit 0;;
        *) die "Argomento sconosciuto: $1 (usa --help)";;
    esac
done

# === Validazioni iniziali ===
[[ $EUID -eq 0 ]] || die "Esegui come root (sudo)."

if [[ "$HTTPS_MODE" == "letsencrypt" ]]; then
    [[ -n "$DOMAIN" ]] || die "--domain obbligatorio con --https-mode letsencrypt"
    [[ -n "$LETSENCRYPT_EMAIL" ]] || die "--email obbligatorio con --https-mode letsencrypt"
fi
if [[ "$HTTPS_MODE" == "selfsigned" ]]; then
    [[ -n "$HOSTNAME_ARG" ]] || HOSTNAME_ARG="$(hostname -f 2>/dev/null || hostname)"
fi
[[ "$HTTPS_MODE" =~ ^(letsencrypt|selfsigned)$ ]] || die "--https-mode deve essere 'letsencrypt' o 'selfsigned'"

INSTALLER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE_DIR="/var/lib/domarc-installer"
STATE_FILE="$STATE_DIR/state.json"
mkdir -p "$STATE_DIR"

# === Export variables for sub-scripts ===
export DOMAIN HOSTNAME_ARG HTTPS_MODE LETSENCRYPT_EMAIL SMTP_PORT ADMIN_PORT
export REPO_URL GIT_REF DRY_RUN FORCE CERTBOT_STAGING INSTALLER_DIR STATE_FILE

# === Helper: marca step completato ===
mark_done() {
    local step="$1"
    [[ "$DRY_RUN" == "1" ]] && return
    local tmp="$STATE_DIR/.state.tmp"
    if [[ -f "$STATE_FILE" ]]; then
        python3 -c "
import json, sys
data = json.load(open('$STATE_FILE'))
data['$step'] = {'done_at': '$(date -Iseconds)', 'host': '$(hostname)'}
json.dump(data, open('$tmp', 'w'), indent=2)
"
    else
        echo "{\"$step\": {\"done_at\": \"$(date -Iseconds)\", \"host\": \"$(hostname)\"}}" > "$tmp"
    fi
    mv "$tmp" "$STATE_FILE"
}
export -f mark_done

is_done() {
    local step="$1"
    [[ "$FORCE" == "1" ]] && return 1
    [[ -f "$STATE_FILE" ]] || return 1
    python3 -c "
import json
data = json.load(open('$STATE_FILE'))
import sys; sys.exit(0 if '$step' in data else 1)
"
}
export -f is_done

# === Banner ===
cat <<EOF

╔══════════════════════════════════════════════════════════╗
║  Domarc SMTP Relay — Installer                          ║
║  Configurazione:                                         ║
║    HTTPS mode  : $HTTPS_MODE
║    Domain      : ${DOMAIN:-(N/A)}
║    Hostname    : ${HOSTNAME_ARG:-(N/A)}
║    SMTP port   : $SMTP_PORT
║    Admin port  : $ADMIN_PORT
║    Repo        : $REPO_URL @ $GIT_REF
║    Dry run     : $DRY_RUN
╚══════════════════════════════════════════════════════════╝

EOF

# === Esecuzione step ===
run_step() {
    local script="$1" name="$2"
    if is_done "$name"; then
        log "SKIP $name (già completato — usa --force per rieseguire)"
        return
    fi
    log "▶ $name"
    if [[ "$DRY_RUN" == "1" ]]; then
        warn "(dry-run: $INSTALLER_DIR/lib/$script)"
        return
    fi
    bash "$INSTALLER_DIR/lib/$script" || die "Step '$name' fallito."
    mark_done "$name"
    log "✓ $name"
}

run_step "01-os-prep.sh"      "01_os_prep"
run_step "02-users-systemd.sh" "02_users_systemd"
run_step "04-app-deploy.sh"    "04_app_deploy"
run_step "03-nginx-https.sh"   "03_nginx_https"
run_step "05-firewall.sh"      "05_firewall"

log "════════════════════════════════════════════════════════"
log "Installazione completata. Servizi systemd:"
log "  systemctl status domarc-smtp-relay-admin"
log "  systemctl status stormshield-smtp-relay-listener"
log "  systemctl status stormshield-smtp-relay-scheduler"
log "════════════════════════════════════════════════════════"

if [[ "$SKIP_WIZARD" != "1" ]]; then
    log "Lancio il wizard di configurazione..."
    sudo -u domarc-relay python3 "$INSTALLER_DIR/wizard/wizard.py" || warn "Wizard interrotto. Rilancialo con: sudo -u domarc-relay python3 $INSTALLER_DIR/wizard/wizard.py"
fi

log "Done. Apri https://${DOMAIN:-$HOSTNAME_ARG}/"
