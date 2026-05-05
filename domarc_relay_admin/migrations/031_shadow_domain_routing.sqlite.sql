-- 031: Shadow mode per domain_routing (fase 2 della modalita' shadow regole)
--
-- Scopo: marcare un intero dominio (es. domarc.it) come "in shadow". Le mail
-- dirette a qualsiasi mailbox del dominio vengono valutate normalmente dal
-- rule engine (chain completa visibile in /events), ma il dispatch finale
-- viene forzato a default_delivery: la mail arriva al destinatario reale,
-- NESSUN side-effect.
--
-- Caso d'uso: testare l'apertura di un nuovo dominio al rule engine (es.
-- cutover di domarc.it dal pilota datia.it) per qualche giorno in shadow,
-- osservare le chain di valutazione, validare le regole, poi promuovere
-- togliendo il flag.
--
-- Cascata di shadow (ordine di check nel listener):
--   dominio -> recipient_group -> regola singola
--   il primo trovato attiva lo shadow e logga shadow_origin

BEGIN;

ALTER TABLE domain_routing ADD COLUMN shadow_mode INTEGER NOT NULL DEFAULT 0;
ALTER TABLE domain_routing ADD COLUMN shadow_note TEXT;

CREATE INDEX IF NOT EXISTS idx_domain_routing_shadow
    ON domain_routing(shadow_mode) WHERE shadow_mode = 1;

COMMIT;
