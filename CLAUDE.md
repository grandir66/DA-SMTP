# Istruzioni per Claude Code — Domarc SMTP Relay

Documento di riferimento per **sviluppo e manutenzione del prodotto SMTP relay
standalone Domarc**. Progetto **completamente svincolato** dal manager
gestionale (sistema diverso, repo diverso, server di runtime diverso).

---

## Contesto progetto

### Domarc SMTP Relay — prodotto standalone

Sistema SMTP relay con rule engine deterministico + AI assistant (Claude API)
per il triage automatico delle mail in ingresso. Standalone: girando su VM
Ubuntu/Debian dedicata, **non dipende** dal sistema gestionale Domarc se non
per la sincronizzazione periodica dell'anagrafica clienti (PG `solution` +
`stormshield`) e l'invio dei ticket via API.

### Componenti

```
Domarc SMTP Relay
├── domarc_relay_admin/        # Admin web (Flask, gunicorn :5443 dietro nginx)
│   ├── app.py                 # Factory create_app
│   ├── config.py              # AppConfig + load_config (env + secrets.env)
│   ├── auth/                  # Login, sessione, role-based access (4 ruoli)
│   ├── routes/                # Blueprint Flask: dashboard, rules, events,
│   │                          # queue, ai, integrations, customer_groups,
│   │                          # privacy_bypass, manual, …
│   ├── customer_sources/      # Adapter pluggable: yaml, sqlite, rest,
│   │                          # stormshield, postgres (con sync periodico)
│   ├── storage/               # SqliteStorage (admin.db, schema migrations)
│   ├── migrations/            # SQL idempotenti, applicate a init_db
│   ├── ai_assistant/          # PII redactor + provider Anthropic/DGX
│   ├── secrets_manager.py     # Fernet wrap per API key cifrate
│   └── tenants/               # Multi-tenant (default tenant_id=1)
│
├── relay/ (path runtime listener: /opt/stormshield-smtp-relay/)
│   ├── listener.py            # SMTP server aiosmtpd (porta 25)
│   ├── pipeline.py            # process_message: privacy bypass → kill switch
│   │                          # → rule engine → action dispatcher
│   ├── rules.py               # RuleEngine v2 con gerarchia padre/figlio
│   ├── actions.py             # do_ignore/forward/redirect/auto_reply/
│   │                          # quarantine/create_ticket/ai_classify
│   ├── auto_reply.py          # Costruzione mail risposta da template DB
│   ├── scheduler.py           # 5 loop async: sync, flush events, drain
│   │                          # outbound, drain dispatch ticket
│   ├── sync.py                # Pull verso admin: customers, rules, settings,
│   │                          # privacy bypass, customer-groups
│   ├── manager_client.py      # HTTP client verso /api/v1/relay/* admin
│   └── storage.py             # SQLite relay.db (cache + queue)
│
├── installer/                 # Bootstrap VM Ubuntu/Debian
│   ├── install.sh             # 5 step idempotenti (state file)
│   ├── lib/01..05-*.sh        # OS prep, users+systemd, nginx+HTTPS, deploy, ufw
│   ├── wizard/wizard.py       # Wizard CLI 4-step post-install
│   └── backup-restore/        # Bundle cifrato Fernet + restore
│
├── templates/admin/           # Jinja2 (no UI Kit esterno, stile inline + admin.css)
└── static/                    # CSS/JS dedicati
```

### Server di runtime

- **Produzione/operativo**: VM `da-smtp-ia` (192.168.4.25, Ubuntu 24.04). Tutti
  i 3 servizi systemd attivi: `domarc-smtp-relay-admin`,
  `stormshield-smtp-relay-listener`, `stormshield-smtp-relay-scheduler`.
  Cert HTTPS GlobalSign wildcard `*.domarc.it`.
- **Sviluppo/workspace**: questo path su 192.168.4.41 è **solo working
  directory** (clone git per editing). **Nessun servizio gira qui**. Le
  modifiche vanno: edit → `git commit` → `git push` →
  `ssh root@192.168.4.25 "cd /opt/domarc-smtp-relay-admin && git pull && systemctl restart …"`.
- **Repo GitHub**: <https://github.com/grandir66/DA-SMTP> (branch `main`,
  tag `v0.9.0-pre-prod`).

