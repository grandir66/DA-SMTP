#!/usr/bin/env bash
# Deploy applicazione: clona repo, crea .venv, installa dipendenze, applica migrations.
# Idempotente: rerun aggiorna a HEAD del git ref.

set -euo pipefail

APP_DIR="/opt/domarc-smtp-relay-admin"
LISTENER_DIR="/opt/stormshield-smtp-relay"
LISTENER_TARBALL_DIR="${LISTENER_TARBALL_DIR:-/opt/domarc-smtp-relay-admin/installer/listener-bundle}"
SECRETS_FILE="/etc/domarc-smtp-relay-admin/secrets.env"

# === 1. Admin web — clone o update ===
if [[ ! -d "$APP_DIR/.git" ]]; then
    if [[ -d "$APP_DIR" && "$(ls -A $APP_DIR 2>/dev/null)" ]]; then
        # Esiste ma non è un git repo: backup + clean
        BACKUP="/var/lib/domarc-installer/admin-pre-clone-$(date +%s).tar.gz"
        echo "[04-app-deploy] $APP_DIR esiste ma non è git repo: backup in $BACKUP"
        tar czf "$BACKUP" -C "$APP_DIR" . 2>/dev/null || true
    fi
    rm -rf "$APP_DIR" 2>/dev/null || true
    git clone --branch "${GIT_REF:-main}" "$REPO_URL" "$APP_DIR"
else
    cd "$APP_DIR"
    git fetch origin
    git checkout "${GIT_REF:-main}"
    git pull --ff-only origin "${GIT_REF:-main}" || echo "[04-app-deploy] pull non fast-forward, skip"
fi

chown -R domarc-relay:domarc-relay "$APP_DIR"

# === 2. Admin venv ===
if [[ ! -d "$APP_DIR/.venv" ]]; then
    sudo -u domarc-relay python3 -m venv "$APP_DIR/.venv"
fi
sudo -u domarc-relay "$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip wheel
sudo -u domarc-relay "$APP_DIR/.venv/bin/pip" install --quiet -e "$APP_DIR" || \
    sudo -u domarc-relay "$APP_DIR/.venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt" 2>/dev/null || \
    echo "[04-app-deploy] WARN: pip install non completato — verifica pyproject.toml o requirements.txt"

# Pacchetti runtime obbligatori
sudo -u domarc-relay "$APP_DIR/.venv/bin/pip" install --quiet \
    flask flask-wtf gunicorn cryptography pyyaml httpx \
    || echo "[04-app-deploy] WARN: install pacchetti core fallito"

# Pacchetti opzionali wizard (DB gestionale)
sudo -u domarc-relay "$APP_DIR/.venv/bin/pip" install --quiet \
    psycopg2-binary pyodbc 2>/dev/null || echo "[04-app-deploy] WARN: psycopg2/pyodbc non installati (richiesto per wizard DB)"

# Pacchetto Anthropic (per AI integration)
sudo -u domarc-relay "$APP_DIR/.venv/bin/pip" install --quiet anthropic 2>/dev/null || true

# === 3. Listener — installa da bundle locale (se presente) ===
if [[ -d "$LISTENER_TARBALL_DIR" ]] && [[ -f "$LISTENER_TARBALL_DIR/relay-bundle.tar.gz" ]]; then
    echo "[04-app-deploy] estrazione listener da bundle..."
    install -d -o stormshield-relay -g stormshield-relay "$LISTENER_DIR"
    tar -xzf "$LISTENER_TARBALL_DIR/relay-bundle.tar.gz" -C "$LISTENER_DIR" --strip-components=0
    chown -R stormshield-relay:stormshield-relay "$LISTENER_DIR"
    if [[ ! -d "$LISTENER_DIR/.venv" ]]; then
        sudo -u stormshield-relay python3 -m venv "$LISTENER_DIR/.venv"
    fi
    sudo -u stormshield-relay "$LISTENER_DIR/.venv/bin/pip" install --quiet --upgrade pip wheel
    sudo -u stormshield-relay "$LISTENER_DIR/.venv/bin/pip" install --quiet \
        aiosmtpd dnspython httpx pyyaml || echo "[04-app-deploy] WARN: pip listener fallito"
    [[ -f "$LISTENER_DIR/pyproject.toml" ]] && \
        sudo -u stormshield-relay "$LISTENER_DIR/.venv/bin/pip" install --quiet -e "$LISTENER_DIR"
