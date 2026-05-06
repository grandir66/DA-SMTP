-- Migration 038 — Domain resolve strategy per domini condivisi tra clienti
--
-- Quando una mail arriva da `*@dominio.it` e quel dominio appare nei domains_json
-- di più clienti del gestionale, oggi il listener prendeva il PRIMO trovato →
-- routing sbagliato (es. domarc.it tra DOMARC SRL, CARMEX ITALIA, ZANFI).
--
-- Questa tabella permette di configurare per ogni dominio condiviso una strategia:
--   - 'auto'    (default): primo cliente con contract_active=1 (preserva comportamento)
--   - 'primary' : usa il cliente specificato in primary_codcli (forza l'owner)
--   - 'bypass'  : NON risolve cliente per quel dominio (codcli=NULL, va a catch-all).
--                Tipico per provider PEC condivisi (dapec.it, aliceposta.it, iol.it).
--
-- L'algoritmo del listener consulta prima gli alias del local-part (più granulare),
-- POI il dominio (con strategia configurata).

CREATE TABLE domain_resolve_strategy (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       INTEGER NOT NULL DEFAULT 1 REFERENCES tenants(id),
    domain          TEXT NOT NULL,
    strategy        TEXT NOT NULL DEFAULT 'auto',
    primary_codcli  TEXT,
    note            TEXT,
    -- Snapshot al momento della creazione/aggiornamento (denormalizzato per UI):
    n_customers     INTEGER NOT NULL DEFAULT 0,
    n_active        INTEGER NOT NULL DEFAULT 0,
    set_by          TEXT,
    set_at          TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (tenant_id, domain),
    CHECK (strategy IN ('auto', 'primary', 'bypass'))
);

CREATE INDEX idx_domain_strategy_domain ON domain_resolve_strategy(tenant_id, domain);
