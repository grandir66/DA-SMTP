-- Migration 012 — modulo ai_assistant: routing per job, provider pluggabili,
-- log decisioni, error clustering semantico, learning loop per regole statiche.
--
-- Quattro pilastri:
--   1. ai_providers       — anagrafica provider (Claude API, DGX Spark locale)
--   2. ai_jobs            — catalogo immutabile dei tipi di lavoro AI
--   3. ai_job_bindings    — routing: job_code → provider+model+config (versionato, A/B)
--   4. ai_decisions       — log strutturato di ogni inferenza (audit + costi + accuracy)
--
-- Tre sistemi avanzati:
--   - ai_error_clusters   — dedup semantica errori (sostituisce error_aggregations rigide)
--   - ai_rule_proposals   — learning loop: dopo N decisioni simili propone regola statica
--   - ai_pii_dictionary   — PII custom da redarre (oltre regex e spaCy NER)
--
-- Default operativo: shadow_mode = ON. Le decisioni vengono loggate ma non applicate
-- finché l'operatore non disattiva esplicitamente il flag in settings.

-- =============================================================
-- 1. PROVIDERS — anagrafica provider IA
-- =============================================================
CREATE TABLE IF NOT EXISTS ai_providers (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       INTEGER NOT NULL DEFAULT 1 REFERENCES tenants(id),
    name            TEXT NOT NULL,
    kind            TEXT NOT NULL CHECK (kind IN ('claude', 'openai_compat', 'local_http')),
    endpoint        TEXT,                                      -- URL base (vuoto per Claude default)
    api_key_env     TEXT,                                      -- nome env var per la chiave (es. ANTHROPIC_API_KEY)
    default_model   TEXT,                                      -- es. claude-haiku-4-5
    enabled         INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    notes           TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    created_by      TEXT,
    UNIQUE(tenant_id, name)
);

-- =============================================================
-- 2. JOBS — catalogo immutabile dei tipi di lavoro IA
-- =============================================================
CREATE TABLE IF NOT EXISTS ai_jobs (
    job_code                  TEXT PRIMARY KEY,
    description               TEXT NOT NULL,
    modality                  TEXT NOT NULL CHECK (modality IN ('sync', 'async', 'batch')),
    default_timeout_ms        INTEGER NOT NULL DEFAULT 5000,
    can_redact_pii            INTEGER NOT NULL DEFAULT 1 CHECK (can_redact_pii IN (0, 1)),
    requires_structured_output INTEGER NOT NULL DEFAULT 1 CHECK (requires_structured_output IN (0, 1))
);

INSERT OR IGNORE INTO ai_jobs (job_code, description, modality, default_timeout_ms, requires_structured_output) VALUES
    ('classify_email',       'Classifica intent/urgenza/summary di una mail',                  'sync',  5000, 1),
    ('summarize_email',      'Sintesi 2-3 righe della mail per ticket',                         'sync',  3000, 1),
    ('extract_codcli',       'Estrae codice cliente dal corpo se presente',                     'sync',  2000, 1),
    ('error_embedding',      'Embedding semantico di subject+body (per clustering)',            'async', 5000, 0),
    ('error_recovery_check', 'Riconosce semanticamente messaggi "ok/recovered/resolved"',        'sync',  2000, 1),
    ('phishing_score',       'Score sospetto phishing/spam contestuale',                        'sync',  3000, 1),
    ('sentiment',            'Tono aggressivo/urgente per escalation',                          'sync',  2000, 1),
    ('language_detect',      'Rilevamento lingua mail (it/en/de/...)',                          'sync',  1500, 1),
    ('pii_ner',              'NER per PII redactor (assistente)',                               'sync',  2000, 1),
    ('rule_proposal',        'Genera proposta regola statica da N decisioni simili',            'async', 10000, 1),
    ('critical_classify',    'Classificazione "critico assoluto" — pre-rule, fail-safe attivo', 'sync',  3000, 1),
    ('attachment_classify',  'Classifica allegati (log, screenshot, fattura, ecc.)',            'async', 5000, 1);

