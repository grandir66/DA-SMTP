-- Migration 011 — privacy bypass list (versione PostgreSQL).
-- Vedi 011_privacy_bypass.sqlite.sql per la documentazione completa.

ALTER TABLE addresses_from ADD COLUMN IF NOT EXISTS privacy_bypass SMALLINT NOT NULL DEFAULT 0 CHECK (privacy_bypass IN (0, 1));
ALTER TABLE addresses_from ADD COLUMN IF NOT EXISTS privacy_bypass_reason TEXT;
ALTER TABLE addresses_from ADD COLUMN IF NOT EXISTS privacy_bypass_at TIMESTAMPTZ;
ALTER TABLE addresses_from ADD COLUMN IF NOT EXISTS privacy_bypass_by TEXT;

ALTER TABLE addresses_to ADD COLUMN IF NOT EXISTS privacy_bypass SMALLINT NOT NULL DEFAULT 0 CHECK (privacy_bypass IN (0, 1));
ALTER TABLE addresses_to ADD COLUMN IF NOT EXISTS privacy_bypass_reason TEXT;
ALTER TABLE addresses_to ADD COLUMN IF NOT EXISTS privacy_bypass_at TIMESTAMPTZ;
ALTER TABLE addresses_to ADD COLUMN IF NOT EXISTS privacy_bypass_by TEXT;

CREATE INDEX IF NOT EXISTS idx_addresses_from_privacy ON addresses_from(privacy_bypass) WHERE privacy_bypass = 1;
CREATE INDEX IF NOT EXISTS idx_addresses_to_privacy ON addresses_to(privacy_bypass) WHERE privacy_bypass = 1;

CREATE TABLE IF NOT EXISTS privacy_bypass_domains (
    id           SERIAL PRIMARY KEY,
    tenant_id    INTEGER NOT NULL DEFAULT 1 REFERENCES tenants(id),
    domain       TEXT NOT NULL,
    scope        TEXT NOT NULL DEFAULT 'both' CHECK (scope IN ('from', 'to', 'both')),
    reason       TEXT,
    enabled      SMALLINT NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by   TEXT,
    UNIQUE(tenant_id, domain, scope)
);
CREATE INDEX IF NOT EXISTS idx_privacy_bypass_domains_tenant ON privacy_bypass_domains(tenant_id, enabled) WHERE enabled = 1;

CREATE TABLE IF NOT EXISTS privacy_bypass_audit (
    id           SERIAL PRIMARY KEY,
    tenant_id    INTEGER NOT NULL DEFAULT 1 REFERENCES tenants(id),
    target_kind  TEXT NOT NULL CHECK (target_kind IN ('address_from', 'address_to', 'domain')),
    target_value TEXT NOT NULL,
    action       TEXT NOT NULL CHECK (action IN ('enable', 'disable', 'create', 'delete')),
    reason       TEXT,
    actor        TEXT,
    at           TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_privacy_audit_at ON privacy_bypass_audit(at DESC);
