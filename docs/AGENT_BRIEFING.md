# Domarc SMTP Relay — Agent Briefing

> **Scopo del documento**: dare a una nuova istanza Claude (o a uno sviluppatore umano che subentra) tutto il contesto per **operare immediatamente** sul progetto senza dover analizzare il codebase da zero.
>
> **Versione**: aggiornato a 2026-05-05, dopo i refactor UI v0.9.x (commit `6c42553`).
>
> **Setup di lavoro**: si lavora direttamente sul server operativo `192.168.4.25` (hostname `da-smtp-ia`). Il manager Domarc su 192.168.4.41 è un sistema **separato** (PostgreSQL sorgente clienti + ricezione ticket); non c'entra con il workflow di edit/deploy del relay.

---

## 1. Cos'è il sistema

Domarc SMTP Relay è un **relay SMTP intelligente con UI di amministrazione** per gestire, classificare e instradare email aziendali in base a regole deterministiche e AI.

Tre processi cooperano sulla stessa VM:

| Servizio | Path | Porta | Cosa fa |
|---|---|---|---|
| `domarc-smtp-relay-admin.service` | `/opt/domarc-smtp-relay-admin/` | 5443 (Flask) → 443 (nginx) | Web UI + DAO + endpoint API per il listener |
| `stormshield-smtp-relay-listener.service` | `/opt/stormshield-smtp-relay/` | 25 (aiosmtpd) | Riceve mail, applica regole, esegue azioni |
| `stormshield-smtp-relay-scheduler.service` | `/opt/stormshield-smtp-relay/` | n/a | Sync periodico admin → cache locale, drain code outbound, cleanup nightly |

**Importante**: il listener **non legge il DB SQLite dell'admin**. Ha la sua DB locale `/var/lib/stormshield-smtp-relay/relay.db` con cache che lo scheduler mantiene aggiornata via API HTTP.

```
                  ┌──────────────────────┐
   Mail SMTP ───→ │ Listener (aiosmtpd)  │
   (porta 25)     │  + Rule engine v2    │
                  │  + Cache locale      │
                  └──┬───────────────────┘
                     │ HTTP API (X-API-Key)
                     ▼
                  ┌──────────────────────┐         ┌─────────────────┐
                  │ Admin Flask          │ ──────→ │ Postgres        │
                  │  + SQLite (admin.db) │         │ solution (4.41) │
                  │  + endpoints /api/v1 │         │ stormshield     │
                  └──────────────────────┘         └─────────────────┘
                     ▲
                     │ Browser (operatori)
                  ┌──┴───────────┐
                  │ UI web       │
                  └──────────────┘
```

### 1.1 Repository git

- **Remote**: `git@github.com:grandir66/DA-SMTP.git`, branch `main`
- **Path lavoro**: `/opt/domarc-smtp-relay-admin/` (clone repo, deploy in-place)
- **Listener mirror**: `services/smtp_listener/relay/` nel repo è il mirror di `/opt/stormshield-smtp-relay/relay/`. Quando modifichi il listener, deploya in `/opt/stormshield-smtp-relay/relay/` e poi sincronizza il mirror prima del commit.

### 1.2 Versione & file di stato

```bash
# Schema version DB admin
sqlite3 /var/lib/domarc-smtp-relay-admin/admin.db "SELECT MAX(version) FROM _migrations;"
# Versione applicativo
grep "^__version__" /opt/domarc-smtp-relay-admin/domarc_relay_admin/__init__.py
# Branch e ultimi commit
cd /opt/domarc-smtp-relay-admin && git log --oneline -10
```

---

## 2. Come operare (workflow tipico)

### 2.1 Modifica codice

```bash
# 1. Pull file dal server in locale per analisi
scp root@192.168.4.25:/opt/domarc-smtp-relay-admin/<path>/<file> /tmp/_<file>

# 2. Edita /tmp/_<file> con Edit tool

# 3. Re-deploy
scp /tmp/_<file> root@192.168.4.25:/opt/domarc-smtp-relay-admin/<path>/<file>

# 4. Restart il servizio impattato
ssh root@192.168.4.25 'systemctl restart domarc-smtp-relay-admin'
# OR per listener:
ssh root@192.168.4.25 'systemctl restart stormshield-smtp-relay-listener stormshield-smtp-relay-scheduler'

# 5. Smoke test
ssh root@192.168.4.25 'curl -sk -o /dev/null -w "%{http_code}\n" -L https://localhost/<endpoint>'

# 6. Logs se errori
ssh root@192.168.4.25 'journalctl -u domarc-smtp-relay-admin --since "1 minute ago" | grep -iE "error|traceback" | head'
```

### 2.2 Modifica listener — sync mirror in repo

```bash
# Dopo aver editato /opt/stormshield-smtp-relay/relay/<file>.py
ssh root@192.168.4.25 'cd /opt/domarc-smtp-relay-admin && \
   for f in actions.py pipeline.py rules.py storage.py sync.py manager_client.py parser.py; do
     cp /opt/stormshield-smtp-relay/relay/$f services/smtp_listener/relay/$f
   done && git add services/smtp_listener/relay/'
```

### 2.3 Migrazioni DB

Le migrations sono in `domarc_relay_admin/migrations/NNN_<nome>.sqlite.sql`. Numerazione progressiva. Il runner gira automaticamente ad ogni init storage (idempotente, traccia `_migrations.version`).