### Database

- **Admin SQLite** (`/var/lib/domarc-smtp-relay-admin/admin.db`):
  regole, gruppi, utenti, settings, eventi auditati, API key cifrate,
  cache clienti syncata da PG (tabella `customers_pg_cache`).
- **Listener SQLite** (`/var/lib/stormshield-smtp-relay/relay.db`):
  cache clienti/rules/settings/templates, outbound queue, dispatch queue,
  quarantine, events_log.
- **Sorgenti esterne** (sync periodico):
  - PostgreSQL `solution` (clienti — tabella `clienti`, `aconto`=codcli)
  - PostgreSQL `stormshield` (`customer_settings`, `customer_aliases`,
    `client_domains`, `customer_availability_types`,
    `smtp_relay_service_hours`, `customer_service_holidays`)

---

## Regole critiche

### Branding

- Il **prodotto** si chiama **Domarc SMTP Relay**, NON "Stormshield Relay"
  (Stormshield è il vendor del firewall, niente a che fare con la mail).
- Path legacy `/opt/stormshield-smtp-relay/` e i 2 servizi
  `stormshield-smtp-relay-listener.service` / `-scheduler.service` esistono
  per ragioni storiche; possono essere rinominati in futuro.
- L'admin è `domarc-smtp-relay-admin.service` con utente `domarc-relay`.

### Test email — mai mittenti generici

Per QUALUNQUE test SMTP/IMAP usare **SOLO**:
- `r.grandi@domarc.it` (su PG → casella reale)
- `r.grandi@datia.it` (su MS365 → casella reale)

NON USARE MAI come mittente test:
- `info@*`, `monitoring@*`, `noreply@*`, `*@example.com`,
  `*@example.org`, indirizzi inventati `@domarc.it` come `test@`,
  `monitoraggio@`, ecc.

Le mail di test inutili a destinatari sbagliati intasano caselle reali +
flooding il pilota datia.it. Ogni invio test deve essere giustificato.

### Rollout flusso mail

- **Pilota attuale**: solo `datia.it` instradato dal sistema antispam
  ESVA (`192.168.4.x`) verso il relay 192.168.4.25.
- **Cutover futuro**: `domarc.it` (interno) e altri domini gestiti
  passeranno dal relay solo dopo validazione del pilota completa.
- Per i test con regole, **evitare CC verso** `@domarc.it` (es.
  `also_deliver_to`) finché il dominio non transita ufficialmente.

### Privacy bypass

- Migration 011 ha introdotto la lista privacy-bypass (indirizzi/domini
  esclusi da rule engine, aggregations, IA, body persistence).
- **Pre-cutover**: prima di mettere un dominio nel relay, popolare la
  privacy bypass list con i ruoli sensibili (DPO, legale, HR, sindacale,
  etc.). Wizard disponibile: `/privacy-bypass/suggest-sensitive`.

### Kill switch

- Setting `relay_passthrough_only` (UI: dashboard banner / pulsante).
- Quando ATTIVO il listener bypassa rule engine + IA + aggregations e
  fa solo `default_delivery` via smarthost del dominio. Da usare in
  emergenza al cutover come rollback rapido (1 click).

### Architettura modulare

Ogni modulo del relay è separato da quelli del manager gestionale.
Il SMTP relay non importa codice del manager. Le integrazioni sono
solo via API HTTPS (per i ticket) e PostgreSQL read-only (per i clienti).

---

## Architettura

### Flusso mail end-to-end

```
mittente → ESVA antispam → 192.168.4.25:25 (listener)
    ├── Privacy bypass check (drop o passthrough silenzioso)
    ├── Kill switch check (se ON → default_delivery, no rules)
    ├── Rule engine (chain padre→figli, prima match wins)
    │   └── Action dispatcher: ignore | flag_only | quarantine
    │                          | auto_reply | forward | redirect
    │                          | create_ticket | ai_classify
    ├── keep_original_delivery (CC al destinatario reale)
    ├── also_deliver_to (CC ad altri destinatari da regola)
    ├── Aggregations (cluster errori semantici)
    └── Insert events_log (audit + flush al admin)

scheduler:
    ├── sync_loop ogni 5min       (pull customers, rules, settings, ...)
    ├── events_flush_loop ogni 60s (push events_log → admin)
    ├── outbound_drain_loop ogni 10s (smarthost delivery)
    ├── dispatch_drain_loop ogni 10s (POST /api/v1/tickets/ → manager)
    └── routes_reload_loop ogni 60s
```

