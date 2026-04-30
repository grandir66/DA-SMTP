#!/usr/bin/env bash
# Prep OS: aggiorna apt, installa pacchetti necessari (Python, nginx, certbot, sqlite3, ecc.)
# Idempotente. Le variabili d'ambiente arrivano da install.sh (DOMAIN, HTTPS_MODE, etc.).

set -euo pipefail

apt-get update -qq

PACKAGES=(
    python3 python3-venv python3-pip python3-dev
    build-essential
    sqlite3
    nginx
    git
    curl
    ca-certificates
    libcap2-bin                          # per setcap (porta 25 senza root)
    libssl-dev libffi-dev                # per Fernet/cryptography
    openssl
    ufw
)

if [[ "$HTTPS_MODE" == "letsencrypt" ]]; then
    PACKAGES+=(certbot python3-certbot-nginx)
fi

# Driver ODBC per MSSQL (gestionale Domarc usa SQL Server). Aggiunge repo Microsoft
# se non presente. Pacchetto richiesto da pyodbc per il wizard.
if ! dpkg -l msodbcsql18 2>/dev/null | grep -q ^ii; then
    if ! curl --silent --output /dev/null --fail https://packages.microsoft.com 2>/dev/null; then
        echo "[01-os-prep] Repo Microsoft non raggiungibile — skip pyodbc/MSSQL (wizard chiederà installazione manuale)"
    else
        curl -fsSL https://packages.microsoft.com/keys/microsoft.asc | gpg --dearmor -o /usr/share/keyrings/microsoft-archive-keyring.gpg
        OS_VERSION="$(lsb_release -rs 2>/dev/null || echo 22.04)"
        OS_CODENAME="$(lsb_release -is 2>/dev/null | tr A-Z a-z || echo ubuntu)"
        echo "deb [arch=amd64,arm64,armhf signed-by=/usr/share/keyrings/microsoft-archive-keyring.gpg] https://packages.microsoft.com/${OS_CODENAME}/${OS_VERSION}/prod ${OS_CODENAME%%-*}-$(lsb_release -cs) main" \
            > /etc/apt/sources.list.d/mssql-release.list 2>/dev/null || true
        apt-get update -qq || true
        ACCEPT_EULA=Y apt-get install -y -qq msodbcsql18 unixodbc-dev || \
            echo "[01-os-prep] msodbcsql18 install fallito — wizard MSSQL non disponibile (potrai usare PostgreSQL come gestionale)"
    fi
fi

apt-get install -y -qq "${PACKAGES[@]}"

# Versione Python — verifica >=3.11
PY_VER="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
if [[ "$(echo "$PY_VER" | tr -d .)" -lt 311 ]]; then
    echo "[01-os-prep] Python $PY_VER troppo vecchio (richiesto >=3.11)" >&2
    echo "[01-os-prep] Su Debian 11/Ubuntu 20.04: aggiungi PPA deadsnakes o aggiorna OS" >&2
    exit 1
fi

# Abilita timesync NTP (cert HTTPS richiede orario corretto)
timedatectl set-ntp true 2>/dev/null || true

echo "[01-os-prep] OK pacchetti installati. Python $PY_VER, nginx $(nginx -v 2>&1 | awk -F/ '{print $2}'), sqlite3 $(sqlite3 --version | awk '{print $1}')"
