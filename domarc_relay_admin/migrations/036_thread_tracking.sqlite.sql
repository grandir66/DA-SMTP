-- 036: Thread tracking RFC 2822 — evita ticket duplicati su risposte
--
-- Caso d'uso: cliente H24 manda mail -> ticket A. Tecnico risponde -> cliente
-- risponde nel thread -> oggi viene aperto ticket B duplicato. Soluzione:
-- usare Message-ID/In-Reply-To/References per rilevare che e' una continuazione
-- di thread gia' tracciato e applicare un'azione differenziata (di default
-- solo default_delivery, niente nuovo ticket).
--
-- Strategia:
-- 1. Salvo in_reply_to + references nei record events
-- 2. Pipeline calcola is_thread_continuation pre-evaluate verificando se
--    in_reply_to o uno dei references matcha un message_id gia' visto
-- 3. Nuovo criterio match_is_thread_continuation (tristate) sulle regole
-- 4. Seed regola "thread continuation: default_delivery" a priority=5
--    in 'globali': intercetta tutti i thread continuati prima delle regole
--    che aprono ticket, fa default_delivery, popola reply_to_event_uuid
-- 5. UI mostra link thread nel detail evento

BEGIN;

-- =========================================================================
-- 1. ALTER events: nuovi campi tracking thread
-- =========================================================================

ALTER TABLE events ADD COLUMN in_reply_to TEXT;
ALTER TABLE events ADD COLUMN references_json TEXT;       -- JSON array dei References
ALTER TABLE events ADD COLUMN reply_to_event_uuid TEXT;   -- UUID dell'evento padre (se thread continuation)
ALTER TABLE events ADD COLUMN thread_root_uuid TEXT;      -- UUID del primo evento del thread

CREATE INDEX IF NOT EXISTS idx_events_in_reply_to
    ON events(in_reply_to) WHERE in_reply_to IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_events_message_id
    ON events(message_id) WHERE message_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_events_thread_root
    ON events(thread_root_uuid) WHERE thread_root_uuid IS NOT NULL;

-- =========================================================================
-- 2. ALTER rules: nuovo criterio match_is_thread_continuation
-- =========================================================================

ALTER TABLE rules ADD COLUMN match_is_thread_continuation INTEGER;
-- Tristate: NULL=indifferente, 1=solo continuazioni, 0=solo prime mail di thread

-- =========================================================================
-- 3. Seed regola di intercettazione thread continuation
--
-- Si piazza a priority=5 nel set 'globali': matcha qualsiasi mail che e'
-- una risposta a un evento tracked (con ticket aperto). Action:
-- default_delivery. Cosi' la risposta arriva al tecnico/cliente reale ma
-- NON viene aperto un ticket duplicato ne' inviato auto_reply.
--
-- L'operatore puo' modificarla (es. cambiare action a ai_classify per
-- decidere caso per caso) o disabilitarla se vuole il vecchio comportamento.
-- =========================================================================

INSERT OR IGNORE INTO rules
    (tenant_id, name, description, scope_type, priority, enabled,
     match_is_thread_continuation, action, action_map, severity,
     continue_after_match, rule_set_id, created_by)
VALUES (
    1,
    'Thread continuation — passa al destinatario',
    'M036: intercetta le risposte a mail gia'' processate (RFC 2822 In-Reply-To/References). Evita di aprire ticket duplicati o inviare auto_reply quando un cliente risponde nel thread di un ticket esistente. La mail viene comunque consegnata via default_delivery. Per personalizzare: modifica priorita''/azione o disabilita se preferisci che ogni risposta riapra il flusso.',
    'global', 5, 1,
    1,
    'default_delivery',
    '{"reason":"thread_continuation","keep_original_delivery":true}',
    NULL, 0,
    (SELECT id FROM rule_sets WHERE code='globali' AND tenant_id=1),
    'system'
);

COMMIT;
