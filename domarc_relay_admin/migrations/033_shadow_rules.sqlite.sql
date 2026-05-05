-- 033: Shadow mode per regola singola (fase 3 della modalita' shadow regole)
--
-- Scopo: granularita' massima — marcare una specifica regola come "in shadow".
-- Quando la regola e' la vincente del rule engine, il dispatch viene forzato
-- a default_delivery (con log payload_metadata.would_have_executed) invece
-- di eseguire l'azione reale.
--
-- Caso d'uso tipico: hai una nuova regola (es. dal wizard) e vuoi vederla
-- "scattare" su mail reali per validarla, senza rischiare di mandare auto-reply
-- sbagliate o aprire ticket non voluti. La metti in shadow per qualche giorno,
-- in /events vedi quando matcha, valuta i casi, poi togli il flag.
--
-- NOTA: M032 (shadow per rule_set) e' stata saltata su richiesta operatore.
-- Se servisse, la cascata e' gia' predisposta: basterebbe aggiungere il check
-- rule_sets.shadow_mode tra domain e regola singola.

BEGIN;

ALTER TABLE rules ADD COLUMN shadow_mode INTEGER NOT NULL DEFAULT 0;
ALTER TABLE rules ADD COLUMN shadow_note TEXT;

CREATE INDEX IF NOT EXISTS idx_rules_shadow
    ON rules(shadow_mode) WHERE shadow_mode = 1;

COMMIT;
