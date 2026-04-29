-- Migration 001 SQLite — schema iniziale Domarc SMTP Relay Admin
--
-- SQLite WAL mode richiesto al runtime (impostato dall'app all'init).
-- Tipi: TEXT per stringhe + ISO datetime; INTEGER per id/numerici/bool;
-- TEXT JSON per JSONB equivalent.

-- ==============================================================
-- TENANTS (multi-tenant first-class — D7 del piano)
-- ==============================================================
CREATE TABLE IF NOT EXISTS tenants (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    codice                   TEXT NOT NULL UNIQUE,
    ragione_sociale          TEXT NOT NULL,
    description              TEXT,
    contract_active          INTEGER NOT NULL DEFAULT 1,
    enabled                  INTEGER NOT NULL DEFAULT 1,
    customer_source_config   TEXT,                              -- JSON
    created_at               TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at               TEXT NOT NULL DEFAULT (datetime('now')),
    created_by               TEXT
);
CREATE INDEX IF NOT EXISTS idx_tenants_enabled ON tenants(enabled);

-- Seed tenant default DOMARC (id=1)
INSERT OR IGNORE INTO tenants (id, codice, ragione_sociale, description, created_by)
VALUES (1, 'DOMARC', 'Domarc — default',
        'Tenant default. Per setup MSP, creare ulteriori tenant.',
        'system_seed');

-- ==============================================================
-- USERS (auth locale — D4 del piano)
-- ==============================================================
CREATE TABLE IF NOT EXISTS users (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    username                 TEXT NOT NULL UNIQUE,
    password_hash            TEXT NOT NULL,
    role                     TEXT NOT NULL DEFAULT 'viewer',    -- admin | operator | viewer
    full_name                TEXT,
    email                    TEXT,
    enabled                  INTEGER NOT NULL DEFAULT 1,
    created_at               TEXT NOT NULL DEFAULT (datetime('now')),
    last_login_at            TEXT
);
CREATE INDEX IF NOT EXISTS idx_users_enabled ON users(enabled);

-- Tabella di mappatura ruoli per tenant (multi-tenant role scoping)
CREATE TABLE IF NOT EXISTS user_tenant_roles (
    user_id                  INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    tenant_id                INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    role                     TEXT NOT NULL,                      -- admin | operator | viewer
    PRIMARY KEY (user_id, tenant_id)
);

