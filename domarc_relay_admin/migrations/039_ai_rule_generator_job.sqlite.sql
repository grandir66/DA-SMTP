-- 039: AI Rule Wizard — registra il nuovo job_code 'rule_generator'.
--
-- Caso d'uso: l'admin descrive a parole una regola (o seleziona N campioni
-- da events_log) e l'AI compila lo schema regola pronto per upsert. Utile
-- soprattutto per pattern di sistemi automatici ripetitivi (CloudTIK,
-- monitoring, alert) dove il riconoscimento del pattern da esempi reali
-- e' superiore alla descrizione testuale.
--
-- Strategia:
-- 1. INSERT idempotente in ai_jobs con job_code='rule_generator'.
-- 2. Niente seed binding di default: l'admin lo crea da /ai/models scegliendo
--    quale provider Claude usare (consigliato Haiku 4.5 per costo+latenza).
--    Il route /rules/ai-wizard mostra un avviso se nessun binding e' attivo.
-- 3. Niente nuove tabelle: l'output dell'AI e' transiente (passa per session
--    flask), il salvataggio finale usa la pipeline regole esistente.

BEGIN;

INSERT OR IGNORE INTO ai_jobs (
    job_code,
    description,
    modality,
    default_timeout_ms,
    can_redact_pii,
    requires_structured_output
) VALUES (
    'rule_generator',
    'Genera la specifica JSON di una regola SMTP a partire da descrizione testuale o da N campioni reali (events_log). Output validato lato server prima del save.',
    'sync',
    20000,
    0,  -- no PII redact: l'admin sta visualizzando dati a cui ha gia' accesso
    1   -- structured output (json_schema)
);

COMMIT;