-- =============================================================
-- 3. JOB BINDINGS — routing per job (versionato, A/B traffic split)
-- =============================================================
CREATE TABLE IF NOT EXISTS ai_job_bindings (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id                INTEGER NOT NULL DEFAULT 1 REFERENCES tenants(id),
    job_code                 TEXT NOT NULL REFERENCES ai_jobs(job_code),
    provider_id              INTEGER NOT NULL REFERENCES ai_providers(id),
    model_id                 TEXT NOT NULL,
    system_prompt_template   TEXT,                              -- Jinja2 template
    user_prompt_template     TEXT,                              -- Jinja2 template
    temperature              REAL NOT NULL DEFAULT 0.0,
    max_tokens               INTEGER NOT NULL DEFAULT 1024,
    timeout_ms               INTEGER,                           -- override del job default
    fallback_provider_id     INTEGER REFERENCES ai_providers(id),
    fallback_model_id        TEXT,
    traffic_split            INTEGER NOT NULL DEFAULT 100 CHECK (traffic_split BETWEEN 0 AND 100),
    enabled                  INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0, 1)),
    version                  INTEGER NOT NULL DEFAULT 1,
    notes                    TEXT,
    created_at               TEXT NOT NULL DEFAULT (datetime('now')),
    created_by               TEXT,
    UNIQUE(tenant_id, job_code, version)
);

CREATE INDEX IF NOT EXISTS idx_ai_bindings_active ON ai_job_bindings(tenant_id, job_code, enabled) WHERE enabled = 1;

