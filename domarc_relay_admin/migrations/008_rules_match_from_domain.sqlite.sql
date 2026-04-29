-- Migration 008 — rules.match_from_domain
--
-- Aggiunge un filtro semplificato sul dominio del mittente, equivalente a
-- match_from_regex='(?i)@<domain>$' ma più rapido da scrivere e leggere.
-- Quando entrambi sono valorizzati, sono in AND (entrambi devono matchare).

ALTER TABLE rules ADD COLUMN match_from_domain TEXT;
CREATE INDEX IF NOT EXISTS idx_rules_match_from_domain ON rules(match_from_domain);
