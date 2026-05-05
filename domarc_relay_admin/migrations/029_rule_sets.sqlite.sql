-- 029: Rule sets — set di regole organizzati per profilo orario + set "sempre attivo"
--
-- Idea: ogni regola appartiene a un set. A runtime, il rule engine valuta solo
-- le regole dei set "attivi" per la mail in arrivo: il set "globali" e' sempre
-- attivo (CloudTIK alerts, sistemi automatici, AI learning, privacy bypass);
-- in piu' viene attivato il set associato al profilo orario del cliente
-- (standard/esteso/h24/nessuno).
--
-- Regole esistenti (22 in produzione) vanno tutte nel set "globali" per
-- preservare il comportamento attuale -- migrazione zero-touch.
-- L'operatore poi sposta singolarmente le regole nei set per profilo quando
-- vuole.

BEGIN;

-- =========================================================================
-- 1. Tabella rule_sets
-- =========================================================================

CREATE TABLE IF NOT EXISTS rule_sets (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       INTEGER NOT NULL DEFAULT 1,
    code            TEXT NOT NULL,                -- 'globali' | 'standard' | 'esteso' | 'h24' | 'nessuno' | custom
    name            TEXT NOT NULL,                -- label leggibile UI
    description     TEXT,

    -- Condizione di attivazione del set:
    is_always_active INTEGER NOT NULL DEFAULT 0,  -- 1 = sempre nel pool di valutazione
    -- profile_code in MAIUSCOLO per fare match con customers.tipologia_servizio
    -- (STD/EXT/H24/NO). NULL se il set non e' legato a un profilo specifico.
    profile_code    TEXT,

    -- Ordering tra set: sempre_attivo deve avere priorita' bassa (es. 10),
    -- set per profilo priorita' standard (es. 100). Usato per ordinamento UI.
    evaluation_order INTEGER NOT NULL DEFAULT 100,

    color           TEXT,                          -- esadecimale per badge UI
    enabled         INTEGER NOT NULL DEFAULT 1,
    is_builtin      INTEGER NOT NULL DEFAULT 0,    -- 1 = seed M029, code non modificabile
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),

    UNIQUE (tenant_id, code)
);

CREATE INDEX IF NOT EXISTS idx_rule_sets_tenant_enabled
    ON rule_sets(tenant_id, enabled);
CREATE INDEX IF NOT EXISTS idx_rule_sets_profile_code
    ON rule_sets(profile_code) WHERE profile_code IS NOT NULL;

-- =========================================================================
-- 2. Seed 5 set built-in
-- =========================================================================

-- Descrizioni allineate alla "regola d'oro" (post-cleanup 2026-05-05):
--   rule_set_id   = TIPO DI CONTRATTO del cliente
--   match_in_service = FINESTRA TEMPORALE corrente (rispetta naturalmente
--                      la gerarchia STD ⊂ EXT ⊂ H24 perche' calcolata dal
--                      profilo del singolo cliente)
INSERT OR IGNORE INTO rule_sets
    (tenant_id, code, name, description,
     is_always_active, profile_code, evaluation_order, color, is_builtin)
VALUES
    (1, 'globali', 'Sempre attive (globali)',
     'Set sempre valutato per ogni mail, indipendentemente dal contratto del cliente. METTI QUI le regole "trasversali": sistemi automatici (CloudTIK alerts), AI learning, privacy bypass, catch-all default. PER REGOLE ORARIO-DIPENDENTI usa questo set + match_in_service=true/false (lo stato temporale rispetta naturalmente la gerarchia STD ⊂ EXT ⊂ H24, perche'' calcolato dal profilo del singolo cliente). In dubbio? Metti qui.',
     1, NULL, 10, '#1e40af', 1),
    (1, 'standard', 'Orario standard',
     'Set attivo SOLO per clienti con tipologia_servizio=STD (Standard, lun-ven 08:30-13:00 + 14:30-17:30). Da usare per regole che valgono SOLO per il livello di contratto Standard (es. tariffe specifiche, template dedicati, blocchi di sicurezza specifici). Per regole valide "in orario lavorativo" indipendentemente dal contratto, usa invece il set "globali" + match_in_service=true.',
     0, 'STD', 100, '#15803d', 1),
    (1, 'esteso', 'Orario esteso',
     'Set attivo SOLO per clienti con tipologia_servizio=EXT (Esteso, lun-ven 06:30-19:30 + sab 06:30-13:00). Da usare per regole che valgono SOLO per il livello di contratto Esteso. Le regole sull''orario lavorativo per qualsiasi contratto vanno in "globali" + match_in_service.',
     0, 'EXT', 100, '#a16207', 1),
    (1, 'h24', 'Servizio H24',
     'Set attivo SOLO per clienti con tipologia_servizio=H24 (servizio 24/7). Da usare per regole specifiche del contratto H24 (es. escalation immediata, tariffe straordinario, integrazione con tecnico reperibile). Le regole "in orario lavorativo del cliente" valgono naturalmente anche per H24 se messe in "globali" + match_in_service=true (un cliente H24 e'' sempre in orario).',
     0, 'H24', 100, '#b91c1c', 1),
    (1, 'nessuno', 'Nessuna copertura',
     'Set attivo SOLO per clienti con tipologia_servizio=NO (nessuna copertura, autorizzazione sempre richiesta). Da usare per regole specifiche del contratto "Nessuna copertura": forzare codice di autorizzazione anche di giorno, blocchi specifici, template "fuori contratto".',
     0, 'NO', 100, '#64748b', 1);

-- =========================================================================
-- 3. ALTER rules + migrazione zero-touch
-- =========================================================================

ALTER TABLE rules ADD COLUMN rule_set_id INTEGER REFERENCES rule_sets(id);

-- Sposta tutte le 22 regole esistenti nel set "globali" (id derivato).
-- Comportamento identico al pre-cambio: un solo set sempre attivo = tutte
-- le regole sempre valutate, esattamente come oggi.
UPDATE rules
   SET rule_set_id = (SELECT id FROM rule_sets WHERE code='globali' AND tenant_id=1)
 WHERE rule_set_id IS NULL;

-- =========================================================================
-- 4. UNIQUE refactoring: includi rule_set_id
--
-- SQLite non supporta DROP UNIQUE su tabella esistente, ma il vincolo
-- originale e' definito tramite CREATE UNIQUE INDEX in M001 (non come
-- table constraint). Drop + ricrea.
-- =========================================================================

DROP INDEX IF EXISTS idx_rules_unique_priority;
DROP INDEX IF EXISTS rules_unique_scope_priority;
DROP INDEX IF EXISTS uq_rules_priority;

-- Nuovo vincolo: priorita' unica DENTRO il set/scope (consente stesse priorita'
-- in set diversi senza collisione).
CREATE UNIQUE INDEX IF NOT EXISTS idx_rules_unique_priority
    ON rules(tenant_id, rule_set_id, scope_type, scope_ref, priority);

-- Index lookup veloce per filtro per set
CREATE INDEX IF NOT EXISTS idx_rules_set_priority
    ON rules(rule_set_id, enabled, priority);

COMMIT;
