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
│   │                          # customer_sync, privacy_bypass, manual, …
│   ├── customer_sources/      # Adapter pluggable: yaml, sqlite, rest,
│   │                          # stormshield, postgres, **local** (default)
│   ├── customer_sync/         # M028: provider pluggabili (postgres, mssql,
│   │                          # csv_file, json_url) + engine + scheduler
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

- **Produzione/operativo**: VM `da-smtp-ia` (192.168.4.25, Ubuntu 24.04).
  Tutti i 3 servizi systemd attivi: `domarc-smtp-relay-admin`,
  `stormshield-smtp-relay-listener`, `stormshield-smtp-relay-scheduler`.
  Cert HTTPS GlobalSign wildcard `*.domarc.it`. **Tutto il flusso mail
  vive qui**: listener SMTP, admin web, scheduler, edit codice, deploy.
  Working directory: `/opt/domarc-smtp-relay-admin/` (admin) +
  `/opt/stormshield-smtp-relay/` (listener+scheduler).
- **Sistema gestionale Domarc** (192.168.4.41): server **separato**, NON
  fa parte del relay. È solo:
  - sorgente PostgreSQL (`solution.clienti` + `stormshield.*`) per il
    sync periodico dei clienti
  - destinazione dei ticket creati dal relay (`POST /api/v1/tickets/`)
  Repo diverso, codebase diverso. Il relay non ha bisogno di SSH al
  4.41 per il proprio workflow.
- **Repo GitHub**: <https://github.com/grandir66/DA-SMTP> (branch `main`,
  tag `v0.9.0-pre-prod`).

### Database

- **Admin SQLite** (`/var/lib/domarc-smtp-relay-admin/admin.db`):
  regole, gruppi, utenti, settings, eventi auditati, API key cifrate,
  **tabella `customers` autoritativa** (M028, ex `customers_pg_cache`)
  alimentata da provider configurabili in `customer_sync_sources`.
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
    ├── Thread continuation check (M036): se la mail ha In-Reply-To
    │   o References che match-ano un message_id di un evento
    │   precedente, popola is_thread_continuation=true e parent_event
    ├── Domain shadow check (M031): se dominio in shadow → tutto
    │   l'evento in shadow_mode → action effettiva = default_delivery
    ├── Rule engine (chain padre→figli, prima match wins)
    │   └── Recipient_group shadow (M030) / rule shadow (M033) cascade
    │   └── Action dispatcher: ignore | flag_only | quarantine
    │                          | auto_reply | forward | redirect
    │                          | create_ticket | ai_classify
    │                          | create_authorized_ticket (H24)
    ├── keep_original_delivery (CC al destinatario reale)
    ├── also_deliver_to (CC ad altri destinatari da regola)
    ├── Aggregations (cluster errori semantici)
    └── Insert events_log (audit + flush al admin)
        ├── M036: thread fields (in_reply_to, references_json,
        │       reply_to_event_uuid, thread_root_uuid, ticket_id
        │       eredito da parent)
        └── M030/M031/M033: shadow_action / shadow_rule_id se shadow

scheduler:
    ├── sync_loop ogni 5min       (pull customers, rules, settings, ...)
    ├── events_flush_loop ogni 60s (push events_log → admin)
    ├── outbound_drain_loop ogni 10s (smarthost delivery)
    ├── dispatch_drain_loop ogni 10s (POST /api/v1/tickets/ → manager)
    └── routes_reload_loop ogni 60s
