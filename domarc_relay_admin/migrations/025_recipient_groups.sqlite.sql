-- 025: Gruppi di destinatari + autodiscovery indirizzi mail
-- Pattern gemello a customer_groups, applicato a indirizzi email (non clienti).
-- Use case: raggruppare indirizzi tecnici per regole di routing/forward
--   (es. "Tecnici no fuori orario" → catchall h24).

BEGIN;

CREATE TABLE IF NOT EXISTS recipient_groups (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id   INTEGER NOT NULL DEFAULT 1 REFERENCES tenants(id),
    code        TEXT NOT NULL,                                -- es. "tecnici_no_fo"
    name        TEXT NOT NULL,                                -- es. "Tecnici no fuori orario"
    description TEXT,
    color       TEXT,                                         -- hex per badge UI
    enabled     INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    created_by  TEXT,
    updated_at  TEXT,
    UNIQUE (tenant_id, code)
);
CREATE INDEX IF NOT EXISTS idx_recipient_groups_tenant_enabled
    ON recipient_groups(tenant_id, enabled);

CREATE TABLE IF NOT EXISTS recipient_group_members (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id    INTEGER NOT NULL DEFAULT 1 REFERENCES tenants(id),
    group_id     INTEGER NOT NULL REFERENCES recipient_groups(id) ON DELETE CASCADE,
    email        TEXT NOT NULL,                               -- normalizzato lowercase
    note         TEXT,
    added_at     TEXT NOT NULL DEFAULT (datetime('now')),
    added_by     TEXT,
    UNIQUE (group_id, email)
);
CREATE INDEX IF NOT EXISTS idx_recipient_group_members_email
    ON recipient_group_members(tenant_id, email);
CREATE INDEX IF NOT EXISTS idx_recipient_group_members_group
    ON recipient_group_members(group_id);

-- Autodiscovery: log di tutti gli indirizzi destinatari visti
-- Popolato dal listener ad ogni mail processata. Serve per popolare i gruppi
-- senza copy/paste manuale: la UI mostrerà la lista con filtri + bulk action.
CREATE TABLE IF NOT EXISTS recipients (
    email          TEXT PRIMARY KEY,
    tenant_id      INTEGER NOT NULL DEFAULT 1 REFERENCES tenants(id),
    domain         TEXT,                                      -- per filtri rapidi
    first_seen_at  TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen_at   TEXT NOT NULL DEFAULT (datetime('now')),
    occurrences    INTEGER NOT NULL DEFAULT 1,
    last_subject   TEXT,                                      -- ultimo subject visto (debug)
    last_from      TEXT,                                      -- ultimo mittente
    note           TEXT,
    enabled        INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1))
);
CREATE INDEX IF NOT EXISTS idx_recipients_tenant_lastseen
    ON recipients(tenant_id, last_seen_at DESC);
CREATE INDEX IF NOT EXISTS idx_recipients_domain
    ON recipients(domain);

COMMIT;
