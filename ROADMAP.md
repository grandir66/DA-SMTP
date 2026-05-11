# Roadmap — Manager + SMTP Relay

> **Fonte di verità unica** per evoluzioni in corso sui due progetti paralleli.
> File mantenuto sessione-per-sessione: ogni nuova feature emersa va annotata qui
> con stato + data; ogni completamento va spuntato. Cercare conferme nel codice
> prima di marcare `[x]`.

**Ambienti:**
- **Manager**: `/opt/domarc/stormshield-manager/web_interface/` (host dev `da-sns-dev` 192.168.4.41, prod `da-sns` 192.168.4.42).
- **SMTP Relay**: `/opt/domarc-smtp-relay-admin/` (runtime VM `da-smtp-ia` 192.168.4.25, repo `grandir66/DA-SMTP`).
- **ESVA antispam**: SAMNET 192.168.20.x.

**Convenzioni stato:**
- `[ ]` da fare
- `[~]` in corso
- `[x]` fatto (verificare in codice prima di marcare)
- `[!]` bloccato / decisione utente attesa

**Ultimo aggiornamento:** 2026-05-11 (sessione standardize + cutover prep + relay ACL + firewall UI).

---

## 0. Recently completed (cronologico inverso)

### Sessione 2026-05-11 (notte/mattino)
- [x] **Standardize project** (commit `08c0df0`): CLAUDE.md slim 568→89 righe, `.claude/skills/{deploy,release,db-migration}`, `.claude/rules/{flask-routes,db-access,migrations,ai-payload}`, `.claude/settings.json` con hook py_compile, `docs/adr/0000-template.md`.
- [x] **Review pre-cutover domarc.it** con 5 agenti paralleli: 11 BLOCKER + 10 SERIO identificati.
- [x] **21 fix applicati** (commit `dbc316d`):
  - BLOCKER: bug NameError aggregations, colonne events_log mancanti, UFW restrict, Gunicorn embeddato, backup SQLite consistente, customer_sync safety threshold 50%, ProxyFix+secure cookies, relay_api_key→bcrypt hash, TLS verify, indice events_log(message_id), backup systemd timer.
  - SERIO: pii_redactor su AI samples, role check attachment DELETE, password complexity, login rate-limit, cache_grace_ttl_sec attivo, anti-loop noreply, migration 038 IF NOT EXISTS, apply_migrations transazionale, pipeline exception→DLQ, scheduler heartbeat 6 loop.
- [x] **Shadow mode `domarc.it`** attivo (commit non necessario, solo UPDATE DB).
- [x] **Topologia rete documentata** (commit `e02ccd7`): ESVA su SAMNET 192.168.20.x, notebook utente su 192.168.99.x.
- [x] **UFW finalizzato**: :25 ristretto a 192.168.4.0/24 + 192.168.20.0/24, :443/:80 Anywhere (dietro firewall aziendale).
- [x] **Relay client ACL** (commit `786c3d5`): migration 040 + UI `/relay-acl/` + check applicativo nel listener handle_MAIL. Voce sidebar **Sistema → Relay client ACL**.
- [x] **Relay ACL edit form** + **Firewall UFW UI** (sessione 2026-05-11 mattina): form esteso per modifica completa entry ACL (label/description/enabled); pagina `/firewall/` superadmin-only con status UFW + add/delete regole + reload, via wrapper `firewall_manager.py` + sudoers ristretto `/etc/sudoers.d/domarc-relay-ufw`.

---

## 1. Manager (`stormshield-manager`) — stato non verificato in questa sessione

*Lavorato dal progetto Claude Code del manager. Verificare lo stato esatto lì.*

### 1.1 Modulo `ai_assistant` — Fase 0
- [ ] Creare `modules/ai_assistant/` provider-agnostic (Claude API come primo backend).
- [ ] Niente backfill su mail storiche: l'IA opera solo sul flusso entrante.
- [ ] PII redactor italiano (regex IBAN/CF/P.IVA + spaCy `it_core_news_sm`).
- [ ] UI base: log decisioni, costi, dashboard semplice.
- [ ] Allineare interfacce con il modulo gemello sul relay (riusare provider/redactor se possibile).

### 1.2 Customer Services Aggregator
- [ ] Nuovo modulo che aggrega servizi reali del cliente: firewall Stormshield (`devices`), VM Proxmox, endpoint ESET Connect, mailbox ESVA, linee WIC, interni 3CX, job di backup.
- [ ] Sostituire progressivamente `customer_additional_products` (resta come fallback).
- [ ] Pagina cliente mostra "servizi attivi reali" non più "prodotti contratto".