```bash
# Crea nuova migration (prossimo numero disponibile)
ls /opt/domarc-smtp-relay-admin/domarc_relay_admin/migrations/ | tail -3
# → es. 027_xxx.sqlite.sql, prossimo è 028

# Applica manualmente (se runner non scatta)
sqlite3 /var/lib/domarc-smtp-relay-admin/admin.db < migrations/028_<nome>.sqlite.sql
sqlite3 /var/lib/domarc-smtp-relay-admin/admin.db "INSERT OR IGNORE INTO _migrations (version) VALUES (28);"
```

### 2.4 Test in browser via Playwright (headless)

```python
# Già installato + chromium in /root/.cache/ms-playwright/
# Password admin: domarc2026 (DOMARC_RELAY_BOOTSTRAP_PASSWORD in /etc/domarc-smtp-relay-admin/secrets.env)

ssh root@192.168.4.25 'cd /opt/domarc-smtp-relay-admin && .venv/bin/python << EOF
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    ctx = b.new_context(ignore_https_errors=True, viewport={"width":1440,"height":900})
    page = ctx.new_page()
    page.on("pageerror", lambda e: print(f"[err] {e}"))
    page.goto("https://localhost/login", wait_until="networkidle")
    page.fill("input[name=username]", "admin"); page.fill("input[name=password]", "domarc2026")
    page.click("button[type=submit]"); page.wait_for_load_state("networkidle")
    # ... test custom
    b.close()
EOF'
```

### 2.5 Commit + push

```bash
ssh root@192.168.4.25 'cd /opt/domarc-smtp-relay-admin && git add -A && git status --short'

# Per commit message lunghi: scrivi /tmp/_msg.txt locale, scp su VM, poi:
ssh root@192.168.4.25 'cd /opt/domarc-smtp-relay-admin && git commit -F /tmp/_msg.txt && git push origin main'
```

**Pattern commit message** (formato già usato consistentemente):
```
<type>(<scope>): titolo breve

Descrizione dettagliata multi-paragrafo.

Sezione 1 — ...
Sezione 2 — ...

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
```

---

## 3. Vita di un'email — flusso end-to-end

```
1. Mail SMTP arriva al listener (aiosmtpd, porta 25)

2. Privacy bypass check (Migration 011)
   - Se from_email/to_email/from_domain/to_domain è in privacy_bypass_cache:
     → mail SCARTATA, no log, no DB. (GDPR-sensitive)

3. Parser (parser.py)
   - decode_header() RFC 2047 sul Subject UNA SOLA VOLTA in entrata
     (fix bug 2026-05-04: evita "AUTH-LD8URD – =?UTF-8?B?...?=" doppio encoding)
   - Estrae attachments + body text/html (cap 64KB/256KB)
   - Normalizza from/to addresses + domains

4. Customer resolve (pipeline.py:_event_dict)
   - Lookup customers_pg_cache per from_address e per primary_to via aliases
   - Risolve codcli + contract_active + availability_type (profilo orario)
   - has_exception_today = se cliente ha override schedule oggi
   - customer_groups = lista codici dei gruppi a cui il cliente appartiene
   - recipient_groups = mapping {email: [group_id]} dai destinatari noti

5. Rule engine v2 (rules.py:RuleEngine.evaluate)
   - Ordinamento: scope_order (global → mailbox → sector_pack), priority
   - Per ogni regola in ordine:
     a. scope_matches: la regola si applica a questo scope?
     b. service_constraint_skip: se la regola ha vincolo orario ma cliente non
        identificato → skip (stesso giorno tonto)
     c. _rule_matches: AND di tutti i criteri:
        - match_from_regex (su event.from_address)
        - match_to_regex (su event.to_address)
        - match_subject_regex / match_body_regex
        - match_from_domain / match_to_domain (lowercase exact)
        - match_at_hours (override del profilo cliente)
        - match_in_service (in/fuori servizio rispetto al profilo)
        - match_known_customer (mittente è codcli risolto?)
        - match_contract_active (contratto attivo?)
        - match_has_exception_today
        - match_customer_groups (CSV "top,sanita")
        - match_to_group_id (Migration 027 — alternativa a match_to_regex)
        - match_tag (header X-Tag)
   - PRIMA regola che matcha vince. Se continue_after_match=True, continua
     a valutare le successive (azioni multiple).

6. Action dispatch (pipeline.py:_dispatch_action)
   Action codes possibili:
   - ignore       → scarta (con log)
   - flag_only    → solo evento, nessuna azione SMTP
   - default_delivery → forward al destinatario originale via smarthost
   - forward      → forward custom (target/port/tls da action_map)
                    + forward_to_emails / forward_to_group_id (override rcpt)
   - redirect     → riscrivi destinatario (action_map.redirect_to)
   - quarantine   → mail in coda quarantine per revisione manuale
   - auto_reply   → invia risposta da template Jinja2
                    (con o senza generate_auth_code)
   - create_ticket → POST al gestionale per aprire ticket diretto
   - create_authorized_ticket → valida codice da subject (cascade
                    oneshot atomico → permanente), apre ticket H24
   - ai_classify / ai_critical_check → invoca IA via /api/v1/relay/ai/classify

7. Override automatici (actions.py)
   - Se customer.contract_active=False E mittente identificato:
     → forza template = always_billable_no_contract (id 12)
     → indipendente da quello configurato sulla regola
   - Se H24 false positive (codice estratto da regex larga ma not_found in DB):
     → re-evaluate rule engine ESCLUDENDO regola corrente (Fix B 2026-05-05)
     → permette alle regole successive di gestire normalmente

8. Aggregazioni in parallelo (aggregations.py)
   - error_aggregations (statiche, regex): fingerprint → counter →
     ticket aggregato a soglia
   - ai_error_clusters (semantici, embedding): pattern simili clusterizzati,
     ticket aggregato a manual_threshold

9. Persist event (events table)
   - relay_event_uuid + from/to/subject/codcli/action_taken/rule_id/ticket_id
   - body_text + body_html con TTL configurabile per GDPR
   - payload_metadata JSON con dettagli audit (chain di valutazione, h24_*, ai_*)

10. Auto-discovery (api.py:_upsert_address_from_event + _autodiscover_recipients)
    - Upsert addresses_from / addresses_to (seen_count, last_seen)
    - Upsert recipients (autodiscovery destinatari per gruppi)

11. Sync verso scheduler/manager (scheduler.py)
    - Eventi flush ogni N secondi via /api/v1/relay/events POST
    - Dispatch outbound_queue → SMTP delivery con retry/backoff
    - Cleanup nightly codici monouso scaduti
```