CREATE TABLE IF NOT EXISTS auth_audit (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    username                 TEXT,
    ip_address               TEXT,
    user_agent               TEXT,
    outcome                  TEXT NOT NULL,                      -- success | failed | logout
    created_at               TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_auth_audit_when ON auth_audit(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_auth_audit_user ON auth_audit(username, created_at DESC);

-- ==============================================================
-- RULES
-- ==============================================================
CREATE TABLE IF NOT EXISTS rules (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id                INTEGER NOT NULL DEFAULT 1 REFERENCES tenants(id),
    name                     TEXT NOT NULL,
    scope_type               TEXT NOT NULL DEFAULT 'global',
    scope_ref                TEXT,
    priority                 INTEGER NOT NULL DEFAULT 100,
    enabled                  INTEGER NOT NULL DEFAULT 1,

    match_from_regex         TEXT,
    match_to_regex           TEXT,
    match_subject_regex      TEXT,
    match_body_regex         TEXT,
    match_to_domain          TEXT,
    match_at_hours           TEXT,
    match_in_service         INTEGER,                             -- NULL = indifferente
    match_contract_active    INTEGER,
    match_tag                TEXT,

    action                   TEXT NOT NULL,
    action_map               TEXT,                                -- JSON
    severity                 TEXT,
    continue_after_match     INTEGER NOT NULL DEFAULT 0,

    created_by               TEXT,
    created_at               TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at               TEXT NOT NULL DEFAULT (datetime('now')),

    UNIQUE (tenant_id, scope_type, scope_ref, priority)
);
CREATE INDEX IF NOT EXISTS idx_rules_tenant_priority ON rules(tenant_id, enabled, priority);

-- ==============================================================
-- REPLY TEMPLATES (auto-reply)
-- ==============================================================
CREATE TABLE IF NOT EXISTS reply_templates (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id                INTEGER NOT NULL DEFAULT 1 REFERENCES tenants(id),
    name                     TEXT NOT NULL,
    description              TEXT,
    subject_tmpl             TEXT NOT NULL,
    body_html_tmpl           TEXT NOT NULL,
    body_text_tmpl           TEXT,
    reply_from_name          TEXT,
    reply_from_email         TEXT,
    attachment_paths         TEXT,                                -- JSON
    enabled                  INTEGER NOT NULL DEFAULT 1,
    created_at               TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at               TEXT NOT NULL DEFAULT (datetime('now')),
    updated_by               TEXT,
    UNIQUE (tenant_id, name)
);

-- ==============================================================
-- SERVICE HOURS PROFILES (modelli, condivisibili tra tenant)
-- ==============================================================
CREATE TABLE IF NOT EXISTS service_hours_profiles (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id                INTEGER REFERENCES tenants(id),     -- NULL = global builtin
    name                     TEXT NOT NULL,
    description              TEXT,
    schedule                 TEXT NOT NULL,                       -- JSON
    holidays                 TEXT NOT NULL DEFAULT '[]',          -- JSON
    holidays_auto            INTEGER NOT NULL DEFAULT 0,
    timezone                 TEXT NOT NULL DEFAULT 'Europe/Rome',
    is_builtin               INTEGER NOT NULL DEFAULT 0,
    enabled                  INTEGER NOT NULL DEFAULT 1,
    created_at               TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at               TEXT NOT NULL DEFAULT (datetime('now')),
    updated_by               TEXT,
    UNIQUE (tenant_id, name)
);

-- Seed 3 profili built-in (tenant_id=NULL = globali, condivisi)
INSERT OR IGNORE INTO service_hours_profiles
    (id, tenant_id, name, description, schedule, holidays, holidays_auto, is_builtin, updated_by)
VALUES
(1, NULL, 'standard', 'Lun-ven 8-13/14-18, festività italiane',
 '{"mon":[["08:00","13:00"],["14:00","18:00"]],"tue":[["08:00","13:00"],["14:00","18:00"]],"wed":[["08:00","13:00"],["14:00","18:00"]],"thu":[["08:00","13:00"],["14:00","18:00"]],"fri":[["08:00","13:00"],["14:00","18:00"]],"sat":[],"sun":[]}',
 '[]', 1, 1, 'system_seed'),
(2, NULL, 'extended', 'Lun-ven 8-20 + sab 9-13, festività italiane',
 '{"mon":[["08:00","20:00"]],"tue":[["08:00","20:00"]],"wed":[["08:00","20:00"]],"thu":[["08:00","20:00"]],"fri":[["08:00","20:00"]],"sat":[["09:00","13:00"]],"sun":[]}',
 '[]', 1, 1, 'system_seed'),
(3, NULL, 'h24', '24/7 nessuna chiusura',
 '{"mon":[["00:00","24:00"]],"tue":[["00:00","24:00"]],"wed":[["00:00","24:00"]],"thu":[["00:00","24:00"]],"fri":[["00:00","24:00"]],"sat":[["00:00","24:00"]],"sun":[["00:00","24:00"]]}',
 '[]', 0, 1, 'system_seed');

-- ==============================================================
-- SERVICE HOURS PER CLIENTE
-- ==============================================================
CREATE TABLE IF NOT EXISTS service_hours (
    tenant_id                INTEGER NOT NULL DEFAULT 1 REFERENCES tenants(id),
    codice_cliente           TEXT NOT NULL,
    profile                  TEXT DEFAULT 'custom',
    profile_id               INTEGER REFERENCES service_hours_profiles(id) ON DELETE SET NULL,
    timezone                 TEXT NOT NULL DEFAULT 'Europe/Rome',
    schedule                 TEXT NOT NULL,                       -- JSON
    holidays                 TEXT,                                -- JSON
    schedule_exceptions      TEXT DEFAULT '[]',                   -- JSON
    ah_key                   TEXT,
    notes                    TEXT,
    enabled                  INTEGER NOT NULL DEFAULT 1,
    created_at               TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at               TEXT NOT NULL DEFAULT (datetime('now')),
    updated_by               TEXT,
    PRIMARY KEY (tenant_id, codice_cliente)
);

-- ==============================================================
-- AUTHORIZATION CODES
-- ==============================================================
CREATE TABLE IF NOT EXISTS authorization_codes (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id                INTEGER NOT NULL DEFAULT 1 REFERENCES tenants(id),
    code                     TEXT NOT NULL UNIQUE,
    codice_cliente           TEXT,
    rule_id                  INTEGER REFERENCES rules(id) ON DELETE SET NULL,
    generated_at             TEXT NOT NULL DEFAULT (datetime('now')),
    valid_until              TEXT NOT NULL,
    used_at                  TEXT,
    used_by                  TEXT,
    note                     TEXT
);
CREATE INDEX IF NOT EXISTS idx_authcodes_tenant_valid ON authorization_codes(tenant_id, valid_until) WHERE used_at IS NULL;

-- ==============================================================
-- EVENTS (replicati dal listener relay)
-- ==============================================================
CREATE TABLE IF NOT EXISTS events (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id                INTEGER NOT NULL DEFAULT 1 REFERENCES tenants(id),
    relay_event_uuid         TEXT NOT NULL UNIQUE,
    received_at              TEXT NOT NULL,
    ingested_at              TEXT NOT NULL DEFAULT (datetime('now')),
    from_address             TEXT,
    to_address               TEXT,
    subject                  TEXT,
    message_id               TEXT,
    codice_cliente           TEXT,
    action_taken             TEXT,
    rule_id                  INTEGER REFERENCES rules(id) ON DELETE SET NULL,
    ticket_id                TEXT,
    payload_metadata         TEXT,                                -- JSON
    body_text                TEXT,
    body_html                TEXT,
    body_expires_at          TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_tenant_received ON events(tenant_id, received_at DESC);
CREATE INDEX IF NOT EXISTS idx_events_received ON events(received_at DESC);
CREATE INDEX IF NOT EXISTS idx_events_body_expires ON events(body_expires_at) WHERE body_expires_at IS NOT NULL;

-- ==============================================================
-- ERROR AGGREGATIONS + OCCURRENCES
-- ==============================================================
CREATE TABLE IF NOT EXISTS error_aggregations (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id                INTEGER NOT NULL DEFAULT 1 REFERENCES tenants(id),
    name                     TEXT NOT NULL,
    description              TEXT,
    match_from_regex         TEXT,
    match_subject_regex      TEXT,
    match_body_regex         TEXT,
    fingerprint_template     TEXT NOT NULL DEFAULT '${from}|${subject_normalized}',
    threshold                INTEGER NOT NULL DEFAULT 2,
    consecutive_only         INTEGER NOT NULL DEFAULT 0,
    window_hours             INTEGER NOT NULL DEFAULT 24,
    reset_subject_regex      TEXT,
    reset_from_regex         TEXT,
    ticket_settore           TEXT,
    ticket_urgenza           TEXT,
    ticket_codice_cliente    TEXT,
    enabled                  INTEGER NOT NULL DEFAULT 1,
    priority                 INTEGER NOT NULL DEFAULT 100,
    created_at               TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at               TEXT NOT NULL DEFAULT (datetime('now')),
    created_by               TEXT,
    UNIQUE (tenant_id, name)
);

CREATE TABLE IF NOT EXISTS error_occurrences (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id                INTEGER NOT NULL DEFAULT 1 REFERENCES tenants(id),
    aggregation_id           INTEGER NOT NULL REFERENCES error_aggregations(id) ON DELETE CASCADE,
    fingerprint              TEXT NOT NULL,
    current_count            INTEGER NOT NULL DEFAULT 1,
    first_seen               TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen                TEXT NOT NULL DEFAULT (datetime('now')),
    sample_from              TEXT,
    sample_subject           TEXT,
    sample_received_at       TEXT,
    sample_message_id        TEXT,
    ticket_opened_at         TEXT,
    ticket_id                TEXT,
    last_reset_at            TEXT,
    total_resets             INTEGER NOT NULL DEFAULT 0,
    UNIQUE (aggregation_id, fingerprint)
);

-- ==============================================================
-- SETTINGS GLOBALI (non scopati per tenant)
-- ==============================================================
CREATE TABLE IF NOT EXISTS settings (
    key                      TEXT PRIMARY KEY,
    value                    TEXT,
    description              TEXT,
    updated_at               TEXT NOT NULL DEFAULT (datetime('now'))
);

INSERT OR IGNORE INTO settings (key, value, description) VALUES
    ('body_retention_hours', '6', 'Quante ore conservare il body delle email per re-eval/debug. 0 = mai. Default 6.'),
    ('body_max_size_kb', '256', 'Limite dimensione body memorizzato (KB). Default 256.'),
    ('schema_version', '1', 'Versione corrente schema (auto-aggiornata dal migration runner).');
