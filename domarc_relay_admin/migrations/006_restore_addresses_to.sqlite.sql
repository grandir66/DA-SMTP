-- Migration 006 — ripristino `addresses_to` (anagrafica destinatari).
--
-- La 005 aveva droppato la tabella, ma serve comunque per visibilità degli
-- indirizzi destinatari intercettati dal listener (utile per riconoscere
-- alias non mappati e suggerire nuove routes).

CREATE TABLE IF NOT EXISTS addresses_to (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id         INTEGER NOT NULL DEFAULT 1 REFERENCES tenants(id),
    email_address     TEXT NOT NULL,
    local_part        TEXT,
    domain            TEXT,
    codice_cliente    TEXT,
    seen_count        INTEGER NOT NULL DEFAULT 1,
    first_seen_at     TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen_at      TEXT NOT NULL DEFAULT (datetime('now')),
    notes             TEXT,
    UNIQUE (tenant_id, email_address)
);

CREATE INDEX IF NOT EXISTS idx_addresses_to_tenant ON addresses_to(tenant_id, last_seen_at DESC);
CREATE INDEX IF NOT EXISTS idx_addresses_to_domain ON addresses_to(tenant_id, domain);
