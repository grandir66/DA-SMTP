-- Migration 011 — privacy bypass list per addresses_from / addresses_to + domini.
--
-- Indirizzi (mittenti o destinatari) e domini interi che NON devono essere
-- elaborati dal rule engine, dalle aggregations e dall'auto-reply per ragioni
-- di privacy (GDPR) o operative (es. avvocati, ufficio risorse umane,
-- comunicazioni riservate alla direzione).
--
-- Comportamento atteso lato listener (modifica pipeline.py):
--   1. Pre-check PRIMA del rule engine: se from_address oppure uno qualsiasi
--      dei to_address è nella privacy bypass list (per email esatta o per
--      dominio), la mail viene direttamente inoltrata (default delivery).
--   2. NESSUNA azione di rule engine viene eseguita (no auto_reply,
--      no create_ticket, no forward, no quarantine, no aggregations).
--   3. NESSUN body memorizzato (di default già non lo è, ma garanzia
--      formale).
--   4. Audit log minimo in events_log: timestamp, from, to, subject,
--      message_id, action='privacy_bypass'. Niente codcli, niente chain,
--      niente payload_metadata complesso.

-- Flag su singoli indirizzi noti
ALTER TABLE addresses_from ADD COLUMN privacy_bypass INTEGER NOT NULL DEFAULT 0 CHECK (privacy_bypass IN (0, 1));
ALTER TABLE addresses_from ADD COLUMN privacy_bypass_reason TEXT;
ALTER TABLE addresses_from ADD COLUMN privacy_bypass_at TEXT;
ALTER TABLE addresses_from ADD COLUMN privacy_bypass_by TEXT;

ALTER TABLE addresses_to ADD COLUMN privacy_bypass INTEGER NOT NULL DEFAULT 0 CHECK (privacy_bypass IN (0, 1));
ALTER TABLE addresses_to ADD COLUMN privacy_bypass_reason TEXT;
ALTER TABLE addresses_to ADD COLUMN privacy_bypass_at TEXT;
ALTER TABLE addresses_to ADD COLUMN privacy_bypass_by TEXT;

CREATE INDEX IF NOT EXISTS idx_addresses_from_privacy ON addresses_from(privacy_bypass) WHERE privacy_bypass = 1;
CREATE INDEX IF NOT EXISTS idx_addresses_to_privacy ON addresses_to(privacy_bypass) WHERE privacy_bypass = 1;

-- Tabella domini in privacy bypass (granularità più ampia)
-- scope:
--   'from'  → bypass se il MITTENTE proviene da questo dominio
--   'to'    → bypass se uno qualsiasi dei DESTINATARI è di questo dominio
--   'both'  → bypass se from O to combaciano col dominio
CREATE TABLE IF NOT EXISTS privacy_bypass_domains (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id    INTEGER NOT NULL DEFAULT 1 REFERENCES tenants(id),
    domain       TEXT NOT NULL,
    scope        TEXT NOT NULL DEFAULT 'both' CHECK (scope IN ('from', 'to', 'both')),
    reason       TEXT,
    enabled      INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    created_by   TEXT,
    UNIQUE(tenant_id, domain, scope)
);
CREATE INDEX IF NOT EXISTS idx_privacy_bypass_domains_tenant ON privacy_bypass_domains(tenant_id, enabled) WHERE enabled = 1;

-- Audit log delle attivazioni/disattivazioni privacy bypass
-- (chi ha attivato cosa quando perché). Letture solo da admin/superadmin.
CREATE TABLE IF NOT EXISTS privacy_bypass_audit (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id    INTEGER NOT NULL DEFAULT 1 REFERENCES tenants(id),
    target_kind  TEXT NOT NULL CHECK (target_kind IN ('address_from', 'address_to', 'domain')),
    target_value TEXT NOT NULL,
    action       TEXT NOT NULL CHECK (action IN ('enable', 'disable', 'create', 'delete')),
    reason       TEXT,
    actor        TEXT,
    at           TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_privacy_audit_at ON privacy_bypass_audit(at DESC);
