-- Migration 040 — Relay client ACL (whitelist IP/CIDR per consegna SMTP)
--
-- Lista di IP o CIDR autorizzati a consegnare mail al listener :25.
-- Quando la tabella ha almeno una riga `enabled=1`:
--   - il listener accetta solo connessioni da quegli IP/CIDR
--   - tutto il resto riceve "550 5.7.1 Relaying denied (client not authorized)"
-- Quando vuota:
--   - nessun enforcement applicativo (filtro solo a livello firewall/UFW)
-- Sostituisce l'esigenza di restringere via UFW a una whitelist troppo lunga
-- e permette gestione runtime via UI senza dover toccare il sistema.

CREATE TABLE IF NOT EXISTS relay_client_acl (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       INTEGER NOT NULL DEFAULT 1 REFERENCES tenants(id),
    -- IPv4 singolo (es. "192.168.20.25") o CIDR (es. "192.168.20.0/24")
    ip_or_cidr      TEXT NOT NULL,
    label           TEXT,
    description     TEXT,
    enabled         INTEGER NOT NULL DEFAULT 1,
    set_by          TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (tenant_id, ip_or_cidr)
);

CREATE INDEX IF NOT EXISTS idx_relay_acl_enabled
    ON relay_client_acl(tenant_id, enabled) WHERE enabled = 1;

-- Seed: nessuna riga di default (backward compatible — listener non enforce).
-- L'admin abilita la feature aggiungendo manualmente le subnet da UI:
--   /relay-acl  →  Quick add: 192.168.20.0/24 ESVA / 192.168.4.0/24 admin / ecc.
