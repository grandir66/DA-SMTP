#!/usr/bin/env bash
# Crea utenti di servizio + cartelle dati + units systemd. Idempotente.

set -euo pipefail

# === Utenti di servizio ===
# domarc-relay: app admin web (Flask)
# stormshield-relay: listener SMTP + scheduler (porta 25 — capability invece di root)
for u in domarc-relay stormshield-relay; do
    if ! id -u "$u" >/dev/null 2>&1; then
        useradd --system --no-create-home --shell /usr/sbin/nologin "$u"
        echo "[02-users-systemd] creato utente $u"
    fi
done

# admin user nel gruppo del listener (cross-service read-only sul DB del listener)
usermod -aG stormshield-relay domarc-relay 2>/dev/null || true

# === Directory dati con permessi corretti ===
install -d -o domarc-relay      -g domarc-relay      -m 0750 /var/lib/domarc-smtp-relay-admin
install -d -o stormshield-relay -g stormshield-relay -m 0750 /var/lib/stormshield-smtp-relay
install -d -o root              -g root              -m 0755 /etc/stormshield-smtp-relay
install -d -o root              -g root              -m 0755 /var/log/domarc-smtp-relay-admin
install -d -o root              -g root              -m 0755 /var/log/stormshield-smtp-relay

# === Bind capability su porta 25 senza root (CAP_NET_BIND_SERVICE) ===
# Lo applichiamo dopo il deploy quando il binario .venv esiste.
# Qui solo verifica che setcap sia disponibile.
command -v setcap >/dev/null || { echo "[02-users-systemd] setcap non disponibile" >&2; exit 1; }

# === systemd units ===
cat > /etc/systemd/system/domarc-smtp-relay-admin.service <<'EOF'
[Unit]
Description=Domarc SMTP Relay - Admin Web (Flask)
After=network.target
Wants=network.target

[Service]
Type=exec
User=domarc-relay
Group=domarc-relay
WorkingDirectory=/opt/domarc-smtp-relay-admin
Environment="PATH=/opt/domarc-smtp-relay-admin/.venv/bin:/usr/bin:/bin"
EnvironmentFile=-/etc/domarc-smtp-relay-admin/secrets.env
ExecStart=/opt/domarc-smtp-relay-admin/.venv/bin/gunicorn \
    -w 2 -b 127.0.0.1:5443 \
    -k gthread --threads 4 --timeout 60 \
    --access-logfile /var/log/domarc-smtp-relay-admin/access.log \
    --error-logfile /var/log/domarc-smtp-relay-admin/error.log \
    "domarc_relay_admin:create_app()"
Restart=on-failure
RestartSec=5

# Hardening
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
NoNewPrivileges=true
ReadWritePaths=/var/lib/domarc-smtp-relay-admin /var/log/domarc-smtp-relay-admin

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/stormshield-smtp-relay-listener.service <<'EOF'
[Unit]
Description=Stormshield SMTP Relay - Listener (aiosmtpd)
After=network.target

[Service]
Type=exec
User=stormshield-relay
Group=stormshield-relay
WorkingDirectory=/opt/stormshield-smtp-relay
Environment="PATH=/opt/stormshield-smtp-relay/.venv/bin:/usr/bin:/bin"
Environment="RELAY_CONFIG=/etc/stormshield-smtp-relay/relay.yaml"
EnvironmentFile=-/etc/stormshield-smtp-relay/secrets.env
ExecStart=/opt/stormshield-smtp-relay/.venv/bin/relay listener
Restart=on-failure
RestartSec=5

# CAP_NET_BIND_SERVICE: serve per legare la porta 25 senza root
AmbientCapabilities=CAP_NET_BIND_SERVICE
CapabilityBoundingSet=CAP_NET_BIND_SERVICE

# Hardening
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
NoNewPrivileges=true
ReadWritePaths=/var/lib/stormshield-smtp-relay /var/log/stormshield-smtp-relay

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/stormshield-smtp-relay-scheduler.service <<'EOF'
[Unit]
Description=Stormshield SMTP Relay - Scheduler (sync + flush + outbound drain)
After=network.target stormshield-smtp-relay-listener.service

[Service]
Type=exec
User=stormshield-relay
Group=stormshield-relay
WorkingDirectory=/opt/stormshield-smtp-relay
Environment="PATH=/opt/stormshield-smtp-relay/.venv/bin:/usr/bin:/bin"
Environment="RELAY_CONFIG=/etc/stormshield-smtp-relay/relay.yaml"
EnvironmentFile=-/etc/stormshield-smtp-relay/secrets.env
ExecStart=/opt/stormshield-smtp-relay/.venv/bin/relay scheduler
Restart=on-failure
RestartSec=5

ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
NoNewPrivileges=true
ReadWritePaths=/var/lib/stormshield-smtp-relay /var/log/stormshield-smtp-relay

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload

# Crea cartella per secrets.env admin (vuota all'inizio, popolata dal wizard)
install -d -o root -g domarc-relay -m 0750 /etc/domarc-smtp-relay-admin
[ -f /etc/domarc-smtp-relay-admin/secrets.env ] || \
    install -o root -g domarc-relay -m 0640 /dev/null /etc/domarc-smtp-relay-admin/secrets.env

echo "[02-users-systemd] OK utenti + cartelle + units systemd creati"
