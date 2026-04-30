#!/usr/bin/env bash
# Configurazione UFW: apre solo porte essenziali (22 SSH, 25 SMTP, 80 HTTP per ACME, 443/admin HTTPS).

set -euo pipefail

if ! command -v ufw >/dev/null; then
    echo "[05-firewall] ufw non installato, skip"
    exit 0
fi

# Reset solo se NON c'è policy attiva (evita di sganciarci se siamo via SSH)
if ! ufw status | grep -q "Status: active"; then
    ufw default deny incoming
    ufw default allow outgoing
    ufw allow 22/tcp comment 'SSH'
fi

ufw allow 80/tcp comment 'HTTP (Let'\''s Encrypt + redirect)' >/dev/null
ufw allow ${ADMIN_PORT:-443}/tcp comment 'HTTPS admin' >/dev/null
ufw allow ${SMTP_PORT:-25}/tcp comment 'SMTP listener' >/dev/null

# Opzionale: se admin port diverso da 443, aprire anche 443 per redirect HTTPS standard
if [[ "${ADMIN_PORT:-443}" != "443" ]]; then
    ufw allow 443/tcp comment 'HTTPS standard' >/dev/null 2>&1 || true
fi

# Abilita ufw se non attivo (force=yes per non chiedere prompt y/n)
if ! ufw status | grep -q "Status: active"; then
    echo "y" | ufw enable
fi

echo "[05-firewall] UFW configurato:"
ufw status numbered | tail -10
