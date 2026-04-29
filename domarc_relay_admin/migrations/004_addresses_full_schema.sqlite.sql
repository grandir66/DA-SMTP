-- Migration 004 — allinea schema addresses_from/to con il manager (PG smtp_relay_addresses_*).
--
-- Aggiunge colonne local_part, domain, codcli_source, created_by, blocked, blocked_reason.
-- Necessario per l'import dal manager e per la UI di gestione anagrafica indirizzi.

BEGIN;

ALTER TABLE addresses_from ADD COLUMN local_part TEXT;
ALTER TABLE addresses_from ADD COLUMN domain TEXT;
ALTER TABLE addresses_from ADD COLUMN codcli_source TEXT;
ALTER TABLE addresses_from ADD COLUMN created_by TEXT;
ALTER TABLE addresses_from ADD COLUMN blocked INTEGER NOT NULL DEFAULT 0;
ALTER TABLE addresses_from ADD COLUMN blocked_reason TEXT;

ALTER TABLE addresses_to ADD COLUMN local_part TEXT;
ALTER TABLE addresses_to ADD COLUMN domain TEXT;

CREATE INDEX IF NOT EXISTS idx_addresses_from_codcli ON addresses_from(tenant_id, codice_cliente);
CREATE INDEX IF NOT EXISTS idx_addresses_from_domain ON addresses_from(tenant_id, domain);
CREATE INDEX IF NOT EXISTS idx_addresses_to_domain ON addresses_to(tenant_id, domain);

COMMIT;
