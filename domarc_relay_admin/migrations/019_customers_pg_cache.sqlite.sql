-- Migration 019 — Customer source PostgreSQL: cache locale clienti.
--
-- Permette di staccarsi dal manager Stormshield: un sync periodico
-- query direttamente i 2 DB PG (`solution` + `stormshield`) e popola
-- la cache locale. Il customer source runtime legge SOLO da qui — zero
-- dipendenza da PG durante il match.
--
-- Se il sync fallisce (PG down, network), l'ultimo snapshot rimane usabile.

CREATE TABLE IF NOT EXISTS customers_pg_cache (
    codcli              TEXT PRIMARY KEY,
    ragione_sociale     TEXT,
    domains_json        TEXT NOT NULL DEFAULT '[]',
    aliases_json        TEXT NOT NULL DEFAULT '[]',
    contract_active     INTEGER NOT NULL DEFAULT 1,
    tipologia_servizio  TEXT,                          -- code (STD/EXT/H24/NO)
    service_hours_json  TEXT,                          -- {profile, timezone, schedule, holidays}
    contract_expiry     TEXT,                          -- ISO date
    timezone            TEXT DEFAULT 'Europe/Rome',
    raw_json            TEXT,                          -- payload completo per audit
    last_synced_at      TEXT NOT NULL DEFAULT (datetime('now')),
    tenant_id           INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_customers_pg_cache_tenant
    ON customers_pg_cache(tenant_id);
CREATE INDEX IF NOT EXISTS idx_customers_pg_cache_synced
    ON customers_pg_cache(last_synced_at DESC);

-- Audit log dei sync runs
CREATE TABLE IF NOT EXISTS customers_pg_sync_log (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at          TEXT NOT NULL,
    finished_at         TEXT,
    duration_ms         INTEGER,
    rows_synced         INTEGER,
    rows_removed        INTEGER,
    success             INTEGER NOT NULL DEFAULT 0,
    error_message       TEXT,
    triggered_by        TEXT                              -- 'scheduled', 'manual', 'startup'
);
CREATE INDEX IF NOT EXISTS idx_customers_pg_sync_log_started
    ON customers_pg_sync_log(started_at DESC);
