# Changelog

Tutte le modifiche rilevanti a questo progetto vengono documentate in questo file.
Il formato è basato su [Keep a Changelog](https://keepachangelog.com/it/1.1.0/).

## [Unreleased]

### Aggiunte — Pannello IA all'interno del form regola
- **Action `ai_classify`** ora selezionabile come card nelle azioni di:
  - [rule_form.html](templates/admin/rule_form.html) (regola orfana)
  - [rule_child_form.html](templates/admin/rule_child_form.html) (figlio di gruppo)
  Icona robot + label "IA classifica".
- **Pannello informativo dinamico** mostrato quando l'azione selezionata è `ai_classify` (sia in fase di nuova regola sia di edit). Contenuti:
  - Spiegazione di cosa farà l'IA (intent / urgenza / summary / suggested_action) e differenza shadow vs live.
  - **Job invocato** + **Binding attivo** (provider, model_id, version, traffic_split %) con link diretto al form binding (`/ai/models/<id>`).
  - **Avviso rosso** se manca un binding configurato per quel job → la regola scatterebbe in fail-safe.
  - **Stato globale**: AI master ON/OFF, SHADOW MODE / LIVE, costo oggi vs budget.
  - Campi action_map dedicati: `timeout_ms` (default 5000) e `tenant_id` (default 1).
  - **Mini-tabella** delle ultime 5 decisioni IA invocate da questa specifica regola (timestamp, intent, urgenza, summary truncato, stato), con link al dettaglio decisione.
- **Helper `_build_ai_form_context(rule_id)`** in [routes/rules.py](domarc_relay_admin/routes/rules.py): centralizza il caricamento di binding+providers+settings+decisioni-correlate per essere riusato dai 3 form (orphan/child/eventuale group). La correlazione regola↔decisione passa per `events.payload_metadata.ai_decision_id` (lookup su 72h).

### Verifiche
- `/rules/43` (regola TEST AI classify) ora mostra: "L'IA classificherà...", "Job invocato: classify_email", "Binding attivo Claude API (test) / claude-haiku-4-5 v1", "AI master ON / SHADOW", "Ultime decisioni IA" — tutti presenti nel rendering.

---

### Aggiunte — Visibilità "dove l'IA è abilitata"
- **Badge `🤖 IA` viola** nella tree view [/rules](http://192.168.4.41:8443/rules) accanto alle regole con `action='ai_classify'` o `action='ai_critical_check'`. Visibile sia per orfane che per figli di gruppo. Click sul badge → porta a `/ai/rules-overview`.
- **Pill action viola** per le action IA (`.dr-action-pill.ai_classify`, `.ai_critical_check`, `.ai_classify_shadow`, `.ai_classify_failsafe`) — distintivo a colpo d'occhio con icona robot.
- **Nuova vista** [/ai/rules-overview](http://192.168.4.41:8443/ai/rules-overview): tabella di tutte le regole IA (orfane e figli) con:
  - Priority + nome + scope (con link al form regola).
  - Match summary (to_domain, from_domain, subject_regex, in_service, contract_active).
  - **Job_code** richiesto (es. `classify_email` per `ai_classify`, `critical_classify` per `ai_critical_check`).
  - **Binding attivo** per quel job (provider + model + version + traffic_split). Se nessun binding configurato: warning rosso "⚠ NESSUN BINDING — la regola scatta ma fallisce → fail-safe".
  - **Statistiche 24h derivate** (correlazione `events.payload_metadata.ai_decision_id` ↔ `ai_decisions`):
    - Conteggio decisioni totali per regola.
    - Distribuzione: ✓N applied / ⊙N shadow / ✗N error / ⚡N fail-safe.
    - Costo cumulativo USD.
  - Header con stato globale: AI master ON/OFF, shadow mode attivo, costo oggi vs budget.
  - Legenda dei badge distribuzione in fondo.
- **Link da AI Dashboard** [/ai/](http://192.168.4.41:8443/ai/) → "Regole IA" — quinta pulsante della top-bar accanto a Provider/Routing/Decisioni/PII.

### Verifiche
- `/rules` ora mostra 4 marker IA (badge rule-ai-badge + icone robot nelle pill) sulla regola di test id=43.
- `/ai/rules-overview` mostra correttamente: regola "TEST AI classify" → job_code `classify_email` → binding "Claude API (test) / claude-haiku-4-5 v1".
- Tutte le 4 pagine (`/rules`, `/events`, `/ai`, `/ai/rules-overview`) rispondono 200 senza errori.

---

### Aggiunte — AI Assistant Fase 1.5: integrazione listener (action `ai_classify` end-to-end)
- **Listener `actions.py`** ([/opt/stormshield-smtp-relay/relay/actions.py](/opt/stormshield-smtp-relay/relay/actions.py)) — nuove funzioni:
  - `do_ai_classify(...)`: chiama l'admin via `POST /api/v1/relay/ai/classify` (timeout configurabile via `action_map.timeout_ms`, default 5000ms). Body: event redacted lato admin, customer_context. Risposta: classification + intent + urgenza + summary + suggested_action + decision_id. In **shadow mode** (default attuale) ritorna `action="ai_classify_shadow"` con metadata in `result.extra` per audit; non applica nessuna azione concreta. In live mode (F3+) eseguirà `suggested_action` (create_ticket / auto_reply / ignore / flag_only).
  - `_ai_failsafe(...)`: invocata su timeout / errore HTTP / provider in errore senza decision_id. Esegue `do_create_ticket(urgenza=ALTA, settore=assistenza, ai_unavailable=true, ai_unavailable_reason=...)` con flag in `payload_metadata` per audit. L'admin vede badge "IA fail-safe" rosso nell'events list.
- **Listener `pipeline.py`** ([/opt/stormshield-smtp-relay/relay/pipeline.py](/opt/stormshield-smtp-relay/relay/pipeline.py)):
  - Nuovo dispatch: `action_name in ("ai_classify", "ai_critical_check") → actions.do_ai_classify(...)`.
  - **Pre-generazione UUID dell'evento** (`pre_event_uuid = str(uuid.uuid4())` all'inizio di `process()`): permette ad `ai_decisions.event_uuid` di contenere l'UUID definitivo invece del placeholder `<placeholder>`. Tutte le 4 occorrenze di `event_uuid="<placeholder>"` sostituite con `event_uuid=pre_event_uuid`. `storage.insert_event(...)` chiamata finale riceve l'UUID già generato come parametro esplicito.
  - `keep_original_delivery=true` forzato su `ai_classify*`: in shadow mode la mail deve comunque essere recapitata al destinatario originale (default delivery aggiuntivo).
- **Admin `routes/api.py`**: già implementato `POST /api/v1/relay/ai/classify` orchestratore (PII redactor → router → provider → log decisione → ritorno). F1.
- **Admin `events_list.html`**: badge IA cliccabile sul payload_metadata dell'evento:
  - 🤖 **IA &lt;shadow&gt;** (azzurro) se evento ha `ai_decision_id` → link al dettaglio decisione.
  - 🤖 **IA fail-safe** (rosso) se `ai_unavailable=true`.
  - 🤖 **IA skip** (giallo) se `ai_skipped=true` (master switch off / budget esaurito / no binding).
  - Tooltip con classification + urgenza + costo USD.

### Correzioni
- **Bug critico double-encode `payload_metadata`** in [storage/sqlite_impl.py::insert_event](domarc_relay_admin/storage/sqlite_impl.py): il listener invia `payload_metadata` come stringa JSON già serializzata, l'admin faceva `json.dumps(...)` su una stringa producendo doppia serializzazione. Risultato: i campi `ai_decision_id`, `ai_classification`, `ai_pii_redactions` non erano accessibili lato admin. Fix: rilevamento `isinstance(str)` e passaggio diretto.
- **Bloccante systemd** `ProtectSystem=strict` impediva `pip install` dall'UI moduli (filesystem `/opt/.../.venv` read-only per il servizio). Fix: aggiunto `/opt/domarc-smtp-relay-admin/.venv` ai `ReadWritePaths` in [/etc/systemd/system/domarc-smtp-relay-admin.service](/etc/systemd/system/domarc-smtp-relay-admin.service). Le altre 9 protezioni di hardening (NoNewPrivileges, ProtectHome, ProtectKernelTunables/Modules/ControlGroups, RestrictAddressFamilies/Namespaces, LockPersonality, RestrictRealtime, SystemCallArchitectures) restano intatte. Codice sorgente `/opt/.../domarc_relay_admin/`, templates, migrations e `/etc/` restano read-only per il servizio.

### Verifiche end-to-end F1.5
- spaCy + `it_core_news_sm` installati dall'UI moduli post-fix systemd: log #3 `install success rc=0 1849ms`. PII redactor ora attiva NER nomi italiani: "Mario Rossi" → `[PER_*]`, "Milano" → `[LOC_*]`.
- Regola di test creata in `rules` (id=43, prio=5, `match_to_domain=datia.it`, `action=ai_classify`, `keep_original_delivery=true`).
- 3 mail di test inviate via swaks a `r.grandi@datia.it` con subject diversi:
  - **Mail #1** ("[ALERT] Backup failed on srv01"): listener match regola 43 → call admin `/api/v1/relay/ai/classify` → admin redatta 3 PII (telefono + Mario Rossi + Milano) → provider Claude test fail (API key di test non valida, `TEST_API_KEY` non impostata) → fail-safe path → ticket urgenza ALTA con `ai_unavailable=true`. event_uuid=`<placeholder>` (pre-fix).
  - **Mail #2** (post-fix UUID): event_uuid=UUID v4 valido in events_log + ai_decisions.
  - **Mail #3** (post-fix payload_metadata): event_uuid=`9e2a5eea...`, ai_decision_id=4, ai_pii_redactions=1 visibili in `events.payload_metadata` admin → badge **IA shadow** azzurro mostrato in `/events`.
- Flush listener → admin OK: 1 evento accepted per ciclo, niente duplicati.
- ai_decisions tabella amministrazione 4 record visibili in `/ai/decisions`: 1 mock test successful + 3 reali (di cui 1 con error API key, 2 fail-safe→ticket, ultimo OK shadow).

### Come testare ora dall'UI

1. AI Assistant → Provider → modifica "Claude API (test)" → cambia `api_key_env` da `TEST_API_KEY` a `ANTHROPIC_API_KEY`.
2. Settings → Chiavi API → Nuova chiave: `ANTHROPIC_API_KEY` con valore reale `sk-ant-api03-...`.
3. Crea regola in `/rules` con `action='ai_classify'` (o usa quella di test id=43 con `match_to_domain=datia.it`).
4. Invia mail di test: `swaks --to r.grandi@datia.it --from x@example.com --header "Subject: test" --body "..." --server 127.0.0.1:25`.
5. Vedi in tempo reale su `/ai/decisions` la decisione con classification, intent, urgenza, summary, latency, costo. Sull'`/events` il badge **🤖 IA shadow** sull'evento → click → `/ai/decisions/<id>` per il dettaglio.
6. Quando soddisfatto della qualità: Settings → `ai_shadow_mode=false` per andare live (F3).

---

### Aggiunte — UI gestione chiavi API e moduli (migration 013)
- **Migration 013** ([migrations/013_secrets_modules_ui.sqlite.sql](domarc_relay_admin/migrations/013_secrets_modules_ui.sqlite.sql)):
  - Tabella `api_keys` — cifratura Fernet del valore (BLOB), masked preview per UI, `env_var_name` (es. ANTHROPIC_API_KEY), `enabled`, `last_rotated_at`. UNIQUE(tenant_id, env_var_name).
  - Tabella `module_install_log` — audit log delle operazioni install/uninstall/upgrade su moduli Python (chi, quando, return code, durata, output stdout/stderr troncato a 100 righe).
- **secrets_manager.py** ([domarc_relay_admin/secrets_manager.py](domarc_relay_admin/secrets_manager.py)) — Fernet encryption con master key in `/var/lib/domarc-smtp-relay-admin/master.key` (auto-generata al primo avvio, permessi 600 owner=domarc-relay). Override del path via env var `DOMARC_RELAY_MASTER_KEY_PATH`. `load_secrets_into_env(storage)` decifra le chiavi enabled e le inietta in `os.environ` al boot dell'app, prima della registrazione dei provider.
- **module_manager.py** ([domarc_relay_admin/module_manager.py](domarc_relay_admin/module_manager.py)) — whitelist hard-coded di 5 moduli installabili dall'UI (`anthropic`, `spacy`, `spacy_it_core_news_sm`, `sentence_transformers`, `cryptography`). Esecuzione tramite `subprocess.run(pip install <package>)` con timeout 600s, output capture, audit log. Caso speciale `it_core_news_sm` con `python -m spacy download`. Rilevamento automatico dell'installato/non installato via `importlib.util.find_spec` + lettura `__version__`.
- **DAO** ([storage/sqlite_impl.py](domarc_relay_admin/storage/sqlite_impl.py)) esteso con: `list_api_keys`, `get_api_key`, `upsert_api_key`, `delete_api_key`, `toggle_api_key`, `list_module_install_log`, `insert_module_install_log`, `update_module_install_log`.
- **UI** ([routes/secrets_modules.py](domarc_relay_admin/routes/secrets_modules.py)) blueprint `/settings/*`:
  - `/settings/api-keys` — lista chiavi (mascherate "sk-ant-...abcd"), banner GDPR su Fernet + master.key.
  - `/settings/api-keys/new` e `/settings/api-keys/<id>` — form con valore in input password, edit senza modifica del valore (lascia vuoto per non ruotare), toggle enabled, descrizione.
  - `/settings/api-keys/<id>/toggle` — attiva/disattiva (carica/rimuove da env on-the-fly).
  - `/settings/api-keys/<id>/delete` — solo superadmin.
  - `/settings/modules` — catalogo whitelist con stato (installato/non), versione, dimensione stimata, dipendenze, "richiesto da" (feature). Bottone Installa/Aggiorna/Disinstalla solo per superadmin. Audit log ultime 20 operazioni in coda.
  - `/settings/modules/<code>/install`, `/uninstall` — POST richiede superadmin, esegue subprocess pip via module_manager con timeout, log dettagliato salvato.
  - `/settings/modules/log/<id>` — dettaglio operazione: stdout+stderr troncato, return_code, durata.
- **Voci menu Configurazione** (admin/superadmin only): "Chiavi API" (icona key) e "Moduli Python" (icona cube).
- **App boot wiring** ([app.py](domarc_relay_admin/app.py)): chiamata `load_secrets_into_env(storage)` dopo registrazione blueprint, in try/except per non bloccare l'avvio se cryptography manca o master.key invalida.

### Sicurezza
- Master key Fernet auto-generata permessi 600 owner del servizio. Se persa/sostituita, le chiavi cifrate diventano illegibili (fail-safe). **Backup obbligatorio** della master key insieme al DB.
- Whitelist moduli installabili: solo i 5 della lista hard-coded, NESSUN input arbitrario dall'utente.
- Pip install richiede ruolo `superadmin` (non admin).
- Eliminazione chiave o disinstallazione modulo opzionale richiedono `superadmin`.

### Verifiche end-to-end migration 013
- Migration applicata (schema v13). Backup pre-migration in [backups/admin.db.pre-secrets-ui-20260429-160108](backups/).
- Master key auto-generata al primo POST: `/var/lib/domarc-smtp-relay-admin/master.key` (44 byte, permessi 600 domarc-relay).
- Test cifratura: `sk-ant-api03-test12345abcdef9876` → cifrato (BLOB) + masked `sk-ant-a...9876` → decifrato roundtrip OK.
- Test pip install: cryptography → success rc=0 264ms (audit log #1).
- Pagine UI `/settings/api-keys`, `/settings/api-keys/new`, `/settings/modules` rispondono 200.
- Detection moduli: anthropic ✓ (0.97), cryptography ✓ (47.0), spacy ✗, sentence-transformers ✗.

---

### Aggiunte — Modulo AI Assistant Fase 1 (migration 012)
- **Migration 012** ([migrations/012_ai_assistant.sqlite.sql](domarc_relay_admin/migrations/012_ai_assistant.sqlite.sql) + parità Postgres):
  - 7 nuove tabelle: `ai_providers` (claude/openai_compat/local_http), `ai_jobs` (catalogo immutabile, 12 entries seed: classify_email, summarize_email, critical_classify, error_embedding, error_recovery_check, phishing_score, sentiment, language_detect, pii_ner, rule_proposal, attachment_classify, extract_codcli), `ai_job_bindings` (routing per job versionato con A/B traffic split), `ai_decisions` (log completo per audit/KPI/learning), `ai_error_clusters` (sostituirà error_aggregations rigide in F2), `ai_rule_proposals` (learning loop F3), `ai_pii_dictionary` (PII custom).
  - 4 nuovi setting: `ai_enabled` (default false, master switch), `ai_shadow_mode` (default true), `ai_daily_budget_usd` (default 50), `ai_fallback_forward_to` (default `ai-fallback@domarc.it`).
- **Package** [domarc_relay_admin/ai_assistant/](domarc_relay_admin/ai_assistant/):
  - `providers/base.py` — interfaccia astratta `AiProvider` (pattern factory identico a `customer_sources/`).
  - `providers/claude_provider.py` — implementazione Anthropic con prompt caching + structured output via tool_use + cost tracking ($/1M token per Haiku/Sonnet/Opus 4.x).
  - `providers/local_http_provider.py` — placeholder per DGX Spark (OpenAI-compatible client).
  - `providers/__init__.py` — factory `get_ai_provider(provider_id)` con import lazy.
  - `router.py` — `AiRouter` singleton con cache in-memory, lookup per job_code, traffic split A/B weighted random, render Jinja2 dei prompt template.
  - `pii_redactor.py` — pipeline 3 stadi (regex deterministici per IBAN/CF/P.IVA/telefono/email/IP/URL+token + signature stripping + spaCy NER italiano lazy + dizionario custom). Restituisce `RedactionResult` con conteggio per audit. spaCy opzionale (graceful fallback regex-only).
  - `prompts/classify_email.j2` — template Jinja2 con sezioni `## SYSTEM` e `## USER` per il job classify_email.
  - `decisions.py` — orchestratore `classify_email(...)` che redacta → router → provider → log decisione. Master switch + budget check + shadow mode + fallback su timeout/errore.
- **DAO** ([storage/sqlite_impl.py](domarc_relay_admin/storage/sqlite_impl.py)) esteso con: `list_ai_providers`/`upsert_ai_provider`/`delete_ai_provider`, `list_ai_jobs`, `list_ai_job_bindings`/`upsert_ai_job_binding` (versionato con flag `new_version=True` per disabilitare le precedenti), `insert_ai_decision`/`list_ai_decisions`/`get_ai_decision`/`sum_ai_decisions_cost_today`, `list_ai_pii_dictionary`/`upsert_ai_pii_dictionary_entry`. Helper `_decode_ai_decision` per parsing JSON.
- **API endpoints**:
  - `POST /api/v1/relay/ai/classify` — chiamato dal listener (header X-API-Key). Body: `{event, event_uuid, customer_context, tenant_id}`. Esegue pipeline classify_email completa.
  - `GET /api/v1/relay/ai-bindings/active` — listener cache i bindings attivi.
- **UI** ([routes/ai.py](domarc_relay_admin/routes/ai.py)) blueprint `/ai/*` con 7 viste:
  - `/ai/` — dashboard con KPI (decisioni 24h, latenza p50/p95, spesa oggi vs budget, top job, top intent), badge stato (AI master, shadow mode).
  - `/ai/providers` — CRUD provider (Claude/DGX) con bottone test connettività.
  - `/ai/providers/new`, `/ai/providers/<id>` — form provider con dropdown kind, env var della key, default model.
  - `/ai/models` — **routing per job**: tabella binding attivi, badge versione, traffic split %, edit inline.
  - `/ai/models/new`, `/ai/models/<id>` — form binding con dropdown job_code/provider, prompt template Jinja2 editor, fallback, traffic split, checkbox "Salva come nuova versione".
  - `/ai/decisions` — tabella decisioni con filtri (job_code, range ore), badge stato (shadow/applied/error).
  - `/ai/decisions/<id>` — dettaglio: job, provider, model, prompt hash, PII redactions count, intent/urgenza/summary, raw output JSON, latency, cost.
  - `/ai/pii-dictionary` — gestione voci PII custom.
- **Voce menu** "AI Assistant" (icona robot, viola `#7c3aed`) nel dropdown Configurazione, visibile solo per admin/superadmin.
- **Dipendenze nuove**: `anthropic` v0.97 (Anthropic SDK), `jinja2` (già presente). spaCy `it_core_news_sm` **opzionale** — graceful fallback se non installato.

### Verifiche end-to-end Fase 1
- Migration 012 applicata (schema v12). Backup pre-migration in [backups/admin.db.pre-ai-assistant-20260429-154347](backups/).
- 7 pagine UI rispondono 200 OK con login admin.
- Endpoint `GET /api/v1/relay/ai-bindings/active` funzionante con autenticazione X-API-Key.
- Test pipeline end-to-end con mock Claude provider:
  - Provider creato + binding `classify_email` configurato + setting `ai_enabled=true`.
  - Mail di test "[ERROR] Backup failed on srv01" con telefono `+39 333 1234567` nel corpo.
  - PII redactor ha rimosso 1 PII (telefono).
  - Mock Claude ha restituito: `intent=problema_tecnico, urgenza=ALTA, summary="Backup fallito su srv01", confidence=0.92, suggested_action=create_ticket`.
  - Decisione salvata in `ai_decisions` con `shadow_mode=true` (default), `applied=false`, costo $0.0007, latenza 420ms.
  - Dashboard `/ai/` mostra correttamente: AI master ON, SHADOW MODE, 1 decisione 24h, latenza p50/p95 = 420ms, top job `classify_email`, top intent `problema_tecnico`.

### Prossimi passi roadmap
- **F1.5**: action `do_ai_classify` lato listener (endpoint `/api/v1/relay/ai/classify` chiamato in modo sync con timeout 5s + fail-safe forward), dispatcher in `pipeline.py`, sync `ai-bindings` in cache locale.
- **F2**: error aggregator IA con embedding semantico (sentence-transformers MiniLM), sostituzione `error_aggregations` rigide.
- **F3**: rule proposer (learning loop) + uscita da shadow mode.
- **F4**: provider DGX Spark self-hosted in coesistenza con Claude API.

---

### Aggiunte — Privacy bypass list (migration 011)
- **Migration 011** ([migrations/011_privacy_bypass.sqlite.sql](domarc_relay_admin/migrations/011_privacy_bypass.sqlite.sql) + parità Postgres):
  - 4 colonne su `addresses_from` e `addresses_to`: `privacy_bypass`, `privacy_bypass_reason`, `privacy_bypass_at`, `privacy_bypass_by`. Indici parziali `WHERE privacy_bypass = 1` per lookup O(1).
  - Nuova tabella `privacy_bypass_domains` con `domain`, `scope ('from'|'to'|'both')`, `reason`, `enabled`. Permette bypass per intero dominio (es. tutto `@studio-legale.it`).
  - Nuova tabella `privacy_bypass_audit` per tracciamento GDPR di tutte le attivazioni/disattivazioni/cancellazioni (chi, quando, perché, target).
- **DAO** ([storage/sqlite_impl.py](domarc_relay_admin/storage/sqlite_impl.py)) esteso con: `set_address_privacy_bypass`, `list_addresses_privacy_bypass`, `list_privacy_bypass_domains`, `upsert_privacy_bypass_domain`, `delete_privacy_bypass_domain`, `list_privacy_bypass_active` (struttura completa per endpoint listener), `list_privacy_bypass_audit`. Tutte le mutazioni inseriscono automaticamente una riga in `privacy_bypass_audit`.
- **Endpoint listener** `GET /api/v1/relay/privacy-bypass/active` ([routes/api.py](domarc_relay_admin/routes/api.py)): payload con 4 chiavi `from`/`to`/`from_domains`/`to_domains` consumato dal sync periodico del listener.
- **UI** ([routes/privacy_bypass.py](domarc_relay_admin/routes/privacy_bypass.py) + [templates/admin/privacy_bypass.html](templates/admin/privacy_bypass.html)) con 4 tab: Mittenti, Destinatari, Domini, Audit log. Quick-add con autocomplete dagli `addresses_*` già rilevati. Eliminazione domini riservata a `superadmin`. Voce menu "Privacy bypass" colorata in rosso nel dropdown Anagrafiche.
- **Modifica chirurgica del listener** (Stormshield SMTP Relay):
  - [`/opt/stormshield-smtp-relay/relay/manager_client.py`](/opt/stormshield-smtp-relay/relay/manager_client.py): nuova dataclass `PrivacyBypassPayload` + metodo `fetch_active_privacy_bypass()` con fallback safe (404 → lista vuota) per backend pre-011.
  - [`/opt/stormshield-smtp-relay/relay/storage.py`](/opt/stormshield-smtp-relay/relay/storage.py): nuova tabella `privacy_bypass_cache` (4 record-types: from_email, to_email, from_domain, to_domain) + metodi `replace_privacy_bypass()` e `is_privacy_bypassed(from, to_list)` con check O(1) per email esatta + dominio.
  - [`/opt/stormshield-smtp-relay/relay/sync.py`](/opt/stormshield-smtp-relay/relay/sync.py): nuovo step di sync (cache invalidata atomicamente).
  - [`/opt/stormshield-smtp-relay/relay/pipeline.py`](/opt/stormshield-smtp-relay/relay/pipeline.py): pre-check **PRIMA** del rule engine, dopo `_resolve_customer`. Se from o uno qualsiasi dei to_addresses è in lista (per email esatta o dominio), la mail bypassa rule engine, aggregations, auto_reply e va in default delivery diretto. Audit log minimo: from, to, subject, message_id, size_bytes, action='privacy_bypass'. Niente body, niente codcli, niente chain regole, niente payload_metadata complesso.
- **Comportamento privacy garantito**: la lista è una *garanzia formale GDPR* — l'unica via di accesso al body in produzione è la quarantine (azione esplicita), e con la privacy bypass list il listener non può MAI quarantenare un indirizzo in lista.

### Verifiche end-to-end privacy bypass
- Migration 011 applicata (schema v11). Backup pre-migration in [backups/admin.db.pre-privacy-bypass-20260429-145056](backups/).
- UI `/privacy-bypass/` 200 OK, 4 tab funzionanti.
- POST `/privacy-bypass/domain/new` con `studio-legale.it` scope=both → endpoint listener ritorna correttamente `{"from_domains": ["studio-legale.it"], "to_domains": ["studio-legale.it"]}`.
- Sync listener (`stormshield-smtp-relay-scheduler`) registra in cache 2 entries; log `Sync privacy bypass OK: 2 entries (from_email=0, to_email=0, from_dom=1, to_dom=1)`.
- Test `is_privacy_bypassed()` su 4 scenari realistici tutti corretti: mail normale (False), mittente in dominio (True/from_domain), destinatario in dominio (True/to_domain), multi-destinatario con uno solo in lista (True/to_domain — logica "uno qualsiasi").



### Aggiunte
- **Rule Engine v2 — UI tree view e wizard (Fasi 3 e 4 della roadmap)**:
  - Refactor [templates/admin/rules_list.html](templates/admin/rules_list.html) → tree view collassabile con `.rule-group/.rule-child/.rule-orphan`, badge gruppo (numero figli, exclusive_match), badge azione, toggle JS, indicatori `continue_in_group`/`exit_group_continue`.
  - Tre form distinti: [rule_form.html](templates/admin/rule_form.html) (orfana — invariato), nuovo [rule_group_form.html](templates/admin/rule_group_form.html) (gruppo: match condivisi + defaults action_map ereditabili + lista figli inline), nuovo [rule_child_form.html](templates/admin/rule_child_form.html) (figlio: banner ereditarietà read-only + match aggiuntivi + action card + anteprima action_map effettiva con evidenziazione chiavi ereditate).
  - Pagina [rule_simulate.html](templates/admin/rule_simulate.html) con form evento+contesto e output flow path completo (gruppo→figlio→azioni eseguite, action_map effettiva inline) basato su `evaluate_v2`.
  - Pagina [rule_flatten_preview.html](templates/admin/rule_flatten_preview.html) — tabella delle regole flat che il listener riceverà, con colonna `_source_group_id` per audit.
  - Wizard [rule_groupable_wizard.html](templates/admin/rule_groupable_wizard.html) — cluster di orfane con match identici, etichetta auto-suggerita, promozione atomica via [rules.groupable_promote_view](domarc_relay_admin/routes/rules.py).
  - 8 nuovi endpoint blueprint in [routes/rules.py](domarc_relay_admin/routes/rules.py): `group_form_view`, `child_form_view`, `promote_view`, `flatten_preview_view`, `simulate_view`, `groupable_wizard_view`, `groupable_promote_view`. Ogni endpoint con decorator auth appropriato (admin per gruppi, superadmin per cluster promote).
  - CSS esteso in [static/css/admin.css](static/css/admin.css) con classi `.rule-tree`, `.rule-group/.rule-group-header/.rule-group-body`, `.rule-child`, `.rule-orphan`, `.rule-badge-group`, `.rule-badge-orphan`, `.rule-badge-children`, `.rule-inherit-badge`, `.rule-inherit-banner`, `.rule-flow-path`, `.rule-simulate-output`. Coerenti con palette `.dr-*` esistente.
  - Documentazione operatori [docs/rule_engine_v2.md](docs/rule_engine_v2.md) con concetti, workflow tipici (creare gruppo, promuovere orfana, wizard cluster, simulare evento, anteprima flatten), elenco validatori e warning, esempio end-to-end "Fuori orario contratto".
  - **Guida di funzionamento integrata** [docs/guida_funzionamento.md](docs/guida_funzionamento.md) — manuale operativo unificato in 8 sezioni (architettura · vita di un'email step-by-step con diagramma · modello regole gerarchico · UI · configurazioni correlate clienti/orari/template/route/tenant · 3 esempi end-to-end commentati · troubleshooting · riferimenti rapidi). Sostituisce il file da operatore unico, copre sia la gestione regole sia il flusso completo del listener (resolve_customer → rule engine → dispatch → default_delivery → aggregazioni → audit).

### Modifiche
- `rules.list_view` ora usa `list_rules_grouped()` e passa al template la struttura a tree (orphan / group+children).
- Il route `/rules` ha pulsanti dedicati per **Nuova regola**, **Nuovo gruppo**, **Anteprima flatten**, **Simulazione**, **Suggerisci gruppi** (sempre visibili), oltre a Toggle/Edit/Promuovi su ogni riga.

### Seed regole di base operative
- Nuovo script idempotente [scripts/seed_baseline_rules.py](scripts/seed_baseline_rules.py) — popola il tenant DOMARC (id=1) con 6 regole/gruppi canonici a partire dai dati reali del Customer Source (`https://manager-dev.domarc.it`):
  - **prio 50** — orfana `Errori critici (ERROR/FAILED/PROBLEMA)`: subject regex `(?i)\b(ERROR|FAILED|PROBLEMA|FAILURE)\b` → `create_ticket` urgenza ALTA + copia a `ticket@domarc.it`. Priorità bassa per intercettare PRIMA dei gruppi clienti.
  - **prio 200** — gruppo `Clienti contratto H24` (scope_ref=H24) → 2 figli (Auto-reply H24 + Ticket urgenza ALTA, settore `assistenza_h24`, also_deliver_to `h24@domarc.it`, auth_code_ttl 4h). Disabilitato (0 clienti H24 oggi) come scaffold pronto.
  - **prio 300** — gruppo `Clienti contratto EXT (fuori orario)` con `match_from_regex` esplicita sui domini reali EXT (3 domini puliti) + `match_in_service=0` → 2 figli (Auto-reply + Ticket NORMALE).
  - **prio 400** — gruppo `Clienti contratto STD (fuori orario)`: 321 clienti coperti via combinazione `match_known_customer=1`+`match_contract_active=1`+`match_in_service=0`+`scope_ref=STD` (la regex with 321 domini supererebbe il tetto 500 char del listener) → 2 figli (Auto-reply + Ticket NORMALE), auth_code_ttl 24h.
  - **prio 600** — orfana `Clienti senza contratto in archivio`: `match_known_customer=1`+`match_contract_active=0` → `auto_reply` con prefix `[Senza contratto]` + copia a `commerciale@domarc.it`. Disabilitata (0 clienti senza contratto oggi) come scaffold.
  - **prio 999** — orfana `Catch-all — log mail non gestite`: `match_to_regex='.*'` → `flag_only` + `keep_original_delivery=true`. Sempre attiva, registra ogni mail non gestita per audit.
- Tutti i gruppi hanno `match_to_domain="domarc.it"` come vincolo di sicurezza (V004) + `exclusive_match=True`.
- Nota di limite operativo (documentata in [docs/guida_funzionamento.md](docs/guida_funzionamento.md)): il listener legacy non valuta ancora i tristate `match_known_customer`/`match_contract_active`/`match_has_exception_today` né `scope_ref` per profilo (sector resta `None` lato `CustomerContext`). Le regole sono modello-corrette e attive nel payload `flatten`, ma alcune discriminazioni (es. STD vs EXT) saranno effettive a runtime solo quando il listener verrà esteso. Oggi STD/EXT si distinguono via `match_in_service` (calcolato dallo schedule profilo-specifico) e via la regex from_domain di EXT.

Simulazioni di verifica (4 scenari, evaluator v2):
- `[ERROR] backup failed` fuori orario → match orfana #31 → `create_ticket` ALTA, STOP.
- Cliente STD fuori orario, subject normale → chain `errori ✗ → EXT ✗ → STD ✓` → `auto_reply` + `create_ticket` NORMALE.
- Cliente STD in orario → chain `errori ✗ → EXT ✗ → STD ✗ → catch-all ✓` → `flag_only`.
- Dominio sconosciuto fuori orario → match STD (limite documentato: oggi il listener non legge `match_known_customer`).

### Verifiche end-to-end
- Smoke test pagine: 7/7 route UI rispondono 200 con login (`/rules`, `/rules/groups/new`, `/rules/groups/27`, `/rules/groups/27/children/28`, `/rules/flatten-preview`, `/rules/simulate`, `/rules/groupable-suggestions`).
- POST simulazione su tenant ACME (gruppo demo) con evento fuori-orario verso domarc.it: chain di 6 step (3 orfane viste come ✗ + gruppo #27 ✓ + figli #28/#29 ✓), 2 azioni eseguite con action_map ereditata visibile (`also_deliver_to`, `auth_code_ttl_hours`, `generate_auth_code`, `keep_original_delivery`, `reply_mode` ereditate dal padre).
- Flatten preview: 3 righe (TEST ACME orfana + 2 figli del gruppo flatted con `_source_group_id=27`), priority globale 99/510/520 invariata.

---

## Rule Engine v2 — backend (Fasi 1 e 2)

### Aggiunte
- **Rule Engine v2 — gerarchia padre/figlio (Fasi 1 e 2 della roadmap)**:
  - Migration `010_rule_groups.sqlite.sql` + parità Postgres `010_rule_groups.pg.sql`. Aggiunte alla tabella `rules` 6 colonne (`parent_id`, `is_group`, `group_label`, `exclusive_match`, `continue_in_group`, `exit_group_continue`) e 3 indici dedicati. Script down in `migrations/down/010_rule_groups_down.sqlite.sql`.
  - Nuovo package [domarc_relay_admin/rules/](domarc_relay_admin/rules/) con `action_map_schema` (whitelist `PARENT_ACTION_MAP_DEFAULTS` / `CHILD_ONLY_ACTION_MAP`), `inheritance.deep_merge_action_map`, `validators` (V001-V008, V_PRI_RANGE + warning W001-W005, W_PRI_GAP), `flatten.flatten_rules` (gerarchia → regole flat per il listener), `evaluator.evaluate_v2` (simulazione gerarchica) e `legacy_evaluator.evaluate_legacy` (replica della logica del listener `/opt/stormshield-smtp-relay/relay/rules.py`, usata nei test di parità).
  - DAO esteso ([storage/sqlite_impl.py](domarc_relay_admin/storage/sqlite_impl.py)) con `list_top_level_items`, `list_group_children`, `list_rules_grouped`, `flatten_rules_for_listener`, `get_rule_with_inheritance`, `promote_rule_to_group` (promozione idempotente), `detect_groupable_rules` (clustering greedy per il wizard di Fase 4). `upsert_rule` ora gestisce i 6 nuovi campi gerarchici.
  - Suite di test con **88 casi verdi**: [tests/test_inheritance.py](tests/test_inheritance.py), [tests/test_validators.py](tests/test_validators.py), [tests/test_flatten.py](tests/test_flatten.py), [tests/test_dao_groups.py](tests/test_dao_groups.py) e soprattutto [tests/test_rule_engine_parity.py](tests/test_rule_engine_parity.py) — il **gate di rilascio** della Fase 2: per ≥ 50 eventi sintetici asserisce che `evaluate_v2(top, children) == evaluate_legacy(flatten(top, children))`.
  - Script idempotente [scripts/seed_demo_group.py](scripts/seed_demo_group.py) che crea il gruppo dimostrativo "Fuori orario contratto" con 2 figli (auto_reply + create_ticket) ed action_map ereditata dal padre.

### Modifiche
- Endpoint `GET /api/v1/relay/rules/active` ([routes/api.py:107](domarc_relay_admin/routes/api.py)): ora chiama `flatten_rules_for_listener()` invece di `list_rules(only_enabled=True)`. Schema JSON retro-compatibile con il listener legacy (campi storici invariati); aggiunti metadata opzionali `_source_group_id` e `_source_child_id` per audit (ignorati dal listener legacy).
- `upsert_rule` accetta i nuovi campi gerarchici e applica il vincolo V004 (gruppi devono avere almeno un `match_*`).

### Ottimizzazioni
- 3 nuovi indici su `rules` (`parent_id`, `is_group`, `priority+enabled`) per accelerare le query di lista gerarchica e flatten.
- Cleanup pre-migration delle 4 regole duplicate "TEST ACME — isolamento" (id 11, 15, 19, 23) per partire da uno stato pulito; conservato solo l'id 7 canonico.

### Correzioni
- Adattata la semantica `exit_group_continue` al modello a priority globale unica: con priority lineari, "saltare i fratelli successivi del gruppo continuando ai top-level" non è esprimibile in flat statico. Ridefinito coerentemente: `exit_group_continue=True` su un figlio si comporta come `continue_in_group=True` MA forza l'ultimo figlio del gruppo a propagare `continue_after_match=True` ai top-level successivi (semantica documentata in `derive_continue_flag()` e validata dai test di parità).

### Note operative
- Backup automatico del DB pre-migration in `/opt/domarc-smtp-relay-admin/backups/admin.db.pre-rule-engine-v2-YYYYMMDD-HHMMSS`.
- Le 5 regole esistenti restano funzionanti come "orfane" (parent_id=NULL, is_group=0). Compatibilità retroattiva totale con il listener.
- Il listener `/opt/stormshield-smtp-relay/relay/rules.py` NON è stato modificato.

### Prossimi passi (roadmap Rule Engine v2)
- Fase 3: UI tree view collassabile, form distinti orfana/gruppo/figlio, simulazione inline, anteprima flatten.
- Fase 4: wizard "Suggerisci gruppi" basato su `detect_groupable_rules`, audit log delle promozioni, documentazione operatori.