---

## 4. Schema database — tabelle essenziali

### 4.1 Core (Migration 001-010)

| Tabella | Scopo | Note |
|---|---|---|
| `tenants` | Multi-tenant (default tenant_id=1) | Tutto è scopato a tenant |
| `users` + `user_tenant_roles` | Auth + ruoli (admin/operator/viewer/readonly) | Auth via session, hashing bcrypt |
| `rules` | Le regole del rule engine v2 | Vedi §4.5 |
| `reply_templates` | Template Jinja2 di risposta | id, name, subject_tmpl, body_html_tmpl, body_text_tmpl |
| `error_aggregations` + `error_occurrences` | Aggregazioni statiche (regex) | Legacy fallback per AI clusters |
| `events` | Log completo di ogni mail processata | TTL body via setting |
| `addresses_from` + `addresses_to` | Anagrafica indirizzi visti (autodiscovery) | seen_count, codcli mappato |
| `customers` | **Tabella autoritativa clienti** (M028, ex `customers_pg_cache`) | Popolata dal `customer_sync/` engine; vedi §4.8 |
| `customer_sync_sources` (M028) | Sorgenti configurate (postgres/mssql/csv/json) | Mapping field-by-field, on_missing policy |
| `customer_sync_runs` (M028) | Storico run (status, conteggi, error) | |
| `service_hours` + `service_hours_profiles` | Profili orari (STD/EXT/H24/NO + custom) + override per cliente | |
| `routes` + `domain_routing` | Smarthost SMTP per routing forward/redirect | |
| `settings` | Chiavi-valore globali (relay_api_key, ai_shadow_mode, ecc.) | |

### 4.2 Customer groups (Migration 018 + 034 self-contained)

- `customer_groups`: gruppi logici di clienti (es. "top_customer", "settore_sanita")
- `customer_group_members`: mapping codcli → group_id (N:N)
- **M034** `customer_group_membership_rules`: regole di auto-assegnamento
  in base ai campi del cliente (`contract_type`, `tipologia_servizio`,
  JSON custom). Re-evaluation periodica (5min) o on-customer-update via
  `recompute_group_memberships(group_id)`.

Usati come criterio nelle regole via `match_customer_groups` (CSV) —
**dopo M035 sono l'unica chiave di filtro per contratto/tipo servizio**
(rule_sets non sono più gating runtime).

### 4.3 Privacy bypass (Migration 011)

- `privacy_bypass_domains`: from_email/to_email/from_domain/to_domain esclusi totalmente dal rule engine
- `privacy_bypass_audit`: log delle modifiche

### 4.4 AI (Migration 012-017)

- `ai_providers`: claude_api, local_http (DGX). Endpoint + credentials.
- `ai_jobs`: catalogo immutabile job_code (classify_email, summarize, critical_classify, error_embedding, rule_proposal, ...)
- `ai_job_bindings`: routing per job (provider+modello+prompt+temperature). Versionato, traffic split A/B.
- `ai_decisions`: log immutabile di ogni inferenza (event_uuid, job_code, classification, urgenza, summary, cost_usd, latency, applied/shadow)
- `ai_error_clusters`: dedup semantica errori (embedding paraphrase-multilingual-MiniLM-L12-v2 384dim)
- `ai_rule_proposals`: regole statiche proposte dal learning loop (≥20 decisioni coerenti → proposta)
- `ai_pii_dictionary`: dizionario custom per PII redactor (regex IT + spaCy `it_core_news_sm`)
- `ai_shadow_audit`: audit toggle shadow_mode

### 4.5 Rules — schema esteso

