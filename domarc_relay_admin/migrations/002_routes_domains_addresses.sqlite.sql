-- Migration 002 SQLite — Routes (smarthost), domain routing, addresses, settings extended
--
-- Aggiunge le tabelle SMTP-relay che erano nel manager Postgres ma non nel
-- skeleton initial standalone:
--   smtp_relay_routes              → routes (alias intercept → smarthost forward)
--   smtp_relay_domain_routing      → domain_routing (per dominio → smarthost default)
--   smtp_relay_addresses_from      → addresses_from (mittenti noti per resolve_codcli)
--   smtp_relay_addresses_to        → ELIMINATO in migration 005 (i destinatari sono i clienti, non un'anagrafica separata)
--   smtp_relay_settings (esteso)   → chiavi default smarthost, helo, ecc.

-- Routes: alias intercettati → smarthost forward / redirect target
CREATE TABLE IF NOT EXISTS routes (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id         INTEGER NOT NULL DEFAULT 1 REFERENCES tenants(id),
    local_part        TEXT NOT NULL,                           -- es. "info"
    domain            TEXT NOT NULL,                           -- es. "acme.it"
    codice_cliente    TEXT,
    forward_target    TEXT,                                    -- smarthost smtp host
    forward_port      INTEGER DEFAULT 25,
    forward_tls       TEXT DEFAULT 'opportunistic',            -- opportunistic | strict | none
    redirect_target   TEXT,                                    -- email a cui rediregere (alternativa a forward)
    enabled           INTEGER NOT NULL DEFAULT 1,
    apply_rules       INTEGER NOT NULL DEFAULT 1,              -- se false, bypassa rule engine
    notes             TEXT,
    created_at        TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at        TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (local_part, domain)
);
CREATE INDEX IF NOT EXISTS idx_routes_tenant ON routes(tenant_id, enabled);

-- Domain routing: per dominio → smarthost di default (se non c'è route specifica)
CREATE TABLE IF NOT EXISTS domain_routing (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id         INTEGER NOT NULL DEFAULT 1 REFERENCES tenants(id),
    domain            TEXT NOT NULL,
    smarthost_host    TEXT,
    smarthost_port    INTEGER DEFAULT 25,
    smarthost_tls     TEXT DEFAULT 'opportunistic',
    apply_rules       INTEGER NOT NULL DEFAULT 1,
    enabled           INTEGER NOT NULL DEFAULT 1,
    notes             TEXT,
    created_at        TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at        TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (tenant_id, domain)
);
CREATE INDEX IF NOT EXISTS idx_domain_routing_tenant ON domain_routing(tenant_id, enabled);

-- Mittenti noti (resolve codcli da from_address)
CREATE TABLE IF NOT EXISTS addresses_from (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id         INTEGER NOT NULL DEFAULT 1 REFERENCES tenants(id),
    email_address     TEXT NOT NULL,
    codice_cliente    TEXT,
    seen_count        INTEGER NOT NULL DEFAULT 1,
    first_seen_at     TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen_at      TEXT NOT NULL DEFAULT (datetime('now')),
    notes             TEXT,
    UNIQUE (tenant_id, email_address)
);
CREATE INDEX IF NOT EXISTS idx_addresses_from_tenant ON addresses_from(tenant_id, last_seen_at DESC);

-- Destinatari noti
CREATE TABLE IF NOT EXISTS addresses_to (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id         INTEGER NOT NULL DEFAULT 1 REFERENCES tenants(id),
    email_address     TEXT NOT NULL,
    codice_cliente    TEXT,
    seen_count        INTEGER NOT NULL DEFAULT 1,
    first_seen_at     TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen_at      TEXT NOT NULL DEFAULT (datetime('now')),
    notes             TEXT,
    UNIQUE (tenant_id, email_address)
);
CREATE INDEX IF NOT EXISTS idx_addresses_to_tenant ON addresses_to(tenant_id, last_seen_at DESC);

-- Settings extended con default smarthost
INSERT OR IGNORE INTO settings (key, value, description) VALUES
    ('default_smarthost', 'smtp.domarc.it',
     'Smarthost di fallback per forward/redirect quando non c''è una route specifica.'),
    ('default_smarthost_port', '25', 'Porta default smarthost.'),
    ('default_smarthost_tls', 'opportunistic', 'TLS mode default smarthost (opportunistic/strict/none).'),
    ('helo_hostname', 'mail-pilot.domarc.it', 'Hostname EHLO/HELO usato dal listener verso smarthost.'),
    ('listener_bind_host', '0.0.0.0', 'Bind host del listener SMTP (info-only, configurato in relay.yaml).'),
    ('listener_bind_port', '25', 'Bind port del listener SMTP (info-only).'),
    ('rate_limit_per_from_domain_hour', '100',
     'Rate limit anti-flood: max msg/h per dominio mittente. Eccesso → quarantine.');
