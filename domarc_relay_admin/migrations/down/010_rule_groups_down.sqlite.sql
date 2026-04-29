-- Rollback migration 010 (SQLite).
-- ATTENZIONE: SQLite non supporta DROP COLUMN su versioni < 3.35. Per ambienti
-- più vecchi serve un rebuild manuale della tabella. Su SQLite >= 3.35 (Ubuntu
-- 22.04+) basta DROP COLUMN.
--
-- Eseguire manualmente solo in caso di rollback completo della feature
-- "Rule Engine v2 — gerarchia padre/figlio". I dati sui figli vengono persi:
-- assicurarsi prima di promuovere/scartare ogni gruppo via UI.

-- Rimuove eventuali figli orfanizzandoli (parent_id=NULL)
UPDATE rules SET parent_id = NULL WHERE parent_id IS NOT NULL;
-- Cancella i record gruppo (non eseguono azioni di per sé)
DELETE FROM rules WHERE is_group = 1;

DROP INDEX IF EXISTS idx_rules_priority_active;
DROP INDEX IF EXISTS idx_rules_is_group;
DROP INDEX IF EXISTS idx_rules_parent_id;

ALTER TABLE rules DROP COLUMN exit_group_continue;
ALTER TABLE rules DROP COLUMN continue_in_group;
ALTER TABLE rules DROP COLUMN exclusive_match;
ALTER TABLE rules DROP COLUMN group_label;
ALTER TABLE rules DROP COLUMN is_group;
ALTER TABLE rules DROP COLUMN parent_id;

DELETE FROM _migrations WHERE version = 10;
