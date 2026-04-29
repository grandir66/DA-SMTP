# Domarc SMTP Relay — Admin Web

Web admin **standalone** del Domarc SMTP Relay: Flask + SQLite (default) o PostgreSQL,
multi-tenant first-class, auth locale, anagrafica clienti pluggabile (YAML / SQLite /
REST / Stormshield Manager).

> **Open core**: il core di questa cartella è MIT, distribuito anche separatamente.
> Edizione **Pro** (LDAP/AD, SSO/OIDC, console multi-tenant centrale, supporto SLA)
> ha licenza separata.

## Stato attuale

**Alpha** — in fase di estrazione dal manager Domarc.
v1.0 attesa entro 9 settimane dal kick-off (vedi [PIANO](../../docs/materiale/PIANO_SMTP_RELAY_STANDALONE.md)).

## Quickstart sviluppo (su 192.168.4.41 accanto al manager)

```bash
cd /opt/domarc/stormshield-manager/web_interface/services/smtp_relay/admin/
python3 -m venv .venv
source .venv/bin/activate
pip install -e .

# Avvio dev (porta 8443)
export DOMARC_RELAY_DB_PATH=/var/lib/domarc-smtp-relay/admin.db
export DOMARC_RELAY_SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_hex(32))")
domarc-smtp-relay-admin serve --port 8443
```

Apri https://localhost:8443 → primo accesso usa l'admin auto-creato dalla migration.

## Architettura in breve

```
domarc_relay_admin/
├── app.py              # Flask app factory
├── cli.py              # entrypoint comandi (serve, migrate, ...)
├── config.py           # caricamento config da env / domarc-relay.yaml
├── auth/               # login locale + bcrypt + sessione
├── tenants/            # middleware multi-tenant + DAO
├── customer_sources/   # 4 adapter pluggabili
│   ├── base.py
│   ├── yaml_source.py
│   ├── sqlite_source.py
│   ├── rest_source.py
│   └── stormshield_source.py
├── storage/            # DAO astratto
│   ├── base.py
│   ├── sqlite_impl.py
│   └── postgres_impl.py
├── routes/             # blueprint per le 6 macroaree UI
├── templates/admin/    # Jinja2 self-contained (no estensione manager)
├── static/             # CSS/JS Domarc-branded
└── migrations/         # 00X_*.sqlite.sql + 00X_*.pg.sql
```

## Configurazione

Variabili d'ambiente principali (tutte prefisso `DOMARC_RELAY_`):

| Var | Default | Descrizione |
|-----|---------|-------------|
| `DOMARC_RELAY_DB_BACKEND` | `sqlite` | `sqlite` o `postgres` |
| `DOMARC_RELAY_DB_PATH` | `/var/lib/domarc-smtp-relay/admin.db` | path SQLite |
| `DOMARC_RELAY_DB_DSN` | (vuoto) | DSN postgres (se backend=postgres) |
| `DOMARC_RELAY_SECRET_KEY` | (genera al primo avvio) | Flask session secret |
| `DOMARC_RELAY_BIND_HOST` | `127.0.0.1` | bind host |
| `DOMARC_RELAY_BIND_PORT` | `8443` | bind port |
| `DOMARC_RELAY_CUSTOMER_SOURCE` | `yaml` | adapter customer (vedi sotto) |
| `DOMARC_RELAY_TELEMETRY_URL` | (vuoto) | heartbeat opt-in (Pro) |

Customer source adapter (uno tra):

```yaml
customer_source:
  backend: "yaml"           # yaml | sqlite | rest | stormshield
  yaml:
    path: /etc/domarc-smtp-relay/customers.yaml
  rest:
    base_url: https://crm.cliente.com/api/v1
    api_key_env: CRM_API_KEY
  stormshield:
    base_url: https://manager-dev.domarc.it
    api_key_env: STORMSHIELD_RELAY_API_KEY
```

## Licenza

Core MIT. Vedi [LICENSE](LICENSE).
