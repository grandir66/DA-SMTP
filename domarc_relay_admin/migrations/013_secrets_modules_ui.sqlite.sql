-- Migration 013 — UI per gestione chiavi API e moduli da installare.
--
-- Tre obiettivi:
--   1. Persistere le API key (Anthropic, future) cifrate Fernet nel DB +
--      iniettarle in os.environ al boot dell'admin (così il pattern
--      `os.environ.get(api_key_env, '')` esistente continua a funzionare).
--   2. Catalogo whitelist di moduli Python (anthropic, spacy + modello it,
--      sentence-transformers ecc.) con stato installato/non + bottone
--      installa via subprocess.
--   3. Audit log delle operazioni install/uninstall (chi, quando, output).
--
-- Vincoli sicurezza:
--   - Solo whitelist hard-coded di pacchetti consentiti (NO arbitrary input).
--   - Solo superadmin può installare moduli.
--   - Master key Fernet auto-generata in /etc/domarc-smtp-relay-admin/master.key
--     al primo avvio. Se cancellata, le chiavi cifrate diventano illegibili
--     (fail-safe).

CREATE TABLE IF NOT EXISTS api_keys (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       INTEGER NOT NULL DEFAULT 1 REFERENCES tenants(id),
    name            TEXT NOT NULL,                            -- es. "Claude API production"
    env_var_name    TEXT NOT NULL,                            -- es. "ANTHROPIC_API_KEY"
    value_encrypted BLOB NOT NULL,                            -- valore cifrato Fernet
    masked_preview  TEXT,                                     -- es. "sk-ant-...abcd" per UI
    description     TEXT,
    enabled         INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    created_by      TEXT,
    last_rotated_at TEXT,
    UNIQUE(tenant_id, env_var_name)
);
CREATE INDEX IF NOT EXISTS idx_api_keys_enabled ON api_keys(enabled) WHERE enabled = 1;

CREATE TABLE IF NOT EXISTS module_install_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    module_code  TEXT NOT NULL,                                -- chiave del catalogo whitelist
    operation    TEXT NOT NULL CHECK (operation IN ('install', 'uninstall', 'upgrade', 'check')),
    status       TEXT NOT NULL CHECK (status IN ('running', 'success', 'failed', 'cancelled')),
    output       TEXT,                                         -- ultime righe stdout/stderr
    return_code  INTEGER,
    duration_ms  INTEGER,
    actor        TEXT,
    started_at   TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_module_log_started ON module_install_log(started_at DESC);
