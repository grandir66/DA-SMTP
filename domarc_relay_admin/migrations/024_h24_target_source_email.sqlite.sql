-- Migration 024: smtp_relay_h24_targets — match granulare per indirizzo email
--
-- Aggiunge `source_email` opzionale: se valorizzato vince sul match per
-- dominio. Pensato per webmail pubblici (gmail, yahoo, libero, outlook.com,
-- icloud, ...) dove è insicuro mappare un intero dominio.
--
-- Match cascade nel listener:
--   1. source_email match esatto sull'email del mittente (case-insensitive)
--   2. source_domain match esatto sul dominio del mittente
--   3. fallback setting h24.default_inbound_alias

-- Allenta UNIQUE su source_domain (ora source_domain può essere NULL se
-- source_email è valorizzato). Necessario ricostruire la tabella in SQLite.
ALTER TABLE smtp_relay_h24_targets ADD COLUMN source_email TEXT;

-- Index per lookup veloce email
CREATE INDEX IF NOT EXISTS idx_h24_targets_email
    ON smtp_relay_h24_targets(source_email)
    WHERE source_email IS NOT NULL AND enabled = 1;