### Customer source pluggable

4 backend:

- `yaml`: file statico, gestione manuale.
- `sqlite`: tabella in admin.db con UI CRUD.
- `rest`: REST API verso CRM proprietario.
- `stormshield`: chiama API del manager Domarc (legacy,
  `/api/v1/relay/customers/active`).
- `postgres`: legge direttamente PG `solution` + `stormshield`,
  sync periodico → `customers_pg_cache` in admin.db.
  **Backend di default per il nuovo deploy**.

### Ticket sink (futuro)

Oggi solo `manager_client.submit_ticket()` → `POST /api/v1/tickets/` del
manager Domarc. Roadmap: TicketSink pluggabile (Jira, Zendesk, generic
REST con Jinja2 body, email-to-ticket, webhook).

### Multi-tenant

`tenant_id` su quasi tutte le tabelle (default 1). Ruolo `superadmin`
può navigare tra tenant via dropdown header. Per ora 1 solo tenant attivo.

---

## Workflow di sviluppo

```bash
# 1. Modifica codice qui (su 4.41, in /opt/domarc-smtp-relay-admin/)
cd /opt/domarc-smtp-relay-admin
$EDITOR ...

# 2. Test syntax + commit
.venv/bin/python -c 'import py_compile; py_compile.compile("path/to/file.py", doraise=True)' || true
git add -A
git commit -m "feat(...): descrizione concisa"

# 3. Push
git push origin main

# 4. Deploy su VM 4.25
ssh root@192.168.4.25 "cd /opt/domarc-smtp-relay-admin && git pull --quiet"

# 5. Restart servizi (nell'ordine)
ssh root@192.168.4.25 "
  systemctl restart domarc-smtp-relay-admin
  systemctl restart stormshield-smtp-relay-listener
  systemctl restart stormshield-smtp-relay-scheduler
"

# 6. Verifica
ssh root@192.168.4.25 "systemctl is-active domarc-smtp-relay-admin stormshield-smtp-relay-listener stormshield-smtp-relay-scheduler"
ssh root@192.168.4.25 "journalctl -u stormshield-smtp-relay-listener -n 30 --no-pager"
```

### Migrations DB

Ogni file `migrations/NNN_*.sqlite.sql` è applicato **una volta** al
primo `init_db=True` di `create_app()` (che chiama `apply_migrations`).
La tabella `_migrations` traccia le versioni applicate. Le migration
sono idempotenti (`CREATE TABLE IF NOT EXISTS`, `ALTER TABLE` con
try/except in mini-migration).

### CSRF

Tutti i form POST hanno `<input type="hidden" name="csrf_token"
value="{{ csrf_token() }}">`. Il blueprint `api_bp` (endpoint
`/api/v1/relay/*` per il listener) è esente via `csrf.exempt(api_bp)`
in `app.py` — usa X-API-Key invece.

### Sync interval

`sync_interval_sec=300` (5 min) di default. Per applicare immediatamente
una modifica al manager (regole, settings) sul listener:
`systemctl restart stormshield-smtp-relay-scheduler` (forza un sync
immediato). Per le settings dell'admin invece bastano i route Flask che
le leggono al volo dal DB.

### Test pytest

`.venv/bin/pytest` esegue 187 test (162 + 25 nuovi nelle ultime sessioni).
Coperture: rule engine v2, validators, PII redactor, AI router, queue,
manual generator, module manager, activity helpers.

---

## Settings runtime — pannelli UI

| Pannello | Path | Cosa configura |
|---|---|---|
| **Integrazioni** | `/integrations` | DB clienti (PG host/user/sslmode/sync), Ticket API (base_url/key/path), AI key Anthropic |
| **Settings** | `/settings` | Variabili runtime generiche (kill switch, body retention, ecc.) |
| **Connessione** | `/connection` | Smarthost default, relay_api_key |
| **AI Provider** | `/ai/providers` | CRUD provider Claude/DGX, test connettività |
| **AI Models** | `/ai/models` | Routing job_code → provider+model, A/B traffic split |
| **Privacy Bypass** | `/privacy-bypass` | Lista indirizzi/domini esclusi (admin only) |
| **Customer Groups** | `/customer-groups` | Raggruppamenti N:N per match nelle regole |