-- =============================================================
-- 4. DECISIONS — log inferenze (per audit, KPI, learning loop)
-- =============================================================
CREATE TABLE IF NOT EXISTS ai_decisions (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id                INTEGER NOT NULL DEFAULT 1 REFERENCES tenants(id),
    event_uuid               TEXT,                              -- FK lasciata implicita (events può essere su altro tenant/db)
    job_code                 TEXT NOT NULL REFERENCES ai_jobs(job_code),
    binding_id               INTEGER REFERENCES ai_job_bindings(id),
    provider                 TEXT,
    model                    TEXT,
    prompt_hash              TEXT,                              -- SHA256 del prompt finale (audit)
    pii_redactions_count     INTEGER NOT NULL DEFAULT 0,
    classification           TEXT,                              -- es. "problema_tecnico"
    urgenza_proposta         TEXT,                              -- BASSA/NORMALE/ALTA/CRITICA
    intent                   TEXT,
    summary                  TEXT,
    suggested_actions_json   TEXT,                              -- {"forward_to":..., "open_ticket":true, ...}
    raw_output_json          TEXT,
    confidence               REAL,                              -- 0.0-1.0 (se modello restituisce)
    latency_ms               INTEGER,
    input_tokens             INTEGER,
    output_tokens            INTEGER,
    cost_usd                 REAL,
    applied                  INTEGER NOT NULL DEFAULT 0 CHECK (applied IN (0, 1)),
    shadow_mode              INTEGER NOT NULL DEFAULT 1 CHECK (shadow_mode IN (0, 1)),
    error                    TEXT,                              -- se errore nella chiamata IA
    fallback_used            INTEGER NOT NULL DEFAULT 0,
    applied_by               TEXT,
    created_at               TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_ai_decisions_event ON ai_decisions(event_uuid);
CREATE INDEX IF NOT EXISTS idx_ai_decisions_job_at ON ai_decisions(job_code, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_ai_decisions_tenant_at ON ai_decisions(tenant_id, created_at DESC);

-- =============================================================
-- 5. ERROR CLUSTERS — dedup semantica errori
-- =============================================================
CREATE TABLE IF NOT EXISTS ai_error_clusters (
    id                            INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id                     INTEGER NOT NULL DEFAULT 1 REFERENCES tenants(id),
    fingerprint_embedding         BLOB,                         -- vettore numpy float32 (384 dim per MiniLM)
    representative_subject        TEXT,
    representative_body_excerpt   TEXT,
    count                         INTEGER NOT NULL DEFAULT 1,
    first_seen                    TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen                     TEXT NOT NULL DEFAULT (datetime('now')),
    recovery_seen_at              TEXT,
    manual_threshold              INTEGER NOT NULL DEFAULT 5,   -- modificabile da UI per cluster
    manual_recovery_window_min    INTEGER NOT NULL DEFAULT 60,  -- modificabile da UI per cluster
    state                         TEXT NOT NULL DEFAULT 'accumulating' CHECK (state IN ('accumulating', 'ticket_opened', 'recovered', 'archived')),
    ticket_id                     TEXT,
    notes                         TEXT
);
CREATE INDEX IF NOT EXISTS idx_ai_clusters_state ON ai_error_clusters(state, last_seen DESC);

-- =============================================================
-- 6. RULE PROPOSALS — learning loop verso regole statiche
-- =============================================================
CREATE TABLE IF NOT EXISTS ai_rule_proposals (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id                INTEGER NOT NULL DEFAULT 1 REFERENCES tenants(id),
    suggested_match_subject  TEXT,
    suggested_match_from     TEXT,
    suggested_match_to       TEXT,
    suggested_match_in_service INTEGER,
    suggested_match_contract_active INTEGER,
    suggested_action         TEXT,
    suggested_action_map_json TEXT,
    confidence               REAL NOT NULL DEFAULT 0.0,         -- 0.0-1.0
    evidence_decision_ids    TEXT,                              -- CSV degli id decisioni che hanno generato la proposta
    sample_subjects          TEXT,                              -- CSV (max 5) per UI evidence
    state                    TEXT NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'accepted', 'rejected', 'archived')),
    accepted_rule_id         INTEGER REFERENCES rules(id) ON DELETE SET NULL,
    reviewer                 TEXT,
    review_at                TEXT,
    review_notes             TEXT,
    created_at               TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_ai_proposals_state ON ai_rule_proposals(state, created_at DESC);

-- =============================================================
-- 7. PII DICTIONARY — entry custom per il redactor
-- =============================================================
CREATE TABLE IF NOT EXISTS ai_pii_dictionary (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id                INTEGER NOT NULL DEFAULT 1 REFERENCES tenants(id),
    kind                     TEXT NOT NULL CHECK (kind IN ('person', 'org', 'product', 'other')),
    value                    TEXT NOT NULL,
    replacement              TEXT NOT NULL,                     -- es. "[PER_1]" / "[ORG_1]"
    source                   TEXT NOT NULL DEFAULT 'manual' CHECK (source IN ('manual', 'auto_ner')),
    occurrences              INTEGER NOT NULL DEFAULT 0,
    last_seen_at             TEXT,
    created_at               TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(tenant_id, value)
);
CREATE INDEX IF NOT EXISTS idx_ai_pii_kind ON ai_pii_dictionary(tenant_id, kind);

-- =============================================================
-- Settings di default per il modulo IA
-- =============================================================
INSERT OR IGNORE INTO settings (key, value, description) VALUES
    ('ai_shadow_mode', 'true',
     'Quando true (default): le decisioni IA vengono loggate ma NON applicate. Switch atomico per produzione.'),
    ('ai_daily_budget_usd', '50',
     'Budget giornaliero in USD per chiamate IA. Quando raggiunto, fail-safe path attivo.'),
    ('ai_fallback_forward_to', 'ai-fallback@domarc.it',
     'Indirizzo a cui inoltrare le mail quando IA è down/timeout (fail-safe).'),
    ('ai_enabled', 'false',
     'Master switch del modulo IA. Quando false, le action ai_* vengono saltate (fail-safe).');
