-- Migration 043 — Job AI dedicato a classificazione tassonomica delle mail
--
-- A differenza di `classify_email` (urgenza + intent + suggested_action),
-- `email_taxonomy` serve SOLO a etichettare ogni mail con una categoria
-- macro + sub-categoria, per costruire KPI di distribuzione e validare
-- la composizione del traffico mail.
--
-- Output atteso (schema SCHEMA_TAXONOMY in ai_assistant/taxonomy.py):
--   {
--     "category": "<una delle CATEGORIE>",
--     "subcategory": "<libero>",
--     "confidence": 0.0-1.0,
--     "rationale": "<1-2 frasi>"
--   }
--
-- Caso d'uso:
-- 1. Tutte le mail (o subset) → action `ai_taxonomy` → log decisione.
-- 2. KPI `/ai/taxonomy`: vedi distribuzione (es. 60% newsletter, 20%
--    transazionali, 10% notifiche automatiche, ...).
-- 3. Dopo qualche giorno di osservazione: crea regole statiche con
--    priority più alta che bypassano l'AI (es. newsletter → quarantine
--    direct senza chiamare Claude → risparmio costo).

INSERT OR IGNORE INTO ai_jobs (job_code, description, modality, default_timeout_ms, can_redact_pii, requires_structured_output)
VALUES (
    'email_taxonomy',
    'Classificazione tassonomica delle mail in categorie macro (newsletter, notifica automatica, transazionale, richiesta assistenza, ecc). Solo log, no side effects.',
    'sync',
    5000,
    1,
    1
);

-- Seed binding default su Haiku (tassonomia non richiede modello grosso)
INSERT OR IGNORE INTO ai_job_bindings
    (tenant_id, job_code, provider_id, model_id, temperature, max_tokens, timeout_ms,
     traffic_split, enabled, version, notes)
SELECT
    1, 'email_taxonomy',
    (SELECT id FROM ai_providers WHERE tenant_id=1 AND kind='claude' AND enabled=1 LIMIT 1),
    'claude-haiku-4-5', 0.0, 400, 5000, 100, 1, 1,
    'seed migration 043: classificazione tassonomica per KPI traffico mail'
WHERE EXISTS (SELECT 1 FROM ai_providers WHERE tenant_id=1 AND kind='claude' AND enabled=1);