---

## Errori comuni da evitare

### Database

- **MAI** scrivere direttamente sui PG di produzione (`solution`,
  `stormshield`) dal relay. Il customer source è **read-only**.
- Usare sempre placeholder SQL (`?` o `%s`), mai f-string per i
  parametri (SQL injection).
- Lock transaction esplicito (`BEGIN IMMEDIATE`) per operazioni di
  sync che fanno DELETE+INSERT massivi.

### Configurazione

- Non hardcodare credenziali. Ogni secret va in
  `/etc/domarc-smtp-relay-admin/secrets.env` (env vars) o cifrato in
  `api_keys` (Fernet con `master.key` in `/var/lib/`).
- **MAI committare** `secrets.env`, `master.key`, `*.db` o `.venv/`.
  Verificare `.gitignore` prima di `git add -A`.

### Regole

- Tutte le route admin hanno `@login_required(role="...")`.
- Endpoint `/api/v1/relay/*` (listener) hanno `@require_api_key`.
- Validare permessi in backend, non solo nel template.

### CSRF protection

- Aggiungere `{{ csrf_token() }}` come hidden field in OGNI form POST
  HTML. Il decorator CSRFProtect è globale.
- Per endpoint API REST con auth diversa, esentare il blueprint con
  `csrf.exempt(blueprint)`.

---

## Sistema protezione codice

**Zona ROSSA** (modificare con estrema cautela):
- `domarc_relay_admin/app.py` factory create_app
- `domarc_relay_admin/config.py` AppConfig
- `domarc_relay_admin/storage/sqlite_impl.py` migrazioni / DAO core
- `relay/pipeline.py` flusso pipeline mail (process_message)
- `relay/rules.py` rule engine v2

**Zona GIALLA** (modificabili con test):
- `relay/actions.py`, `relay/auto_reply.py`
- `routes/*.py` blueprint
- `customer_sources/*.py`

**Zona VERDE** (modificabili liberamente):
- Templates `.html`
- CSS `static/`
- Nuovi moduli o blueprint isolati
- Documentazione

### Pattern Anti-Loop Debug

Se un cambiamento ROMPE qualcosa di esistente: **NON** modificare il
codice esistente per fixare; aggiungere un wrapper / feature flag /
nuovo modulo separato. Stop change-ripple.

---

## CHANGELOG

`CHANGELOG.md` aggiornato a ogni modifica rilevante (categorie:
Aggiunte / Modifiche / Ottimizzazioni / Correzioni). Formato Keep a
Changelog. Date ISO `YYYY-MM-DD`. Lingua italiana.

Voci che includono il riferimento file (es. `routes/integrations.py`)
quando utile per traceback storico.

---

## Backup & restore

```bash
# Backup completo (cifrato Fernet)
sudo /opt/domarc-smtp-relay-admin/installer/backup-restore/domarc-backup.py \
    --output /var/lib/domarc-smtp-relay-backups/snapshot-$(date +%F).tar.gz.enc \
    --interactive

# Restore
sudo /opt/domarc-smtp-relay-admin/installer/backup-restore/domarc-restore.py \
    --input /path/to/snapshot.tar.gz.enc
```

Bundle include: admin.db (con o senza body emails), relay.db, master.key,
secrets.env, *.yaml, nginx config, ssl certs self-signed, systemd units,
manifest.json con sha256.

---

## Repository di partenza per Claude Code

Quando apri Claude Code in questo path, hai:
- Repo git pulito (clone GitHub, branch main).
- Memoria progetto separata in
  `/root/.claude/projects/-opt-domarc-smtp-relay-admin/memory/`.
- Direttive di questo `CLAUDE.md`.

Per modifiche al codice: edit qui → commit → push → pull su 4.25
(workflow descritto sopra).

Per il manager gestionale (sistema diverso, repo diverso) c'è un altro
project Claude Code: lavorare lì in `/opt/domarc/stormshield-manager/web_interface/`
con il suo CLAUDE.md.

---

*Ultimo aggiornamento: 2026-04-30 — separazione del progetto SMTP relay
dal manager gestionale.*
