-- Migration 041 — Whitelist regole "force_live" (bypass shadow cascade)
--
-- Permette di marcare singole regole come "sempre live", anche se il dominio
-- destinatario o il recipient_group dovrebbero metterle in shadow. Caso d'uso
-- principale: una regola `ai_classify` che vogliamo sempre attiva (analisi +
-- arricchimento metadati) mentre il resto del dominio è ancora in shadow per
-- la fase di cutover.
--
-- Logica nel listener (`_check_shadow_cascata`):
--   if winning_rule.force_live: return None  # niente shadow per questa rule
--
-- Default 0: comportamento invariato (le regole rispettano il shadow del dominio).

-- ALTER idempotente via mini-migration Python (SQLite non supporta IF NOT EXISTS
-- su ALTER ADD COLUMN). La duplicate column viene catturata dalla mini-migration
-- in storage/sqlite_impl.py.
ALTER TABLE rules ADD COLUMN force_live INTEGER NOT NULL DEFAULT 0;
