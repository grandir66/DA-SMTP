# Domarc SMTP Relay — Admin Web (DA-SMTP)

Web admin **standalone** per gestione SMTP relay multi-tenant, con rule engine
deterministico gerarchico, AI Assistant integrato, privacy bypass GDPR e UI
self-contained.

> **Open core MIT** — vedi [LICENSE](LICENSE).
> Edizione **Pro** (LDAP/AD, SSO/OIDC, console multi-tenant centrale, supporto SLA)
> ha licenza separata.

## Stato

**v1.0.0** — Production-ready (release iniziale completata).

## Features principali

- 🌳 **Rule Engine v2** con gerarchia padre/figlio (1 livello), priorità globale
  unica, ereditarietà action_map, validatori V001-V008, simulazione inline,
  wizard "Suggerisci gruppi", flatten verso listener, test parità (88 casi).

- 🛡 **Privacy bypass GDPR**: liste indirizzi e domini esclusi dal rule engine
  con audit log. Pre-check nel listener prima del processing.

- 🤖 **AI Assistant** integrato:
  - Provider pluggabili (Claude API + futuro DGX Spark self-hosted)
  - Routing per job versionato + A/B traffic split
  - PII redactor (regex + spaCy NER italiano + dictionary custom)
  - Decisioni loggate con cost tracking + audit
  - Action `ai_classify` integrata nel listener (timeout 5s + fail-safe forward)
  - **F2 Error Aggregator** — clustering errori semantico + recovery automatico
  - **F3 Shadow → Live** — switch atomico con confidence threshold + pre-flight check
  - **F3.5 Rule Proposer** — learning loop AI → regole statiche

- 🔐 **Settings UI cifrate**: API keys con Fernet encryption, moduli Python
  installabili da UI con whitelist.

- ❤️ **Health check** sistema con 10 component checks + test stack live
  (DB + Fernet + Claude API).

- 📖 **Manual auto-generato** + CHANGELOG visibili in `/manual` (HTML render).

## Quickstart

### Prerequisiti

- Python 3.11+
- SQLite 3.35+ (default) o PostgreSQL 14+ (opzionale)
- nginx (consigliato come reverse proxy TLS davanti a `:5443`)

### Installazione

```bash
# 1. Clone + venv
git clone https://github.com/<tuo-username>/DA-SMTP.git /opt/domarc-smtp-relay-admin
cd /opt/domarc-smtp-relay-admin
python3 -m venv .venv
source .venv/bin/activate
pip install -e .

# 2. Variabili d'ambiente (in /etc/domarc-smtp-relay-admin/secrets.env)
cat > /etc/domarc-smtp-relay-admin/secrets.env <<EOF
DOMARC_RELAY_DB_PATH=/var/lib/domarc-smtp-relay-admin/admin.db
DOMARC_RELAY_BIND_HOST=127.0.0.1
DOMARC_RELAY_BIND_PORT=5443
DOMARC_RELAY_SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_hex(32))")
DOMARC_RELAY_BOOTSTRAP_PASSWORD=changeme-on-first-login
DOMARC_RELAY_CUSTOMER_SOURCE=yaml
EOF
chmod 600 /etc/domarc-smtp-relay-admin/secrets.env

# 3. Apply migrations + first launch
domarc-smtp-relay-admin migrate
domarc-smtp-relay-admin serve

# 4. Login: http://localhost:5443
#    user=admin password=<DOMARC_RELAY_BOOTSTRAP_PASSWORD>
```

### Setup AI Assistant (opzionale)

```bash
# Aggiungi Anthropic SDK (già nelle dipendenze richieste)
pip install anthropic

# (Opzionale) PII redactor con NER nomi italiani
pip install spacy
python -m spacy download it_core_news_sm

# UI: aggiungi API key + provider + binding
# Settings → Chiavi API → Nuova: ANTHROPIC_API_KEY = sk-ant-...
# AI Assistant → Provider → Aggiungi provider Claude API
# AI Assistant → Routing → Crea binding classify_email → Haiku
# Settings → ai_enabled = true (lascia ai_shadow_mode=true per i primi giorni)
```

## Architettura