```sql
CREATE TABLE rules (
    id INTEGER PRIMARY KEY,
    tenant_id INTEGER NOT NULL DEFAULT 1,
    name TEXT NOT NULL,
    description TEXT,                          -- M023: note dettagliate
    scope_type TEXT,                           -- global | mailbox | sector_pack
    scope_ref TEXT,
    priority INTEGER NOT NULL DEFAULT 100,     -- più basso = prima
    enabled INTEGER NOT NULL DEFAULT 1,

    -- Match criteria (AND tra tutti i campi compilati)
    match_from_regex TEXT,
    match_from_domain TEXT,                    -- M008: lookup veloce
    match_to_regex TEXT,
    match_to_domain TEXT,
    match_subject_regex TEXT,
    match_body_regex TEXT,
    match_at_hours TEXT,                       -- override profilo cliente
    match_in_service INTEGER,                  -- tristate: NULL/0/1
    match_contract_active INTEGER,             -- M020 tristate
    match_known_customer INTEGER,              -- tristate
    match_has_exception_today INTEGER,         -- M009 tristate
    match_customer_groups TEXT,                -- M018 CSV "top,sanita" — UNICA chiave filtro contratto/tipo (M035)
    match_is_thread_continuation INTEGER,      -- M036 tristate: NULL/0/1
    match_tag TEXT,
    match_to_group_id INTEGER,                 -- M027 FK recipient_groups
    rule_set_id INTEGER,                       -- M029 organizzazione UI per profilo orario (NON gating runtime, post-M035)
    shadow_mode INTEGER NOT NULL DEFAULT 0,    -- M033 shadow per regola singola
    shadow_note TEXT,

    -- Action
    action TEXT NOT NULL,
    action_map TEXT,                           -- JSON action-specific params
    severity TEXT,
    continue_after_match INTEGER NOT NULL DEFAULT 0,

    -- Rule Engine v2 — gerarchia (M010)
    parent_id INTEGER REFERENCES rules(id) ON DELETE CASCADE,
    is_group INTEGER NOT NULL DEFAULT 0,
    group_label TEXT,
    exclusive_match INTEGER NOT NULL DEFAULT 1,
    continue_in_group INTEGER NOT NULL DEFAULT 0,
    exit_group_continue INTEGER NOT NULL DEFAULT 0,

    -- Forward extension (M027)
    forward_to_emails TEXT,                    -- lista ; o , o whitespace
    forward_to_group_id INTEGER,               -- FK recipient_groups

    UNIQUE (tenant_id, scope_type, scope_ref, priority)
);
```

