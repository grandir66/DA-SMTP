-- Migration 017 — F3.5 Rule Proposer: aggiunge fingerprint_hex per dedup proposte.
--
-- Senza questo campo, ogni run del proposer rigenererebbe le stesse proposte
-- per cluster identici. Con fingerprint_hex (SHA256 di intent+action+subject_pattern+from_domain)
-- il proposer può saltare cluster già processati (pending/accepted/rejected).

ALTER TABLE ai_rule_proposals ADD COLUMN fingerprint_hex TEXT;
CREATE INDEX IF NOT EXISTS idx_ai_proposals_fingerprint ON ai_rule_proposals(tenant_id, fingerprint_hex);