else
    echo "[04-app-deploy] WARN: nessun bundle listener in $LISTENER_TARBALL_DIR — solo admin web installato. Per il listener completo, copia il bundle prima di rilanciare."
fi

# === 4. CAP_NET_BIND_SERVICE su Python venv del listener (porta 25 senza root) ===
if [[ -f "$LISTENER_DIR/.venv/bin/python3" ]]; then
    # Rispetta i symlink: applica capability al binario reale
    REAL_PY="$(readlink -f "$LISTENER_DIR/.venv/bin/python3")"
    setcap 'cap_net_bind_service=+ep' "$REAL_PY" || echo "[04-app-deploy] WARN: setcap fallito su $REAL_PY"
fi

# === 5. SECRET_KEY e RELAY_API_KEY se non presenti ===
if [[ ! -s "$SECRETS_FILE" ]] || ! grep -q "^SECRET_KEY=" "$SECRETS_FILE" 2>/dev/null; then
    SK="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"
    {
        echo "# Generati dall'installer il $(date -Iseconds)"
        echo "SECRET_KEY=$SK"
    } >> "$SECRETS_FILE"
    chmod 0640 "$SECRETS_FILE"
fi

# Listener secrets.env
LISTENER_SECRETS="/etc/stormshield-smtp-relay/secrets.env"
if [[ ! -f "$LISTENER_SECRETS" ]] || ! grep -q "^RELAY_API_KEY=" "$LISTENER_SECRETS" 2>/dev/null; then
    install -d -o root -g stormshield-relay -m 0750 /etc/stormshield-smtp-relay
    AK="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"
    {
        echo "# Generati dall'installer il $(date -Iseconds)"
        echo "RELAY_API_KEY=$AK"
    } >> "$LISTENER_SECRETS"
    chmod 0640 "$LISTENER_SECRETS"
    chown root:stormshield-relay "$LISTENER_SECRETS"
fi

# === 6. Migrations admin (al primo start dell'app è automatico, ma forziamo qui) ===
sudo -u domarc-relay "$APP_DIR/.venv/bin/python3" -c "
from domarc_relay_admin.config import load_config
from domarc_relay_admin.storage import get_storage
storage = get_storage(load_config())
n = storage.apply_migrations() if hasattr(storage, 'apply_migrations') else 0
print(f'[04-app-deploy] migrations applicate: {n}')
" 2>&1 | tail -5

# === 7. Enable + start servizi ===
systemctl enable domarc-smtp-relay-admin >/dev/null
[[ -f "$LISTENER_DIR/.venv/bin/relay" ]] && systemctl enable stormshield-smtp-relay-listener stormshield-smtp-relay-scheduler >/dev/null

systemctl restart domarc-smtp-relay-admin
[[ -f "$LISTENER_DIR/.venv/bin/relay" ]] && systemctl restart stormshield-smtp-relay-listener stormshield-smtp-relay-scheduler

sleep 3
echo "[04-app-deploy] stato servizi:"
systemctl is-active domarc-smtp-relay-admin && echo "  admin: active"
[[ -f "$LISTENER_DIR/.venv/bin/relay" ]] && systemctl is-active stormshield-smtp-relay-listener && echo "  listener: active"
[[ -f "$LISTENER_DIR/.venv/bin/relay" ]] && systemctl is-active stormshield-smtp-relay-scheduler && echo "  scheduler: active"

echo "[04-app-deploy] OK"