**Vincolo chiave**: `match_to_regex` e `match_to_group_id` sono **alternative esclusive** (validato dal route layer + UI grigia uno se l'altro è valorizzato).

### 4.6 H24 authorization (Migration 022 + 026)

- `authorization_codes` (codici monouso/oneshot):
    - code, codcli, rule_id, valid_until
    - **M026 lifecycle**: sent_to_email, sent_at, accepted_at, accepted_by_email, state (pending/accepted/expired/canceled)
- `customer_h24_codes` (codici permanenti cliente, es. "DOMARC-DATIA")
- `customer_h24_codes_usage` (storico utilizzi)
    - **M026**: from_email, body_excerpt (max 4000 char) per audit completo
- `smtp_relay_h24_targets` (mailbox di rientro multi-brand)
    - source_domain → h24_alias + urgent_fee_eur
    - **M024**: source_email per match più specifico del solo dominio

### 4.7 Recipient groups (Migration 025 + 027 + 030 shadow)

- `recipient_groups`: gruppi logici di indirizzi destinatari (gemello di customer_groups)
- `recipient_group_members`: email → group_id
- `recipients`: autodiscovery destinatari visti (popolato dal listener via api.py:_autodiscover_recipients)
- **M030**: `recipient_groups.shadow_mode`, `shadow_note` per shadow mode per gruppo

Use case: "Tecnici no fuori orario" → regola con `match_to_group_id=tecnici_no_fo` + `match_in_service=False` → `forward_to_emails=h24@datia.it`.

### 4.8 Customer sync agnostico (Migration 028)

```sql
CREATE TABLE customer_sync_sources (
    id INTEGER PRIMARY KEY,
    tenant_id INTEGER NOT NULL DEFAULT 1,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,            -- postgres | mssql | csv_file | json_url
    enabled INTEGER NOT NULL DEFAULT 1,
    config_json TEXT NOT NULL,     -- {host,port,user,password_enc,dbname} | {path,delimiter,encoding} | {url,headers,auth_enc}
    query_or_path TEXT,            -- SQL per postgres/mssql; JSONPath per json_url; null per csv
    mapping_json TEXT NOT NULL,    -- {"src_col": "target_col"} o {"src_col": {"target":"col","transform":"split:,"}}
    schedule_hours INTEGER NOT NULL DEFAULT 24,
    on_missing TEXT NOT NULL DEFAULT 'flag',  -- flag | delete | keep
    last_run_at, last_run_status, last_run_error, next_run_at,
    UNIQUE (tenant_id, name)
);
CREATE TABLE customer_sync_runs (
    id INTEGER PRIMARY KEY, source_id INTEGER, started_at, finished_at,
    status, n_fetched, n_inserted, n_updated, n_unchanged,
    n_flagged_missing, n_errored, error_message, triggered_by, report_json
);
ALTER TABLE customers ADD COLUMN last_synced_from_source_id INTEGER;
```

Provider in `customer_sync/` (postgres, mssql, csv_file, json_url).
Engine `customer_sync/engine.py`: fetch → map → upsert → on_missing policy → audit.
Scheduler `customer_sync/scheduler.py`: thread loop 60s, lock per worker concorrenti.

### 4.9 Rule sets (Migration 029) + thread tracking (Migration 036)

- **M029**: `rule_sets` (`globali`, `std_window`, `ext_window`,
  `h24_window`) + `rules.rule_set_id`. Dopo M035 sono **solo
  organizzazione UI** per profilo orario, NON gating runtime.
- **M036**: ALTER `events_log` ADD `in_reply_to`, `references_json`,
  `reply_to_event_uuid`, `thread_root_uuid` + ALTER `rules` ADD
  `match_is_thread_continuation` (tristate). Seed regola "Thread
  continuation — passa al destinatario" priority=5 in `globali` con
  azione `default_delivery`.

### 4.10 Shadow mode in cascata (M030/M031/M033)

| Migration | ALTER | Cosa abilita |
|---|---|---|
| 030 | `recipient_groups` ADD `shadow_mode`, `shadow_note` | Solo destinatari del gruppo in shadow |
| 031 | `domain_routes` ADD `shadow_mode`, `shadow_note` | Tutto il dominio in shadow (override globale) |
| 033 | `rules` ADD `shadow_mode`, `shadow_note` | Solo quella singola regola in shadow |

Cascata: dominio shadow → tutto in shadow; recipient_group shadow →
solo membri; rule shadow → solo match. Audit in
`events_log.shadow_action` / `shadow_rule_id` ricostruisce "cosa
sarebbe successo".

---

## 5. Rule Engine v2 — concetti chiave

### 5.1 Ordinamento e match

1. Le regole sono raggruppate per `scope_type` (`global`, `mailbox`, `sector_pack`).
2. Dentro ogni scope sono ordinate per `priority` ascendente (più bassa = prima).
3. Si itera: la **prima** che matcha tutti i criteri vince. Se `continue_after_match=True`, dopo l'azione si continua a valutare.
4. Se nessuna regola matcha → `default_delivery` (forward al destinatario reale via smarthost).

### 5.2 Gerarchia padre/figlio (gruppi)

Gruppo: `is_group=1`, ha criteri di match condivisi (es. "from_domain=cloudtik.it") ma **non ha azione**.

Figli: `parent_id=<id_gruppo>`, ereditano i match del padre, possono raffinarli (AND aggiuntivo) e hanno azione propria. `action_map` figlio + padre uniti via `deep_merge` (figlio override padre).

Flag di gruppo:
- `exclusive_match=1` (default): nel gruppo vince un solo figlio
- `continue_in_group=1`: dopo un figlio matchato, valuta anche gli altri figli
- `exit_group_continue=1`: dopo aver finito col gruppo, valuta anche le regole successive al gruppo

### 5.3 Validazione (route layer)

- V001: `match_*_regex` deve essere regex Python valida
- V003: orfani senza criteri sono catch-all pericolosi → almeno un match_* o scope_ref
- V004: gruppi con `is_group=1` devono avere almeno un match_* condiviso
- V_PRI_RANGE: priorità 1-9999
- UNIQUE (tenant_id, scope_type, scope_ref, priority)
- Mutex `match_to_regex` vs `match_to_group_id`

### 5.4 Thread continuation (M036)

Pre-rule-engine, `pipeline.py` chiama `find_thread_root(in_reply_to,
references)` di `relay/storage.py`. Se la mail risponde a un
`message_id` già presente in `events_log`:
- `ev_dict["is_thread_continuation"] = True`
- `extra["reply_to_event_uuid"]` e `extra["thread_root_uuid"]`
  popolati
- L'evento eredita `ticket_id` dal parent
- La regola seed (priority=5 in `globali`) con
  `match_is_thread_continuation=1` matcha → `default_delivery` →
  NESSUN nuovo ticket

Per disabilitare per casi specifici, basta una regola a priorità
inferiore con `match_is_thread_continuation=0` o tristate libero.

### 5.5 Shadow mode runtime

Cascata in `pipeline.py`:
1. **Domain shadow** (M031): se `domain_routes.shadow_mode=1` per il
   dominio destinatario → tutto l'evento in shadow → action effettiva
   `default_delivery`, action shadow registrata in `events_log`.
2. **Recipient group shadow** (M030): per ogni destinatario
   appartenente a un gruppo shadow → solo per quel destinatario
   shadow.
3. **Rule shadow** (M033): se la regola matchata ha `shadow_mode=1` →
   action shadow registrata, action effettiva `default_delivery`.

### 5.6 Falsi positivi H24 (Fix B 2026-05-05)

Quando `action=create_authorized_ticket`:
- Il regex estrae un codice dal subject
- `validate_auth_code()` lookup in DB → se `not_found` E `extracted_from_subject=True`:
    → ritorna `ActionResult` con `extra.h24_false_positive=True`
    → pipeline.py ri-evalua `engine.evaluate(exclude_rule_ids={rule_id})`
    → le regole successive vengono valutate normalmente
- Esempio: mail CloudTIK con subject `[OptiWize] - RT-FRANCESCHETTA-4833 | Problem:` non era un codice H24 ma il regex lo estraeva → ora si recupera con regola CloudTIK alert (id 55).

---

## 6. Integrazione AI

### 6.1 Architettura

```
Mail → Rule engine → match action='ai_classify' →
       PII redactor (regex IT + spaCy + dizionario custom) →
       AI Router (lookup binding per job_code, traffic split A/B) →
       Provider (Claude Haiku/Sonnet o DGX locale) →
       Decision storage (ai_decisions) →
       Action dispatcher applica (se non shadow) o solo log (se shadow)
```

### 6.2 Job catalog

`ai_jobs` (immutabile, seed in M012):
- `classify_email` — classificazione mail (intent, urgenza, summary, suggested_action) — Haiku default
- `summarize` — riassunto per ticket — Haiku
- `critical_classify` — escalation per decisioni critiche — Sonnet
- `error_embedding` — embedding per cluster — modello locale (DGX) se disponibile
- `rule_proposal` — proposta regole statiche dal learning loop — Haiku

### 6.3 Shadow mode

Setting `ai_shadow_mode=true` (default ON al primo deploy):
- L'IA viene interpellata, decisione loggata in `ai_decisions` con `applied=false`
- Il rule engine applica comunque la sua decisione
- Operatore confronta in `/ai/decisions` cosa avrebbe fatto l'IA vs cosa ha fatto il rule engine
- Quando soddisfatto → `ai_shadow_mode=false` (audit in `ai_shadow_audit`)

### 6.4 Fail-safe

Se Claude timeout (5s default) / errore / budget esaurito:
- Mail forwardata a `ai-fallback@domarc.it`
- Ticket urgenza ALTA con `ai_unavailable=true` in `payload_metadata`

Cost cap: setting `ai_daily_budget_usd` (default $50). Reset a 00:00 UTC.

### 6.5 PII redactor

Pipeline applicata **prima di ogni chiamata Claude**:
1. Regex: IBAN, codice fiscale italiano, P.IVA, telefono italiano, email, URL+token
2. spaCy `it_core_news_sm` (50MB, scaricato a setup): nomi propri (PER), aziende (ORG), località (LOC)
3. `ai_pii_dictionary` (custom per cliente)

Sostituzioni: `<PERSONA_1>`, `<AZIENDA_2>`, `<IBAN>`, ecc.

### 6.6 Error clusters semantici (F2)

Sostituzione semantica delle `error_aggregations` deterministiche:
- Embedding `paraphrase-multilingual-MiniLM-L12-v2` 384dim, italiano-friendly, CPU-friendly
- Cluster lookup per cosine similarity > 0.85
- `manual_threshold` (default 5) prima di aprire ticket aggregato
- Recovery via keyword (`ok/resolved/recovered/cleared`) o classify IA

Le `error_aggregations` legacy (regex) restano come fallback se IA off.

### 6.7 Rule proposer (learning loop)

Worker async analizza `ai_decisions`:
- Raggruppa per pattern coerente (intent + subject normalizzato + from_domain)
- Se ≥ 20 decisioni con classificazione consistente → genera record in `ai_rule_proposals`
- Operatore review in `/ai/proposals` → accept crea regola in `rules` con `created_by='ai_proposal_<id>'`
- Le mail successive con quel pattern matchano la regola statica → no più chiamata IA → riduzione costi nel tempo

---

## 7. UI — struttura attuale

### 7.1 Menu sidebar (post-refactor 2026-05-05)

```
Dashboard | Flusso mail
▼ Regole & Mail flow      (Regole, Template, Aggregazioni con tab statiche/AI)
▼ H24 & Autorizzazioni    (Panoramica /codes-h24/, Monouso, Permanenti, Mailbox, Settings)
▼ Anagrafiche             (Clienti, Gruppi clienti, Gruppi destinatari, Sync esterni, Indirizzi, Domini, Privacy)
▼ Orari                   (Orari per cliente, Profili)
AI Assistant              (top-level, admin only)
▼ Sistema                 (Utenti, Health, Integrazioni, Settings, API keys, Documentazione)
```

### 7.2 Convenzioni UI

- Tutte le tabelle `.dr-table` / `.fw-table` hanno **sort cliccabile** (`static/js/sortable.js`)
- Indicatore globale nell'header: badge **verde "OK"** o **rosso "KILL ON"** sempre visibile
- Form regole: **4 sotto-card espanse** (Origine + anagrafica cliente, Destinazione, Oggetto/Contenuto, Orario)
- Mutex visivo: campi reciprocamente esclusivi grigiati con messaggio
- Preset orari: pulsanti che riempiono `match_at_hours` dai profili esistenti
- Editor template HTML: CodeMirror 5 + preview iframe live + toolbar variabili Jinja

### 7.3 File template critici

| Template | Scopo |
|---|---|
| `_base.html` | Header + sidebar + flash messages — tocca solo per nav globale |
| `_group_form_macro.html` | Macro Jinja shared per group form (customer + recipient) |
| `dashboard.html` | KPI principale + box H24 KPI assorbito |
| `rule_form.html` | Form regola con 4 sotto-card e preset orari |
| `template_form.html` | Editor HTML CodeMirror + preview live |
| `codes_h24_overview.html` | Panoramica unificata codici H24 con 3 tab |
| `aggregations_list.html` + `ai_clusters.html` | Coppia con barra tab condivisa |
| `addresses_list.html` | Lista mittenti/destinatari con bulk action su `/addresses-to` |
| `customers_list.html` | Filtri AND/OR/NOT + bulk action |

---

## 8. API endpoints (admin → listener)

Tutti sotto `/api/v1/relay/`, autenticati via `X-API-Key` (no CSRF). Chiave in `settings.relay_api_key`.

### 8.1 Sync endpoints (GET, dato lo scheduler legge)

| Endpoint | Cosa restituisce |
|---|---|
| `/customers/active` | Tutti i clienti del tenant |
| `/customer-groups/active` | Gruppi clienti + membri |
| `/recipient-groups/active` | M027 — gruppi destinatari + membri (+ M030 shadow flags) |
| `/rules/active` | Regole flat (gruppi espansi in figli con action_map deep-merged) |
| `/templates/active` | Template attivi |
| `/aggregations/active` | Aggregazioni statiche attive |
| `/privacy-bypass/active` | Lista privacy bypass |
| `/h24-targets/active` | Mailbox di rientro multi-brand |
| `/settings/active` | Settings rilevanti per il listener |
| `/routes/active` | Smarthost per dominio |
| `/domain-routing/active` | Domain routing |

### 8.2 Action endpoints (POST, listener invoca admin)

| Endpoint | Scopo |
|---|---|
| `/events` | Listener flush eventi → admin per persistence |
| `/auth-codes` | Listener emette codice monouso (genera + assegna) |
| `/auth-codes/sent` | Marca a posteriori sent_to_email (M026) |
| `/auth-codes/validate` | Valida codice da subject (cascade oneshot atomico → permanente) |
| `/ai/classify` | Inferenza inline IA (timeout 5s) |
| `/aggregations/<id>/occurrence` | Replica occurrence statica |
| `/maintenance/cleanup-oneshot-codes` | Cleanup codici monouso scaduti (nightly) |

### 8.3 Endpoint UI (no api/v1, autenticati via session)

- `/templates/preview` (POST) — render Jinja con context demo per editor template (CSRF-exempt, idempotente)

---

## 9. Listener — file chiave

`/opt/stormshield-smtp-relay/relay/`:

| File | Cosa fa |
|---|---|
| `__main__.py` | Entry point CLI (listener / scheduler) |
| `listener.py` | aiosmtpd handler |
| `parser.py` | Parsing MIME, decode RFC 2047, attachment extraction |
| `pipeline.py` | Process orchestrator, customer resolve, rule eval, dispatch, persist |
| `rules.py` | RuleEngine v2 (evaluate con exclude_rule_ids) |
| `actions.py` | do_forward, do_auto_reply, do_create_authorized_ticket, ecc. |
| `aggregations.py` | Aggregazioni statiche fingerprint |
| `forwarder.py` | Outbound SMTP delivery con retry/backoff |
| `manager_client.py` | Backend HTTP verso admin (StormshieldManagerBackend) |
| `storage.py` | SQLite cache locale (rules_cache, customers_cache, recipient_groups_cache, ecc.) |
| `sync.py` | Sync periodico fetch_active_* |
| `scheduler.py` | Loop scheduler (sync 5min, events flush, outbound drain, cleanup nightly) |
| `service_hours.py` | Logica in_service / has_exception_today |
| `auto_reply.py` | Build MIME auto-reply da template Jinja con allegati |

---

## 10. Stato attuale (snapshot)

### 10.1 Versione + commit

```
$ git log --oneline -8
6c42553 feat(templates): editor HTML CodeMirror + preview live + toolbar variabili
b2e9162 refactor(ui): riorganizzazione globale - menu, tab unificate, kill switch, form regola
7d4ea8f refactor(ui): form regola riorganizzato in 5 sotto-sezioni logiche
4c920ec fix(rules,listener): falsi positivi regola H24 + nuova regola CloudTIK alert
a5da9c2 docs+ui: manuale aggiornato + refactor recipient groups + sort tabelle
de7069c feat(listener): subject decode RFC 2047 + autodiscovery + recipient groups + forward emails
825f866 feat: recipient groups + autodiscovery + tracking codici + fix RFC 2047 + template no-contract
d389faa feat(ui): righe editabili in /h24-targets + profilo orario contestuale in /customers
```

### 10.2 Migrations applicate

```
1-9    initial + routes + addresses + auth + tenant
010    rule_groups (gerarchia padre/figlio)
011    privacy_bypass
012    ai_assistant (tabelle AI complete)
013-17 ai_shadow_audit, error_clusters, proposals, ecc.
018    customer_groups
019    customers_pg_cache
020    contract_type
021    aggregations_delay_minutes
022    h24_authorization_flow (codici + targets)
023    rule_description
024    h24_target_source_email
025    recipient_groups + members + recipients (autodiscovery)
026    h24_tracking_extended (sent_to/sent_at/accepted/state + body_excerpt)
027    rules_recipient_match (match_to_group_id + forward_to_emails)
028    customer_sync_sources + customer_sync_runs (rename customers_pg_cache → customers, sorgente legacy seed)
029    rule_sets (organizzazione UI per profilo orario, post-M035 non più gating)
030    shadow_recipient_groups (shadow_mode + shadow_note)
031    shadow_domain_routing (shadow_mode + shadow_note)
033    shadow_rules (shadow_mode + shadow_note su rules)
034    group_membership_rules (auto-assignment self-contained gruppi cliente)
035    simplify_rules_to_group_based (filtro contratto solo via match_customer_groups)
036    thread_tracking (in_reply_to/references/reply_to_event_uuid + match_is_thread_continuation + seed regola)
```

### 10.3 Regole esempio in produzione

```
1     Codice H24 in subject (qualsiasi mailbox)   priority=1   regex ristretto post-fix 2026-05-05
60    Thread continuation — passa al destinatario priority=5   M036, seed in `globali`
55    CloudTIK/OptiWize alert - Problem            priority=60  Fix C 2026-05-05
44    Da evento — riccardo.grandi@gmail.com        priority=100
28    Auto-reply out_of_hours                       priority=510
29    Crea ticket NORMALE                           priority=520
42    Catch-all — passa al destinatario reale       priority=999
```

### 10.4 Servizi VM 192.168.4.25

```
domarc-smtp-relay-admin.service          active (Flask + nginx 443)
stormshield-smtp-relay-listener.service  active (aiosmtpd :25)
stormshield-smtp-relay-scheduler.service active (sync 5min)
```

### 10.5 Bug noti recenti risolti

- ✅ **2026-05-04**: doppio encoding RFC 2047 nel subject delle reply (fix in parser.py:_decode_mime_header)
- ✅ **2026-05-04**: cliente senza contratto non riceveva template dedicato (override automatico actions.py)
- ✅ **2026-05-05**: regola H24 catturava nomi device CloudTIK come codici (regex ristretto + recovery falsi positivi)
- ✅ **2026-05-05**: risposta a thread tracked apriva un nuovo ticket (M036 thread tracking RFC 2822)
- ✅ **2026-05-05**: form gruppo padre/figlio mancavano di campi importanti (recipient_groups, fasce orarie, subject/body, gruppi cliente) — sincronizzati 3 form

### 10.6 Limiti noti / da gestire

- Test offline su singolo evento via `/rules/<id>/simulate` esistente, ma non c'è suite end-to-end automatizzata
- DGX Spark (Fase 4 IA) non ancora deployato — Claude API è l'unico provider attivo
- 8 clienti con `availability_type_id=NULL` ma `contract_active=True` — anomalia gestionale (cliente attivo senza profilo orario assegnato)
- Multi-tenant: `tenant_id=1` hardcoded in routes/api.py:120 — TODO se serve veramente tenant isolation

---

## 11. Comandi rapidi

```bash
# === Status & log ===
ssh root@192.168.4.25 'systemctl status domarc-smtp-relay-admin stormshield-smtp-relay-listener stormshield-smtp-relay-scheduler --no-pager'
ssh root@192.168.4.25 'journalctl -u stormshield-smtp-relay-listener -f'

# === DB query ===
ssh root@192.168.4.25 'sqlite3 /var/lib/domarc-smtp-relay-admin/admin.db "SELECT id, name, priority, action FROM rules WHERE enabled=1 ORDER BY priority;"'

# === API key per test ===
ssh root@192.168.4.25 'sqlite3 /var/lib/domarc-smtp-relay-admin/admin.db "SELECT value FROM settings WHERE key=\"relay_api_key\";"'

# === Force sync listener (restart) ===
ssh root@192.168.4.25 'systemctl restart stormshield-smtp-relay-scheduler'

# === Smoke test endpoint ===
ssh root@192.168.4.25 'curl -sk -o /dev/null -w "%{http_code}\n" -L https://localhost/'

# === Bootstrap password (login UI) ===
ssh root@192.168.4.25 'grep BOOTSTRAP_PASSWORD /etc/domarc-smtp-relay-admin/secrets.env'
# → user: admin, pwd: domarc2026
```

---

## 12. Convenzioni di lavoro (preferenze utente)

- **Mai modificare codice esistente per "fixare" problemi di nuove funzionalità** — usa wrapper/extension/feature flag/file separato. Vedi `docs/materiale/SISTEMA_PROTEZIONE_CODICE_ESISTENTE.md` nel manager.
- **Italiano** in commit, manuale, log, UI. Identifier tecnici (path, funzioni, var) in italiano dove convenzionale (`codice_cliente`, `ragione_sociale`) o inglese tecnico (`event_uuid`, `auth_code`).
- **Test indirizzi**: solo `r.grandi@domarc.it` o `r.grandi@datia.it`. **MAI** `info@`, `monitoring@`, ecc. (caselle reali).
- **Modifica server-side**: scp locale → remoto, no editor in SSH diretto.
- **CHANGELOG**: aggiornato sui progetti del manager, non qui (qui usiamo commit message + git log).
- **Mai pushare senza richiesta esplicita** dell'utente, ma in questo repo è ok perché l'utente ha sempre detto di farlo. Per modifiche grosse, validare l'approccio prima.
- **Memory file**: l'utente ha un sistema di memoria persistente (`/root/.claude/projects/.../memory/`). Usalo per ciò che è non ovvio dal codice.

---

## 13. Roadmap / cose pendenti

Priorità:

1. **DGX Spark (Fase 4 AI)**: server NVIDIA acquistato, deploy NVIDIA NIM o Ollama+vLLM con OpenAI-compatible endpoint per spostare `error_embedding` e `pii_ner_assist` dal Claude API al locale. Vedi `docs/ai_assistant.md` se esistente o piano in memoria progetto.
2. **Suite test e2e** automatizzata del rule engine (oggi solo simulate manuale).
3. **Migrazione `error_aggregations` → solo `ai_error_clusters`** quando AI è stabile da N giorni in live (oggi coesistono).
4. **Tenant isolation reale** se servirà multi-cliente (oggi tutto su tenant_id=1).

---

## 14. Quando in dubbio

1. **Leggi il commit log** — i commit hanno messaggi dettagliati che spiegano il "perché"
2. **Leggi `docs/manuale_utente/MANUALE_UTENTE.md`** per visione utente delle feature
3. **Leggi `docs/guida_funzionamento.md`** per visione tecnica delle feature
4. **Cerca nel codice con grep** — i commenti inline sono ricchi di "perché" specialmente in actions.py, pipeline.py, rules.py
5. **Chiedi all'utente** prima di destructive operations: drop tabelle, force push, reset DB, modifiche al kill switch attivo

---

*Documento mantenuto a mano, aggiornare quando arrivano feature significative. Ultima modifica: 2026-05-05 dopo M028-M036 (customer sync agnostico, gruppi self-contained, shadow mode in cascata, thread tracking RFC 2822, semplificazione rule engine, sync form regole).*
