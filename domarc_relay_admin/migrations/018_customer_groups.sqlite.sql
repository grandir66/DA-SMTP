-- Migration 018 — Customer groups: raggruppamento clienti per applicazione regole.
--
-- Permette di definire gruppi di clienti (es. "Top customer", "Settore sanità",
-- "Pilot AI") e applicare regole all'INTERO gruppo invece che cliente per cliente.
--
-- - customer_groups: anagrafica gruppi (per tenant).
-- - customer_group_members: membership N:N (un cliente può stare in più gruppi).
-- - rules.match_customer_groups: CSV di group code; la regola matcha se il
--   cliente del messaggio appartiene ad almeno uno dei gruppi (OR).

CREATE TABLE IF NOT EXISTS customer_groups (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id   INTEGER NOT NULL DEFAULT 1 REFERENCES tenants(id),
    code        TEXT NOT NULL,                                -- es. "top_customer"
    name        TEXT NOT NULL,                                -- es. "Top Customer"
    description TEXT,
    color       TEXT,                                         -- hex per badge UI
    enabled     INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    created_by  TEXT,
    updated_at  TEXT,
    UNIQUE (tenant_id, code)
);

CREATE INDEX IF NOT EXISTS idx_customer_groups_tenant_enabled
    ON customer_groups(tenant_id, enabled);

CREATE TABLE IF NOT EXISTS customer_group_members (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       INTEGER NOT NULL DEFAULT 1 REFERENCES tenants(id),
    group_id        INTEGER NOT NULL REFERENCES customer_groups(id) ON DELETE CASCADE,
    codice_cliente  TEXT NOT NULL,
    added_at        TEXT NOT NULL DEFAULT (datetime('now')),
    added_by        TEXT,
    UNIQUE (group_id, codice_cliente)
);

CREATE INDEX IF NOT EXISTS idx_customer_group_members_codcli
    ON customer_group_members(tenant_id, codice_cliente);
CREATE INDEX IF NOT EXISTS idx_customer_group_members_group
    ON customer_group_members(group_id);

-- Match per gruppi nelle regole. CSV "top_customer,sanita".
ALTER TABLE rules ADD COLUMN match_customer_groups TEXT;
