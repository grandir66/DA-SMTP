-- 030: Shadow mode per recipient_groups (fase 1 della modalita' shadow regole)
--
-- Scopo: marcare un gruppo destinatari come "in shadow". Le mail dirette
-- ai membri del gruppo vengono valutate normalmente dal rule engine (chain
-- completa visibile in /events), ma il dispatch finale viene forzato a
-- default_delivery: la mail arriva al destinatario reale, NESSUN side-effect
-- (no auto_reply, no ticket, no forward, no redirect). In events.payload_metadata
-- viene loggato `would_have_executed` con dettaglio della regola vincente.
--
-- Caso d'uso tipico: aggiungere progressivamente mailbox al gruppo shadow,
-- osservare in /events cosa SAREBBE stato fatto, validare le regole, poi
-- promuovere la mailbox a live togliendola dal gruppo.
--
-- Fasi successive (M031, M032, M033) estenderanno lo shadow a domain,
-- rule_set, regola singola con la stessa cascata di override.

BEGIN;

ALTER TABLE recipient_groups ADD COLUMN shadow_mode INTEGER NOT NULL DEFAULT 0;
ALTER TABLE recipient_groups ADD COLUMN shadow_note TEXT;

CREATE INDEX IF NOT EXISTS idx_recipient_groups_shadow
    ON recipient_groups(shadow_mode) WHERE shadow_mode = 1;

COMMIT;
