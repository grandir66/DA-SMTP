-- Migration 042 — AI model override per singola regola
--
-- Permette di selezionare un modello AI specifico (es. Claude Sonnet o Opus)
-- per UNA singola regola, invece di usare quello del binding di default per
-- il job_code. Utile per regole critiche che richiedono modello piu' accurato.
--
-- NULL = usa binding di default (comportamento attuale = Haiku per tutti).
-- Esempi:
--   - rule 100 ai_classify per mail H24 critiche  → ai_model_id = 'claude-sonnet-4-6'
--   - rule 200 ai_classify per mail standard    → ai_model_id = NULL (haiku default)

ALTER TABLE rules ADD COLUMN ai_model_id TEXT;
