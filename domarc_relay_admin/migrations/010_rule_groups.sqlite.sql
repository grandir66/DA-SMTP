-- Migration 010 — rules: gerarchia padre/figlio (1 livello).
--
-- Introduce gruppi di regole con ereditarietà di match_* e action_map_defaults.
-- Lo spazio di priorità resta unico globale (1..999999): ogni record ha la sua
-- priority assoluta, niente moltiplicazioni padre*1000+figlio. La gerarchia è
-- solo logica per UI, ereditarietà e exclusive_match.
--
-- Compatibilità retroattiva totale: tutte le regole esistenti restano "orfane"
-- (parent_id=NULL, is_group=0) e si comportano come prima.
--
-- Il listener legacy (/opt/stormshield-smtp-relay/) continua a ricevere regole
-- flat tramite /api/v1/relay/rules/active: l'admin standalone si occupa di
-- appiattire la gerarchia prima di servirle.

ALTER TABLE rules ADD COLUMN parent_id INTEGER REFERENCES rules(id) ON DELETE CASCADE;
ALTER TABLE rules ADD COLUMN is_group INTEGER NOT NULL DEFAULT 0 CHECK (is_group IN (0, 1));
ALTER TABLE rules ADD COLUMN group_label TEXT;
ALTER TABLE rules ADD COLUMN exclusive_match INTEGER NOT NULL DEFAULT 1 CHECK (exclusive_match IN (0, 1));
ALTER TABLE rules ADD COLUMN continue_in_group INTEGER NOT NULL DEFAULT 0 CHECK (continue_in_group IN (0, 1));
ALTER TABLE rules ADD COLUMN exit_group_continue INTEGER NOT NULL DEFAULT 0 CHECK (exit_group_continue IN (0, 1));

CREATE INDEX IF NOT EXISTS idx_rules_parent_id ON rules(parent_id);
CREATE INDEX IF NOT EXISTS idx_rules_is_group ON rules(is_group);
CREATE INDEX IF NOT EXISTS idx_rules_priority_active ON rules(enabled, priority);