### 1.3 Badge cliente → servizi SW futuri
- [ ] 69 articoli SW unmapped (DATIA / ESVA / ESET / Office365 / firewall): incrociare con stato moduli, non solo presenza in contratto.
- [ ] Definire mapping articolo → check tecnico (es. ESVA mailbox attive ≠ riga di contratto).

### 1.4 Service profiles canonici
- [ ] 4 profili (standard / esteso / h24 / no servizio) con orari reali.
- [ ] Sostituiscono i 7 profili legacy.
- [ ] Workflow autorizzazione interventi fuori orario (chi approva, log decisione).

### 1.5 Email Conversations + Mini-Ticket (modulo `ingestion`)
- [ ] Threading conversazioni (Message-ID / In-Reply-To / References).
- [ ] Inventario codice `INV-AAAA-NNNNNN`.
- [ ] Mini-ticket leggeri (alternativa al ticket gestionale pesante).
- [ ] Coesistenza con `tickets_manager` MSSQL (no migrazione forzata).

### 1.6 Modulo `ingestion_smtp` (pilota)
- [ ] Nuovo `modules/ingestion_smtp/` alias `mail-pilot.domarc.it`.
- [ ] IMAP + SMTP coesistenti.
- [ ] Analisi AI **asincrona** (no blocco delivery).
- [ ] Bucket M365 (assistenza) come pilota.

### 1.7 Quotes ERP Export/Import
- [ ] Sotto-pacchetto `modules/quotes_manager/erp/`.
- [ ] Push PG → MSSQL `offeh*` (export verso gestionale).
- [ ] Pull MSSQL → nuovo PG (import).
- [ ] Target dev = 4.14, prod = 4.4.
- [ ] Convenzioni: serie `S`, `rk='A'`, divisori `**` / `*`.

### 1.8 Architettura — uscita dalla replica Docker
- [ ] Rimuovere replica MSSQL Docker locale.
- [ ] Flask dev → 4.14 diretto, Flask prod → 4.4 diretto.
- [ ] Cache letture in PG `solution`.
- [ ] Verificare moduli che ancora puntano alla replica.

### 1.9 Server GPU NVIDIA
- [!] Specs TBD (utente le condividerà).
- [ ] Setup post-Fase 0 di `ai_assistant`.
- [ ] Stesso server può servire anche il relay (vedi §2.5).

---

## 2. SMTP Relay (`domarc-smtp-relay`)

### 2.1 Cutover `domarc.it` (era: pilota datia.it → produzione)
- [x] Flusso `datia.it` attivo via VM 4.25 (in osservazione, attualmente in `shadow_mode=1`).
- [x] **`domarc.it` configurato in `domain_routing`** (smarthost `domarc-it.mail.protection.outlook.com:25`).
- [x] **`domarc.it.shadow_mode = 1`** attivato 2026-05-11 per osservazione 48-72h.
- [ ] **Cambio MX `domarc.it`** verso ESVA/relay quando shadow ha mostrato traffico atteso.
- [ ] Popolare `/privacy-bypass` per `domarc.it` con ruoli sensibili emersi da `events_log.shadow_action`.
- [ ] Disattivare `domarc.it.shadow_mode` per andare live.
- [ ] Per i test usare SOLO `r.grandi@domarc.it` o `r.grandi@datia.it`, mai `info@`/`monitoring@`.

### 2.2 Modulo `ai_assistant` — Fase 1 (foundation)
- [x] Migration 012 + 7 tabelle: `ai_providers`, `ai_jobs`, `ai_job_bindings`, `ai_decisions`, `ai_error_clusters`, `ai_rule_proposals`, `ai_pii_dictionary`, `ai_shadow_audit` (8 tabelle in realtà).
- [x] Package `domarc_relay_admin/ai_assistant/` (router, providers, redactor, decisions, rule_generator, rule_proposer, error_aggregator).
- [x] Provider Claude (Anthropic SDK).
- [x] PII redactor IT (in `pii_redactor.py`; spaCy NER da verificare).
- [x] Endpoint inferenza inline `POST /api/v1/relay/ai/classify` chiamato dal listener.
- [x] Action listener `do_ai_classify` con fail-safe.
- [x] UI `/ai/models`, `/ai/decisions`, `/ai/dashboard`, `/ai/providers` (blueprint `ai_bp` registrato).
- [x] AI rule wizard (UI `/ai-rules` per generare regole da descrizione/samples).
- [x] Shadow mode globale toggleable.
- [x] Doc `docs/ai_assistant.md`.
- [ ] Verifica: prompt caching attivo e funzionante (sessione 2026-05-11 ha standardizzato chiamate Claude, ma il caching va misurato).
- [ ] Test coverage: i 194 test pytest passano ma manca audit specifico AI router/PII redactor/decisions.

