-- 028: Customer sync agnostico — tabella clienti autoritativa + sorgenti pluggabili
--
-- Scopo: scollegare definitivamente la tabella clienti dai DB esterni
-- `solution`/`stormshield` (hardcoded in customer_sources/postgres_source.py)
-- e renderla alimentabile da N sorgenti eterogenee (postgres custom, mssql,
-- csv_file, json_url) con field-mapping configurabile e schedule indipendente.
--
-- La tabella `customers_pg_cache` viene rinominata in `customers` e diventa
-- AUTORITATIVA (non piu' cache): se la sorgente fallisce, l'ultimo snapshot
-- resta valido e il listener continua a leggere senza perdita.
--
-- Strategia transizione single-shot:
--   - La sorgente legacy "Postgres solution Domarc" e' seedata gia'
--     enabled=1 con sentinel `_use_legacy_pgconfig=true`: il provider
--     postgres riconosce il sentinel e legge la config da PgConfig.from_settings()
--     come fa gia' il vecchio sync. Schedule 24h.
--   - Il vecchio start_sync_thread() del backend `postgres` viene disabilitato
--     in app.py (sostituito da start_sync_scheduler()). Codice resta come
--     riferimento ma non piu' invocato.
--   - Operatore puo' modificare query/mapping della sorgente, disabilitarla,
--     o aggiungerne altre dalla UI /customer-sync/.

BEGIN;

-- =========================================================================
-- 1. Rinomina customers_pg_cache -> customers (tabella autoritativa)
-- =========================================================================

ALTER TABLE customers_pg_cache RENAME TO customers;

-- last_synced_from_source_id: traccia quale sorgente ha portato ogni record
-- (NULL = pre-migration o cliente manuale futuro).
ALTER TABLE customers ADD COLUMN last_synced_from_source_id INTEGER;

-- Reindex (gli indici vecchi sono stati rinominati automaticamente
-- da SQLite ALTER TABLE RENAME TO ma alcuni nomi rimangono)
DROP INDEX IF EXISTS idx_customers_pg_cache_tenant;
DROP INDEX IF EXISTS idx_customers_pg_cache_synced;
CREATE INDEX IF NOT EXISTS idx_customers_tenant
    ON customers(tenant_id);
CREATE INDEX IF NOT EXISTS idx_customers_synced
    ON customers(last_synced_at DESC);
CREATE INDEX IF NOT EXISTS idx_customers_source
    ON customers(last_synced_from_source_id);

-- =========================================================================
-- 2. customer_sync_sources: sorgenti pluggabili
-- =========================================================================

CREATE TABLE IF NOT EXISTS customer_sync_sources (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       INTEGER NOT NULL DEFAULT 1,
    name            TEXT NOT NULL,
    -- Tipo provider supportato: postgres | mssql | csv_file | json_url
    -- (csv_url rinviato a fase 2)
    kind            TEXT NOT NULL,
    enabled         INTEGER NOT NULL DEFAULT 1,

    -- Connection params kind-specific (cifrati Fernet i campi sensibili).
    -- postgres/mssql: {host, port, user, password_enc, dbname, sslmode?}
    --                 OPPURE {"_use_legacy_pgconfig": true} per la sorgente
    --                 legacy che riusa PgConfig.from_settings()
    -- csv_file:       {path, delimiter, encoding, has_header}
    -- json_url:       {url, headers_json, auth_enc?, method?}
    config_json     TEXT NOT NULL,

    -- Per postgres/mssql: SQL parametrizzato (single SELECT).
    -- Per json_url: JSONPath per estrarre l'array di record dalla risposta.
    -- Per csv_file: NULL.
    -- Per la sorgente legacy: NULL (il provider usa la logica discovery interna).
    query_or_path   TEXT,

    -- Mapping {colonna_sorgente -> colonna_locale} oppure
    --         {colonna_sorgente -> {target, transform}}.
    -- Esempi transform: lowercase | strip | default:<v> | split:<sep> | bool |
    --                   json_parse | coalesce:<col1,col2>
    -- Per la sorgente legacy: '{"_legacy": true}' (passthrough, fetch ritorna
    -- gia' campi canonici).
    mapping_json    TEXT NOT NULL,

    -- Scheduling: ogni quante ore eseguire un sync. Default 24h.
    schedule_hours  INTEGER NOT NULL DEFAULT 24,

    -- Cosa fare con codcli che non sono piu' nella sorgente:
    --   flag   -> UPDATE customers SET contract_active=0 (default, conserva storico)
    --   delete -> DELETE FROM customers (rimozione fisica)
    --   keep   -> nessuna azione (utile per import una-tantum o sorgenti parziali)
    on_missing      TEXT NOT NULL DEFAULT 'flag',

    -- Stato ultimo run
    last_run_at     TEXT,
    last_run_status TEXT,            -- 'ok' | 'error' | 'partial' | 'running'
    last_run_error  TEXT,
    next_run_at     TEXT,            -- now + schedule_hours dopo ogni run

    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    created_by      TEXT,

    UNIQUE (tenant_id, name)
);

CREATE INDEX IF NOT EXISTS idx_customer_sync_sources_next_run
    ON customer_sync_sources(enabled, next_run_at);

-- =========================================================================
-- 3. customer_sync_runs: audit log per ogni run (manuale o schedulato)
-- =========================================================================

CREATE TABLE IF NOT EXISTS customer_sync_runs (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id         INTEGER NOT NULL REFERENCES customer_sync_sources(id) ON DELETE CASCADE,
    started_at        TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at       TEXT,
    duration_ms       INTEGER,
    status            TEXT,             -- 'running' | 'ok' | 'error' | 'partial'
    n_fetched         INTEGER DEFAULT 0,
    n_inserted        INTEGER DEFAULT 0,
    n_updated         INTEGER DEFAULT 0,
    n_unchanged       INTEGER DEFAULT 0,
    n_flagged_missing INTEGER DEFAULT 0,
    n_errored         INTEGER DEFAULT 0,
    error_message     TEXT,
    triggered_by      TEXT,             -- 'schedule' | 'manual:<username>' | 'startup'
    dry_run           INTEGER NOT NULL DEFAULT 0,
    report_json       TEXT              -- preview/diff per dry-run
);

CREATE INDEX IF NOT EXISTS idx_customer_sync_runs_source_started
    ON customer_sync_runs(source_id, started_at DESC);

-- =========================================================================
-- 4. customer_sync_locks: anti race-condition tra worker gunicorn
-- =========================================================================

CREATE TABLE IF NOT EXISTS customer_sync_locks (
    source_id   INTEGER PRIMARY KEY REFERENCES customer_sync_sources(id) ON DELETE CASCADE,
    acquired_at TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    holder      TEXT             -- es. 'pid:1234@hostname'
);

-- =========================================================================
-- 5. Seed sorgente legacy "Postgres solution Domarc"
--
-- Sentinel _use_legacy_pgconfig=true => il provider postgres usa
-- PgConfig.from_settings() esistente (env + UI Integrations) e replica
-- la logica delle 5 funzioni _load_* del vecchio postgres_source.py.
-- mapping_json = {"_legacy": true} => fetch() ritorna gia' record canonici.
-- =========================================================================

INSERT OR IGNORE INTO customer_sync_sources
    (tenant_id, name, kind, enabled, config_json, query_or_path,
     mapping_json, schedule_hours, on_missing, created_by)
VALUES
    (1,
     'Postgres solution Domarc (legacy)',
     'postgres',
     1,
     '{"_use_legacy_pgconfig": true}',
     NULL,
     '{"_legacy": true}',
     24,
     'keep',  -- legacy: keep per evitare di disattivare clienti se lo schema cambia
     'system');

COMMIT;
