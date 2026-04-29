-- Migration 010 — rules: gerarchia padre/figlio (versione PostgreSQL).
-- Vedi 010_rule_groups.sqlite.sql per la documentazione completa.

ALTER TABLE rules ADD COLUMN IF NOT EXISTS parent_id INTEGER REFERENCES rules(id) ON DELETE CASCADE;
ALTER TABLE rules ADD COLUMN IF NOT EXISTS is_group SMALLINT NOT NULL DEFAULT 0 CHECK (is_group IN (0, 1));
ALTER TABLE rules ADD COLUMN IF NOT EXISTS group_label TEXT;
ALTER TABLE rules ADD COLUMN IF NOT EXISTS exclusive_match SMALLINT NOT NULL DEFAULT 1 CHECK (exclusive_match IN (0, 1));
ALTER TABLE rules ADD COLUMN IF NOT EXISTS continue_in_group SMALLINT NOT NULL DEFAULT 0 CHECK (continue_in_group IN (0, 1));
ALTER TABLE rules ADD COLUMN IF NOT EXISTS exit_group_continue SMALLINT NOT NULL DEFAULT 0 CHECK (exit_group_continue IN (0, 1));

CREATE INDEX IF NOT EXISTS idx_rules_parent_id ON rules(parent_id);
CREATE INDEX IF NOT EXISTS idx_rules_is_group ON rules(is_group);
CREATE INDEX IF NOT EXISTS idx_rules_priority_active ON rules(enabled, priority) WHERE enabled = 1;