### 2.3 Modulo `ai_assistant` — Fase 2 (error aggregator semantico)
- [~] `error_aggregator.py` esiste ma usa logica regex/fingerprint, NON embedding semantico.
- [ ] Embedding model `paraphrase-multilingual-MiniLM-L12-v2` in-memory.
- [ ] Worker async: eventi error → embedding → cluster lookup → soglia manuale → ticket.
- [ ] UI `/ai/clusters` con drill-down + edit `manual_threshold` e `manual_recovery_window_min`.
- [ ] Recovery semantico: classifica messaggi "ok/recovered/cleared" → chiude ticket.
- [x] `error_aggregations` (legacy) resta come fallback, fixato il bug NameError che impediva apertura ticket.

### 2.4 Modulo `ai_assistant` — Fase 3 (rule proposer + uscita shadow)
- [~] `rule_proposer.py` esiste ma logica "≥ 20 decisioni simili → proposta" da verificare.
- [x] AI rule wizard funzionante (modalità descrizione + samples).
- [ ] UI `/ai/proposals` con accept/reject; accept → riga in `rules`.
- [ ] Dashboard `/ai/dashboard` con decisioni/giorno, costo cumulativo, accuracy, top patterns (UI esiste, verificare metriche).
- [ ] Switch atomico `ai_shadow_mode = false` con audit log.

### 2.5 Modulo `ai_assistant` — Fase 4 (DGX Spark self-hosted)
- [ ] Setup server NVIDIA (NIM / Ollama / vLLM, OpenAI-compatible endpoint).
- [ ] Modello suggerito: Llama 3.1 8B Q5_K_M o Mistral Nemo 12B.
- [ ] Embedding locale: `nomic-embed-text-v1.5`.
- [ ] Provider `local_http_provider.py`.
- [ ] Migrazione bindings ad alta frequenza sul DGX.
- [ ] Doc `docs/dgx_spark_setup.md`.

### 2.6 Coda mail e gestione capacity
- [x] Volume target: ~100 mail/h (capacità listener > 30× il richiesto).
- [x] Kill switch `relay_passthrough_only` documentato in CLAUDE.md e operativo.
- [ ] Verificare periodicamente la dashboard coda (mail in coda, tempi, mittenti).

### 2.7 Cleanup ambienti
- [x] Workflow git push diretto da VM 4.25 attivo (SSH a GitHub).
- [x] Archivio finale 4.41 in `/var/lib/domarc-smtp-relay-backups/archive-FINAL-20260430_133046/`.
- [ ] Decidere fato del clone su 4.41 (`/opt/domarc-smtp-relay-admin/` post-cleanup): rimuovere o tenere read-only come backup.

### 2.8 Integrations panel
- [x] UI `/integrations` con 3 sezioni (DB sorgente clienti, API ticket manager, API key IA).
- [x] Test live per ciascuna integrazione.
- [ ] Utente deve completare configurazione Ticket API in produzione VM (credenziali).

### 2.9 Hardening produzione (nuova sezione 2026-05-11)
- [x] Gunicorn embeddato (4w/8t gthread, /dev/shm) sostituisce Werkzeug.
- [x] ProxyFix + SESSION_COOKIE_SECURE + WTF_CSRF_SSL_STRICT in prod.
- [x] Errorhandler 404/403/500 con pagina utente generica.
- [x] Bootstrap fail-fast: rifiuta avvio in prod se `DOMARC_RELAY_BOOTSTRAP_PASSWORD` mancante o <10 char.
- [x] Login rate-limit (5 fail/15min → lockout 15min).
- [x] Password complexity (min 10 char, 3 tipi/4, blacklist).
- [x] `relay_api_key` migrata a bcrypt hash con auto-migration.
- [x] UFW: :25 ristretto subnet interne, :443/:80 Anywhere (firewall aziendale a monte).
- [x] Backup systemd timer 03:00 daily + retention 14gg + passphrase in `/etc/domarc-backup-pass`.
- [x] Scheduler heartbeat 6 loop (sync, events_flush, routes_reload, outbound_drain, dispatch_drain, pending_tickets).
- [x] Listener pipeline exception → DLQ quarantine.
- [x] Customer sync safety threshold 50%.
- [x] `apply_migrations` transazionale con ROLLBACK.
- [ ] **Cambio `DOMARC_RELAY_BOOTSTRAP_PASSWORD`** da `domarc2026` (debole, blacklistata) a passphrase 16+ char random.
- [ ] **Cancellare `settings.relay_api_key`** plaintext da admin.db dopo copia nel listener.
- [ ] **Verifica cert manager 4.41** ora che `TLS_VERIFY=1`: `curl -v https://manager-dev.domarc.it/`.
- [ ] **Configurare NAS host** per rsync notturno backup off-site (decommentare `ExecStartPost` in `systemd/domarc-backup.service`).