```

### Customer source pluggable

5 backend:

- `local` (**default post-M028**): legge dalla tabella autoritativa
  `customers` in admin.db, popolata dal `SyncEngine` di
  `customer_sync/` con provider configurabili dalla UI.
- `yaml`: file statico, gestione manuale.
- `sqlite`: tabella in admin.db con UI CRUD (compat con vecchi deploy).
- `rest`: REST API verso CRM proprietario.
- `stormshield`: chiama API del manager Domarc (legacy,
  `/api/v1/relay/customers/active`).
- `postgres`: legge direttamente PG `solution` + `stormshield` con sync
  periodico in `customers`. Mantenuto per compat; sostituito di fatto
  dal provider `postgres` di `customer_sync/` con seed legacy.

### Customer sync agnostico (M028)

Il `customer_sync/` package fornisce provider pluggabili (postgres,
mssql, csv_file, json_url), un engine di sync schedulato (default 24h)
e una UI `/customer-sync/` per configurare sorgenti, mapping field-by-
field e on_missing policy (`flag` / `delete` / `keep`). La tabella
`customers` è autoritativa: cambi di schema del manager esterno si
gestiscono modificando il mapping, non il codice.

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
# Lavoro DIRETTAMENTE sul server operativo 192.168.4.25 (hostname=da-smtp-ia).
# Le edit ai file sono già live sul filesystem — basta restart per applicare.

# 1. Modifica codice in /opt/domarc-smtp-relay-admin/
cd /opt/domarc-smtp-relay-admin
$EDITOR ...

# 2. Test syntax
.venv/bin/python -c 'import py_compile; py_compile.compile("path/to/file.py", doraise=True)' || true

# 3. Commit + push (backup GitHub)
git add -A
git commit -m "feat(...): descrizione concisa"
git push origin main

# 4. Restart servizi (l'edit è già live, serve solo ricaricare il processo)
systemctl restart domarc-smtp-relay-admin
# Per il listener:
# systemctl restart stormshield-smtp-relay-listener stormshield-smtp-relay-scheduler

# 5. Verifica
systemctl is-active domarc-smtp-relay-admin
journalctl -u domarc-smtp-relay-admin -p err --since "30s ago"
curl -sk -o /dev/null -w "HTTP %{http_code}\n" https://localhost/<endpoint>
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
| **Customer Groups** | `/customer-groups` | Raggruppamenti N:N + regole di membership self-contained (M034) |
| **Customer Sync** | `/customer-sync` | Sorgenti dati clienti agnostiche (postgres/mssql/csv/json) — mapping field-by-field, on_missing policy, schedule, storico run (M028) |
| **Recipient Groups** | `/recipient-groups` | Gruppi destinatari + shadow mode per gruppo (M030) |

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

## H24 — flusso autorizzazioni urgenti a pagamento

Feature operativa per autorizzare apertura ticket urgenti a pagamento via
codice nel subject mail. Implementata in 6 fasi (commit `2192501..57a7823`).

### Componenti

| Pezzo | Path | Cosa fa |
|---|---|---|
| Migration 022 | `domarc_relay_admin/migrations/022_h24_authorization_flow.sqlite.sql` | 3 tabelle nuove + ALTER `authorization_codes` + 5 settings seed |
| Extractor codice | `domarc_relay_admin/h24_code_extractor.py` | Regex liberale, prefix prioritari `AUTH-`/`H24-`, fallback multi-trattino o lettere+cifre |
| Storage CRUD | `domarc_relay_admin/storage/sqlite_impl.py` (sezione H24) | `create_h24_code`, `validate_*` (atomici), `list_h24_targets`, `cleanup_expired_oneshot_codes`, ecc. |
| API admin | `domarc_relay_admin/routes/api.py` | `POST /auth-codes`, `POST /auth-codes/validate` (cascade oneshot→permanente), `GET /h24-targets/active`, `POST /maintenance/cleanup-oneshot-codes`, `POST /usage/<id>/ticket` |
| UI admin | `domarc_relay_admin/routes/h24_codes.py` + 4 template | `/h24-dashboard`, `/h24-codes`, `/h24-targets`, `/h24-settings`, `/h24-codes/<id>/usages` |
| Listener action | `services/smtp_listener/relay/actions.py:do_create_authorized_ticket` | Estrae codice, valida via API, costruisce payload ticket URGENZA=PAGAMENTO, manda ack/reject |
| Listener cache | `services/smtp_listener/relay/storage.py:h24_targets_cache` | Sync da admin per multi-brand |
| Loop scheduler | `services/smtp_listener/relay/scheduler.py:_h24_maintenance_loop` (24h cleanup) + `_h24_usage_flush_loop` (5min stub rendicontazione) | |
| Reply templates | DB `reply_templates` (5 H24): `out_of_hours_with_paid_option`, `out_of_hours_no_paid_option`, `h24_ack`, `h24_already_used`, `h24_reject` | Jinja2 con `auth_code`, `h24_inbound_alias`, `urgent_fee`, `ticket_id` |
| Runbook operativo | `docs/H24_RUNBOOK.md` | Setup step-by-step, test e2e, troubleshooting |

### Concetti

- **Codici MONOUSO** (`authorization_codes`): emessi da auto-reply fuori orario,
  TTL ≤ 24h, consume atomico race-safe via `UPDATE … WHERE used_at IS NULL`.
  Format `AUTH-XXXXXX` nel subject (alfabeto leggibile 32 char no I/O/0/1).
- **Codici PERMANENTI** (`customer_h24_codes`): per clienti H24 contrattuale,
  riusabili, audit completo in `customer_h24_codes_usage` (con
  `reported_to_manager_at` predisposto per rendicontazione futura).
- **Multi-brand** (`smtp_relay_h24_targets`): mapping `source_domain → h24_alias`
  per il `mailto:` brand-aware. Cascade: `action_map` > target lookup > setting.
- **Ticket urgente**: `URGENZA=URGENTE`, `SETTORE=S`, payload con note
  arricchite (codice, tipo, importo, mailbox di rientro, evento originario).

### Setup operativo (riferimento rapido)

1. Settings `/h24-settings`: imposta default alias, fee, prefix.
2. Targets `/h24-targets`: aggiungi `datia.it → h24@datia.it`,
   `domarc.it → h24@domarc.it` (o redirezionato durante pilot).
3. Codici permanenti `/h24-codes`: crea per ogni cliente H24 contrattuale.
4. Regole pipeline (3):
   - `auto_reply` su mailbox sorgente con `generate_auth_code=true` +
     `auto_reply_template=out_of_hours_with_paid_option` + `only_outside_service_hours=true`.
   - `create_authorized_ticket` su mailbox di rientro (priorità alta) con
     `match_subject_regex=AUTH-|H24-|...` + `ack_template_id` + `reject_template_id`.
   - `auto_reply` catch-all sulla stessa mailbox di rientro (priorità più bassa)
     con `auto_reply_template=h24_reject`.

Per dettagli, troubleshooting, comandi diagnostici: vedi `docs/H24_RUNBOOK.md`.

---

## Customer sync agnostico (M028)

Tabella `customers` autoritativa, alimentata da provider configurabili.

| Componente | Path | Cosa fa |
|---|---|---|
| Migration 028 | `domarc_relay_admin/migrations/028_customer_sync_sources.sqlite.sql` | RENAME `customers_pg_cache`→`customers`, tabelle `customer_sync_sources` e `customer_sync_runs`, seed sorgente legacy con sentinel `_use_legacy_pgconfig` |
| Provider package | `domarc_relay_admin/customer_sync/` | `base.py` (ABC), `postgres.py`, `mssql.py`, `csv_file.py`, `json_url.py`, `mapper.py`, `engine.py`, `scheduler.py` |
| Backend runtime | `domarc_relay_admin/customer_sources/local_source.py` | Legge da `customers` (default per nuovi deploy) |
| UI admin | `domarc_relay_admin/routes/customer_sync.py` + 4 template | `/customer-sync/` lista, wizard 4-step, test connessione, mapping editor, storico run |

Policy on_missing per ogni sorgente: `flag` (contract_active=0,
default), `delete`, `keep`. Schedule default 24h.

---

## Rule engine — semplificato (M035) + thread tracking (M036)

### Filtro contratto: **solo gruppi cliente**

Dopo M035, il filtro contratto/profilo orario nelle regole avviene
**esclusivamente** via `match_customer_groups`. I rule_sets (M029)
sono solo container UI per organizzare le regole per profilo orario,
NON gating runtime.

I gruppi cliente (M018) sono **self-contained** (M034): regole di
membership configurabili in `/customer-groups/<id>/membership-rules`
auto-assegnano i clienti al gruppo in base ai campi del cliente
(`contract_type`, `tipologia_servizio`, JSON custom). Eliminata
dipendenza dal manager esterno.

### Thread continuation (M036)

Le risposte a mail già tracciate (`In-Reply-To` o `References` che
match-ano un `message_id` in `events_log`) NON aprono un nuovo ticket.
Implementata via:
- ALTER `events_log` con `in_reply_to`, `references_json`,
  `reply_to_event_uuid`, `thread_root_uuid`.
- Tristate `match_is_thread_continuation` su rules.
- Seed regola "Thread continuation — passa al destinatario"
  priority=5 in rule_set `globali` (azione `default_delivery`).
- L'evento risposta eredita `ticket_id` dal parent.

### Shadow mode (M030/M031/M033)

Modalità passive per testare regole/gruppi/domini in produzione senza
eseguire le azioni. Cascade: dominio shadow → tutto in shadow;
recipient_group shadow → solo destinatari del gruppo; rule shadow →
solo quella regola. Audit completo in `events_log.shadow_action` /
`shadow_rule_id` ricostruisce "cosa sarebbe successo".

---

## Form regole UX v3 (2026-05-06): Toggle Modalità Base/Avanzata

I 3 form regola (orfana / gruppo padre / figlio) hanno **toggle
globale Base/Avanzata** in cima, persistito in `localStorage`.
Filosofia: in Base solo i campi essenziali per il caso d'uso normale
(gestione per gruppo cliente); il singolo cliente è eccezione.

### Stack UI

| File | Cosa |
|---|---|
| `static/css/rule_form.css` | Classi condivise: `.rf-section` numerate, `.rf-base-only`, `.rf-advanced-only`, `.rf-mode-toggle-bar`, `.rf-priority-presets`, `.rf-mini-sim`, `.rf-impact-preview` |
| `static/js/rule_form_modes.js` | `rfSetMode` (persiste localStorage), `rfSetPriority`, `rfLiveValidate` (regex client-side), `rfSimulateInline`, `rfPreviewImpact` (POST `/rules/preview-impact`) |
| `templates/admin/rule_form.html` (orfana) | 5 sezioni + mini-sim + impact in fondo |
| `templates/admin/rule_group_form.html` | 5 sezioni, no mini-sim/impact (gruppo non agisce) |
| `templates/admin/rule_child_form.html` | 5 sezioni + mini-sim + impact, banner "ereditato dal padre" in cima |
| `static/mockups/rule_form_v2.html` | Mockup statico di riferimento (raggiungibile in browser) |

### Backend endpoint nuovo

- `POST /rules/preview-impact` (`routes/rules.py:preview_impact`) —
  riceve match_* del form, scansiona events_log ultimi 7gg (cap 2000),
  ritorna conteggio + sample + top domini. Valuta in Python:
  from_domain (split @), regex compilate, in_service tristate,
  customer_groups tramite `list_group_members`.

### Validazione regola — V001-V008 finalmente wired

`validate_rule()` di `rules/validators.py` era definito ma mai
chiamato. Ora wired nei 3 route handler via helper
`_run_full_validators()` in `routes/rules.py`. Errori bloccano save
con flash; warnings (W004, W_PRI_GAP) flushed come info.

`upsert_rule` aggiunge:
- check "almeno un match_*" esteso a M018/M027/M035/M036
  (`match_customer_groups`, `match_to_group_id`, tristate, ecc.)
- `re.compile()` su tutti i regex prima del salvataggio
- mutex `match_to_regex`/`match_to_group_id` e `forward_to_emails`/
  `forward_to_group_id`
- range priority 1..999_999 (V007 hard server-side)

`validators.MATCH_FIELDS_*` esteso e `_matches_compatible` gestisce
ora `match_to_group_id` (uguaglianza) e `match_customer_groups`
(intersezione CSV non vuota).

---

*Ultimo aggiornamento: 2026-05-06 — Form regole UX v3 (toggle Base/Avanzata + validazione live + anteprima impatto + mini-simulatore + 5 sezioni semantiche identiche).*
