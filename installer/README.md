# Domarc SMTP Relay — Installer

Installer automatico per VM Ubuntu 22.04+ / Debian 12+. Installa il sistema completo
in pochi minuti: pacchetti, utenti, servizi, nginx + HTTPS, applicazione, primo admin.

## Cosa installa

| Componente | Versione minima | Sorgente |
|---|---|---|
| Python | 3.11+ | apt |
| nginx | 1.18+ | apt |
| certbot (Let's Encrypt) | 1.21+ | apt |
| sqlite3 | 3.35+ | apt |
| Domarc SMTP Relay Admin | da repo `grandir66/DA-SMTP` | git |
| Domarc SMTP Relay Listener | da bundle locale | tarball |

## Layout finale

```
/opt/domarc-smtp-relay-admin/      # admin web (Flask, porta 5443)
/opt/stormshield-smtp-relay/       # listener SMTP (porta 25) + scheduler
/var/lib/domarc-smtp-relay-admin/  # admin.db + master.key (Fernet)
/var/lib/stormshield-smtp-relay/   # relay.db (events_log + outbound queue)
/etc/nginx/sites-enabled/domarc-relay  # reverse proxy → :5443
/etc/letsencrypt/                   # certificati (se modalità letsencrypt)
/etc/systemd/system/domarc-smtp-relay-admin.service
/etc/systemd/system/stormshield-smtp-relay-listener.service
/etc/systemd/system/stormshield-smtp-relay-scheduler.service
```

## Quick start

```bash
# Su VM Ubuntu/Debian appena installata, come root:
git clone git@github.com:grandir66/DA-SMTP.git /tmp/da-smtp
cd /tmp/da-smtp/installer
sudo ./install.sh --domain relay.example.com --https-mode letsencrypt --email admin@example.com

# Oppure modalità sviluppo/intranet (cert self-signed):
sudo ./install.sh --hostname relay-dev.local --https-mode selfsigned

# Dopo l'install, parte automaticamente il wizard:
sudo /opt/domarc-smtp-relay-admin/installer/wizard/wizard.py
```

## Flag install.sh

| Flag | Descrizione | Esempio |
|---|---|---|
| `--domain` | FQDN per cert Let's Encrypt | `--domain relay.example.com` |
| `--hostname` | hostname interno (selfsigned) | `--hostname relay-dev.local` |
| `--https-mode` | `letsencrypt` (default) o `selfsigned` | `--https-mode selfsigned` |
| `--email` | email per Let's Encrypt | `--email admin@example.com` |
| `--smtp-port` | porta SMTP listener (default 25) | `--smtp-port 2525` |
| `--admin-port` | porta nginx HTTPS (default 443) | `--admin-port 8443` |
| `--repo-url` | repo git da clonare | `--repo-url git@github.com:grandir66/DA-SMTP.git` |
| `--git-ref` | branch/tag (default `main`) | `--git-ref v0.9.0-pre-prod` |
| `--skip-wizard` | salta wizard finale (per CI) | |
| `--dry-run` | stampa solo i comandi che eseguirebbe | |

## Wizard post-install

Dopo l'installazione il wizard CLI configura:

1. **Connessione gestionale (DB clienti)**
   - Backend: PostgreSQL o MSSQL
   - Host, port, database, user, password
   - Test connessione + lettura sample tabella `clienti`
2. **API ticketing del gestionale**
   - Base URL (es. `https://manager.example.com`)
   - API key (cifrata Fernet in admin.db)
   - Test `GET /api/v1/health`
3. **Provider AI (opzionale)**
   - Anthropic API key (cifrata)
   - Test 1 chiamata `claude-haiku-4-5` con prompt minimale
4. **Bootstrap utente admin**
   - Username, email, password (hash bcrypt)
   - Crea ruolo `superadmin` per il primo utente

## Backup e restore

```bash
# Backup completo (DB + secrets + config + master.key)
sudo /opt/domarc-smtp-relay-admin/installer/backup-restore/domarc-backup.py \
    --output /root/backup-$(date +%F).tar.gz.enc \
    --passphrase "il-tuo-segreto"

# Restore su altro sistema
sudo /opt/domarc-smtp-relay-admin/installer/backup-restore/domarc-restore.py \
    --input /root/backup-2026-04-30.tar.gz.enc \
    --passphrase "il-tuo-segreto"
```

Cosa contiene il bundle (cifrato AES-256 via Fernet con passphrase derivata):
- `admin.db` (regole, gruppi, utenti, audit, eventi recenti)
- `relay.db` (cache listener)
- `master.key` (chiave Fernet → senza questa le API key cifrate sono illegibili)
- `secrets.env`, `*.yaml` (config listener + admin)
- `nginx.conf`, systemd unit files
- file `state.json` con metadata (data backup, hostname sorgente, versione)

> **Attenzione**: il bundle contiene segreti in cifrato + master.key in cifrato.
> Custodire la passphrase con cura — senza, il backup è illegibile.

## Idempotenza

Tutti gli script sono idempotenti: puoi rilanciare `install.sh` su un sistema
già installato e re-applicherà solo i passi mancanti. Lo state file
`/var/lib/domarc-installer/state.json` traccia i passi completati.

## Troubleshooting

| Sintomo | Causa probabile | Fix |
|---|---|---|
| `nginx: bind() to 0.0.0.0:443 failed` | porta 443 già occupata | `--admin-port 8443` o ferma altro nginx |
| `letsencrypt: connection refused` | DNS non punta alla VM | Verifica `dig +short DOMAIN` |
| `certbot: rate limit` | troppi tentativi | Usa staging: `--certbot-staging` |
| `listener: permission denied su porta 25` | utente senza CAP_NET_BIND | `setcap` già applicato dall'installer; verifica con `getcap` |
| `admin: 502 Bad Gateway` | servizio admin non parte | `journalctl -u domarc-smtp-relay-admin -n 50` |

## Architettura post-install

```
internet  ───▶  nginx :443 (HTTPS)  ───▶  admin :5443 (Flask)
                                            │
internet  ───▶  listener :25 (SMTP)         │
                  │                         │
                  ▼                         ▼
           SQLite relay.db            SQLite admin.db
                  │                         │
                  └──── HTTP API ────▶ /api/v1/relay/*
                       (X-API-Key)
```