### 2.10 Relay client ACL (nuova sezione 2026-05-11)
- [x] Migration 040 + tabella `relay_client_acl` + cache `relay_client_acl_cache` lato listener.
- [x] UI `/relay-acl/` con quick-add + toggle + delete + validation IP/CIDR via `ipaddress`.
- [x] **Form esteso `/relay-acl/new` e `/relay-acl/<id>/edit`** per modifica completa (label, description, ip_or_cidr, enabled).
- [x] Listener `handle_MAIL` enforce check `is_client_allowed(session.peer[0])`.
- [x] Voce sidebar **Sistema → Relay client ACL**.
- [ ] **Popolare con subnet di produzione** (suggerito: `192.168.20.0/24` ESVA, `192.168.4.0/24` debug locale). Finché lista vuota, enforcement OFF (backward compat).

### 2.12 Firewall UFW via UI (nuova sezione 2026-05-11)
- [x] Modulo `firewall_manager.py`: wrapper sicuro per `ufw` con subprocess shell=False + whitelist regex su port/proto/source/comment.
- [x] Sudoers `/etc/sudoers.d/domarc-relay-ufw`: NOPASSWD per `domarc-relay` su `ufw status/allow/--force delete/reload/enable/disable`.
- [x] Blueprint `firewall_bp` (route `/firewall/`) — solo `superadmin`. Parse di `ufw status numbered`, add/delete regole, reload.
- [x] Template `firewall.html` con status banner (active/inactive), form add + tabella regole + reload + warning lock-out SSH.
- [x] Voce sidebar **Sistema → Firewall UFW** (rosso, visibile solo a superadmin).
- [ ] Confirmation modal JS più robusto (oggi `confirm()` nativo).
- [ ] Audit log persistente in admin.db (oggi solo `logger.warning` → journal).

### 2.11 Mailbox interne domarc.it (da verificare)
- [ ] `h24@domarc.it` — mailbox di rientro autorizzazioni urgenti.
- [ ] `assistenza@domarc.it` — fallback `assistance_email` in auto_reply.
- [ ] `ai-fallback@domarc.it` — fallback `ai_classify_failsafe`.
- [ ] `noreply@domarc.it` — mittente standard auto-reply.
- [ ] `ticket@domarc.it` — reply-to per ticket.

---

## 3. Cross-progetto

### 3.1 Coordinamento `ai_assistant`
- [ ] Decidere se condividere PII redactor / provider tra Manager e Relay (package comune vs copia indipendente).
- [ ] Allineare prompt e tassonomia (urgenza, intent, classification) per coerenza UI.

### 3.2 Server GPU NVIDIA condiviso
- [ ] Una sola installazione DGX/Spark serve entrambi i progetti via endpoint OpenAI-compatible.
- [ ] Decidere chi orchestra (LB / quotas / monitoring).

### 3.3 Pipeline mail end-to-end
- [ ] Flusso reale: ESVA → Relay (filtri/instradamento) → Manager `ingestion_smtp` (threading + ticket).
- [ ] Definire chi fa cosa per evitare doppioni AI (es. l'IA classifica nel relay, il manager riceve la classifica già pronta in header `X-Domarc-AI-*`).

---

## 4. Backlog input richiesti all'utente

Da chiudere prima di andare in produzione `domarc.it`:

| # | Punto | Stato |
|---|---|---|
| A | Cambio password bootstrap `domarc2026` → passphrase 16+ char | ⏳ |
| B | Cancellare `settings.relay_api_key` plaintext | ⏳ |
| C | Verificare cert manager 4.41 (TLS_VERIFY=1) | ⏳ |
| D | Verifica mailbox `h24@`, `assistenza@`, `ai-fallback@`, `noreply@`, `ticket@domarc.it` | ⏳ in carico |
| E | codcli Domarc nel PG `solution` | ⏳ |
| F | NAS host per rsync backup off-site | ⏳ |
| G | Lista privacy bypass nominativi extra (emergerà da shadow_mode) | ⏳ |

---

## 5. Note di manutenzione di questo file

- **Quando aggiornare**: ogni volta che completi/inizi/blocchi una voce, ogni volta che emerge una nuova richiesta o limite tecnico durante una conversazione.
- **Dove aggiungere voci nuove**: nella sezione tematica corrispondente (Manager § / Relay § / Cross §). Se non rientra in nessuna, crea sotto-sezione nuova.
- **Cosa NON mettere qui**: bug-fix puntuali (vanno in commit), task con scope < 1 giornata (vanno in conversazione), discussioni di design (vanno in `docs/adr/`).
- **All'inizio di una sessione**: leggere prima di tutto questa roadmap e segnalare proattivamente all'utente le voci aperte rilevanti al task corrente.
