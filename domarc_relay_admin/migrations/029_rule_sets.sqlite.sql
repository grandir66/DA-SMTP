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

INSERT OR IGNORE INTO rule_sets
    (tenant_id, code, name, description,
     is_always_active, profile_code, evaluation_order, color, is_builtin)
VALUES
    (1, 'globali', 'Sempre attive (globali)',
     'Regole valutate per ogni mail, indipendentemente dal profilo orario del cliente. Tipico uso: alert CloudTIK, sistemi automatici che aprono ticket, apprendimento AI, privacy bypass, regole catch-all.',
     1, NULL, 10, '#1e40af', 1),
    (1, 'standard', 'Orario standard',
     'Regole attive solo per clienti con profilo Standard (STD: lun-ven 08:30-13:00 + 14:30-17:30).',
     0, 'STD', 100, '#15803d', 1),
    (1, 'esteso', 'Orario esteso',
     'Regole attive solo per clienti con profilo Esteso (EXT: lun-ven 06:30-19:30 + sab 06:30-13:00).',
     0, 'EXT', 100, '#a16207', 1),
    (1, 'h24', 'Servizio H24',
     'Regole attive solo per clienti con profilo H24 (sempre in servizio).',
     0, 'H24', 100, '#b91c1c', 1),
    (1, 'nessuno', 'Nessuna copertura',
     'Regole attive solo per clienti con profilo NO (autorizzazione sempre richiesta).',
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