```
admin/                       (questo repo)
├── domarc_relay_admin/
│   ├── ai_assistant/        # Provider Claude/DGX, router, PII redactor,
│   │   │                    # decisions, error_aggregator, rule_proposer
│   │   ├── providers/       # base + claude + local_http (DGX Spark)
│   │   └── prompts/         # template Jinja2 per ogni job IA
│   ├── customer_sources/    # 4 adapter (yaml/sqlite/rest/stormshield)
│   ├── migrations/          # 17 migrations SQL (sqlite + postgres parità)
│   ├── routes/              # blueprint per le 12 macroaree UI
│   ├── rules/               # engine v2 (validators, flatten, evaluator)
│   ├── storage/             # DAO astratto (sqlite + postgres impl)
│   ├── tenants/              # middleware multi-tenant
│   ├── auth/                 # login locale + bcrypt + sessione
│   ├── secrets_manager.py   # Fernet encryption API keys
│   ├── module_manager.py    # pip install via UI con whitelist
│   ├── manual_generator.py  # auto-generazione manual.md
│   └── app.py               # Flask app factory
├── templates/admin/         # Jinja2 self-contained
├── static/                  # CSS/JS Domarc-branded
├── tests/                   # 162 test pytest
├── docs/                    # guida_funzionamento.md, ai_assistant.md, etc.
├── CHANGELOG.md
└── README.md  (questo file)
```

Il **listener SMTP** vive in un repository separato (`/opt/stormshield-smtp-relay/`)
e parla con questo admin tramite endpoint REST `/api/v1/relay/*`.

## Configurazione completa

Variabili d'ambiente principali (prefisso `DOMARC_RELAY_`):

| Var | Default | Descrizione |
|-----|---------|-------------|
| `DOMARC_RELAY_DB_BACKEND` | `sqlite` | `sqlite` o `postgres` |
| `DOMARC_RELAY_DB_PATH` | `/var/lib/domarc-smtp-relay-admin/admin.db` | path SQLite |
| `DOMARC_RELAY_DB_DSN` | (vuoto) | DSN postgres |
| `DOMARC_RELAY_SECRET_KEY` | (genera al primo avvio) | Flask session secret |
| `DOMARC_RELAY_BIND_HOST` | `127.0.0.1` | bind host |
| `DOMARC_RELAY_BIND_PORT` | `5443` | bind port |
| `DOMARC_RELAY_CUSTOMER_SOURCE` | `yaml` | adapter customer (yaml/sqlite/rest/stormshield) |
| `DOMARC_RELAY_BOOTSTRAP_PASSWORD` | (none) | password admin auto-creato al primo avvio |
| `DOMARC_RELAY_MASTER_KEY_PATH` | `/var/lib/domarc-smtp-relay-admin/master.key` | path master key Fernet |
| `DOMARC_RELAY_MANUAL_PATH` | `/var/lib/domarc-smtp-relay-admin/manual.md` | output manual auto-generato |
| `ANTHROPIC_API_KEY` | (none) | chiave Claude API (gestibile da UI Settings → Chiavi API) |

## Documentazione

- [docs/manual.md](docs/manual.md) — auto-generato (rigenerato all'avvio)
- [docs/guida_funzionamento.md](docs/guida_funzionamento.md) — guida operativa narrativa
- [docs/ai_assistant.md](docs/ai_assistant.md) — modulo IA in dettaglio
- [docs/rule_engine_v2.md](docs/rule_engine_v2.md) — Rule Engine v2 reference
- [docs/operations.md](docs/operations.md) — backup, master.key rotation, troubleshooting
- [CHANGELOG.md](CHANGELOG.md) — storia versioni

## Test

```bash
cd /opt/domarc-smtp-relay-admin
.venv/bin/python -m pytest tests/ -v
# 162 test PASS in ~5s
```

Suite copre: rule engine v2 parity (88), PII redactor, Claude provider mock,
router A/B split, DAO, error aggregator, rule proposer.

## Health check

```bash
# JSON con tutti i check componenti
curl -b cookies.txt http://localhost:5443/health/full | jq

# Pagina HTML
http://localhost:5443/health/system

# Endpoint pubblico no-auth (per loadbalancer)
curl http://localhost:5443/health
```

## Backup

Vedi [docs/operations.md](docs/operations.md) per la procedura completa.
Sintesi:

```bash
# DB + master key (entrambi necessari per restore)
cp /var/lib/domarc-smtp-relay-admin/admin.db backup-$(date +%F).db
cp /var/lib/domarc-smtp-relay-admin/master.key backup-$(date +%F).key
chmod 600 backup-*.key
```

⚠️ **Senza master.key le API keys cifrate diventano illegibili.** Backup combinato è obbligatorio.

## Licenza

Core MIT. Vedi [LICENSE](LICENSE).

---

_Generato per la release v1.0.0 — vedi [CHANGELOG.md](CHANGELOG.md) per le novità._
