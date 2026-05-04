-- 026: Tracking esteso codici monouso (oneshot) + log utilizzi codici permanenti
--
-- Codici monouso:
--   - sent_to_email: a quale indirizzo è stato spedito il codice (mailto button)
--   - sent_at: timestamp spedizione
--   - accepted_at / accepted_by_email: quando e da chi è stato consumato
--   - state: pending | accepted | expired | canceled (derivato/esplicito)
--
-- Codici permanenti — log utilizzi esteso:
--   - body_excerpt: primi 4000 char del corpo della mail di richiesta
--   - from_email già presente come from_address (riusato)
--
BEGIN;

ALTER TABLE authorization_codes ADD COLUMN sent_to_email TEXT;
ALTER TABLE authorization_codes ADD COLUMN sent_at TEXT;
ALTER TABLE authorization_codes ADD COLUMN accepted_at TEXT;
ALTER TABLE authorization_codes ADD COLUMN accepted_by_email TEXT;
ALTER TABLE authorization_codes ADD COLUMN state TEXT NOT NULL DEFAULT 'pending';

CREATE INDEX IF NOT EXISTS idx_authcodes_state
    ON authorization_codes(state, valid_until);

-- Backfill state per i record esistenti
UPDATE authorization_codes
   SET state = CASE
                  WHEN used_at IS NOT NULL THEN 'accepted'
                  WHEN datetime(valid_until) < datetime('now') THEN 'expired'
                  ELSE 'pending'
               END
 WHERE state = 'pending';

-- Anche accepted_at per i record già usati
UPDATE authorization_codes
   SET accepted_at = used_at,
       accepted_by_email = used_by
 WHERE used_at IS NOT NULL AND accepted_at IS NULL;

-- Log utilizzi codici permanenti: aggiungi body_excerpt
ALTER TABLE customer_h24_codes_usage ADD COLUMN body_excerpt TEXT;
ALTER TABLE customer_h24_codes_usage ADD COLUMN from_email TEXT;

-- Migra from_address → from_email (nuova colonna canonica)
UPDATE customer_h24_codes_usage
   SET from_email = from_address
 WHERE from_email IS NULL AND from_address IS NOT NULL;

COMMIT;
