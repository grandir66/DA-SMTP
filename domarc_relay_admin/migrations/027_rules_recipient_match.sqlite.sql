-- 027: Regole con match destinatario tramite gruppo + forward verso lista
--
-- match_to_group_id: alternativa esclusiva a match_to_regex (NON entrambi).
--   Se valorizzato, la regola scatta solo se uno dei destinatari della mail
--   è membro del gruppo recipient_groups indicato.
--
-- forward_to_emails: lista di indirizzi separati da ';' o ',', usata
--   dall'azione `forward` come destinatari (espansa al momento dell'invio).
--   Si può anche usare in combinazione con un gruppo destinatari tramite
--   forward_to_group_id (resolved at action time dal listener).
--
-- forward_to_group_id: shortcut per indicare un gruppo come target di forward.
--   Il listener espanderà i membri del gruppo.

BEGIN;

ALTER TABLE rules ADD COLUMN match_to_group_id INTEGER REFERENCES recipient_groups(id) ON DELETE SET NULL;
ALTER TABLE rules ADD COLUMN forward_to_emails TEXT;
ALTER TABLE rules ADD COLUMN forward_to_group_id INTEGER REFERENCES recipient_groups(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_rules_match_to_group
    ON rules(match_to_group_id) WHERE match_to_group_id IS NOT NULL;

COMMIT;
