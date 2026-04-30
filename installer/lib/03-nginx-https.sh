#!/usr/bin/env bash
# Configura nginx come reverse proxy + HTTPS (Let's Encrypt o self-signed). Idempotente.

set -euo pipefail

INSTALLER_DIR="${INSTALLER_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
TPL="$INSTALLER_DIR/templates/nginx.conf.tpl"
CONFIG_OUT="/etc/nginx/sites-available/domarc-relay"
ENABLED_LINK="/etc/nginx/sites-enabled/domarc-relay"

SERVER_NAME="${DOMAIN:-${HOSTNAME_ARG:-localhost}}"

# === 1. Generazione certificato ===
case "$HTTPS_MODE" in
    letsencrypt)
        SSL_CERT_DIR="/etc/letsencrypt/live/$DOMAIN"
        SSL_CERT="$SSL_CERT_DIR/fullchain.pem"
        SSL_KEY="$SSL_CERT_DIR/privkey.pem"

        # Pre-prepare ACME challenge dir
        install -d -o www-data -g www-data /var/www/letsencrypt

        # Se cert non esiste o sta per scadere (<30gg), richiedilo
        if [[ ! -f "$SSL_CERT" ]] || ! openssl x509 -checkend 2592000 -noout -in "$SSL_CERT" 2>/dev/null; then
            echo "[03-nginx-https] richiesta cert Let's Encrypt per $DOMAIN..."
            STAGING_FLAG=""
            [[ "${CERTBOT_STAGING:-0}" == "1" ]] && STAGING_FLAG="--staging"
            certbot certonly --webroot -w /var/www/letsencrypt \
                -d "$DOMAIN" \
                --email "$LETSENCRYPT_EMAIL" \
                --agree-tos --non-interactive \
                $STAGING_FLAG \
                || { echo "[03-nginx-https] certbot fallito — verifica DNS e raggiungibilità HTTP da internet" >&2; exit 1; }
        else
            echo "[03-nginx-https] cert Let's Encrypt esistente e valido"
        fi
        ;;
    selfsigned)
        SSL_CERT_DIR="/etc/ssl/domarc-relay"
        SSL_CERT="$SSL_CERT_DIR/fullchain.pem"
        SSL_KEY="$SSL_CERT_DIR/privkey.pem"
        install -d -o root -g root -m 0755 "$SSL_CERT_DIR"
        if [[ ! -f "$SSL_CERT" ]]; then
            echo "[03-nginx-https] generazione cert self-signed per $SERVER_NAME (validità 5 anni)..."
            openssl req -x509 -newkey rsa:4096 -sha256 \
                -days 1825 -nodes \
                -keyout "$SSL_KEY" \
                -out "$SSL_CERT" \
                -subj "/CN=$SERVER_NAME/O=Domarc Internal" \
                -addext "subjectAltName=DNS:$SERVER_NAME,DNS:localhost,IP:127.0.0.1"
            chmod 600 "$SSL_KEY"
        fi
        ;;
    *)
        echo "[03-nginx-https] HTTPS_MODE non valido: $HTTPS_MODE" >&2
        exit 1;;
esac

# === 2. Render template nginx ===
sed \
    -e "s|__SERVER_NAME__|$SERVER_NAME|g" \
    -e "s|__ADMIN_PORT__|${ADMIN_PORT:-443}|g" \
    -e "s|__SSL_CERT__|$SSL_CERT|g" \
    -e "s|__SSL_KEY__|$SSL_KEY|g" \
    "$TPL" > "$CONFIG_OUT"

# Disabilita default nginx (rimuove redirect/welcome page) e abilita il nostro
[[ -L /etc/nginx/sites-enabled/default ]] && rm /etc/nginx/sites-enabled/default
[[ -L "$ENABLED_LINK" ]] || ln -s "$CONFIG_OUT" "$ENABLED_LINK"

# === 3. Test config + reload ===
nginx -t || { echo "[03-nginx-https] nginx -t fallito" >&2; exit 1; }
systemctl restart nginx
systemctl enable nginx >/dev/null 2>&1 || true

echo "[03-nginx-https] OK nginx attivo su :${ADMIN_PORT:-443} (HTTPS) verso 127.0.0.1:5443"
echo "[03-nginx-https] Cert: $SSL_CERT"
