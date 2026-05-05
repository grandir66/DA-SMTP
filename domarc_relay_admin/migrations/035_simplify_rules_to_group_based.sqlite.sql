-- 035: Semplificazione regole — passa da rule_set per profilo a match_customer_groups.
--
-- Razionale: con M034 i gruppi cliente built-in (contract_active, contract_h24,
-- contract_standard, ecc.) sono auto-popolati al sync da regole di mapping.
-- Quindi ora UN SOLO meccanismo di filtro per contratto sufficiente:
-- match_customer_groups. I rule_set per profilo (standard/esteso/h24/nessuno)
-- diventano ridondanti -> deprecati.
--
-- Cambiamenti:
-- 1. ALTER rule_sets ADD is_deprecated (flag per nascondere dalla UI)
-- 2. Migrazione automatica: per ogni regola in rule_set='standard' (o equiv):
--      - aggiunge match_customer_groups='contract_standard' (o equiv)
--      - sposta rule_set_id a quello di 'globali'
-- 3. Mark deprecated i 4 rule_set per profilo (NON eliminati per safety:
--    se qualcuno avesse override custom o scrivesse regole nel set deprecato,
--    funzionerebbero comunque)
--
-- Idempotente: se il match_customer_groups gia' contiene il gruppo target,
-- non viene aggiunto duplicato.

BEGIN;

-- =========================================================================
-- 1. ALTER rule_sets: flag is_deprecated
-- =========================================================================

ALTER TABLE rule_sets ADD COLUMN is_deprecated INTEGER NOT NULL DEFAULT 0;

-- =========================================================================
-- 2. Migrazione regole: rule_set per profilo -> match_customer_groups
--
-- Mapping rule_set.code -> customer_groups.code:
--   standard -> contract_standard
--   esteso   -> contract_extended
--   h24      -> contract_h24
--   nessuno  -> contract_inactive  (NO = nessuna copertura ~ contract_inactive)
-- =========================================================================

-- Per la sicurezza creo una tabella temporanea di mapping
CREATE TEMP TABLE _m035_rule_set_to_group (
    rule_set_code TEXT,
    target_group_code TEXT
);
INSERT INTO _m035_rule_set_to_group VALUES
    ('standard', 'contract_standard'),
    ('esteso',   'contract_extended'),
    ('h24',      'contract_h24'),
    ('nessuno',  'contract_inactive');

-- Per ogni regola in un rule_set per profilo:
--   - se match_customer_groups e' NULL/vuoto: imposta al gruppo target
--   - se gia' contiene altri gruppi: appende solo se non gia' presente
--   - sposta rule_set_id al set 'globali'
UPDATE rules
SET match_customer_groups = (
    SELECT
        CASE
            -- Caso 1: vuoto/NULL -> imposta al gruppo target
            WHEN COALESCE(NULLIF(TRIM(rules.match_customer_groups), ''), '') = ''
                THEN m.target_group_code
            -- Caso 2: gia' contiene il target_group_code (idempotenza)
            WHEN ',' || rules.match_customer_groups || ',' LIKE '%,' || m.target_group_code || ',%'
                THEN rules.match_customer_groups
            -- Caso 3: contiene altri gruppi -> appendi
            ELSE rules.match_customer_groups || ',' || m.target_group_code
        END
    FROM _m035_rule_set_to_group m
    JOIN rule_sets rs ON rs.code = m.rule_set_code
    WHERE rs.id = rules.rule_set_id
    LIMIT 1
)
WHERE rule_set_id IN (
    SELECT rs.id FROM rule_sets rs
    JOIN _m035_rule_set_to_group m ON m.rule_set_code = rs.code
);

-- Sposta tutte le regole migrate al set 'globali'
UPDATE rules
SET rule_set_id = (SELECT id FROM rule_sets WHERE code='globali' AND tenant_id=1)
WHERE rule_set_id IN (
    SELECT rs.id FROM rule_sets rs
    JOIN _m035_rule_set_to_group m ON m.rule_set_code = rs.code
);

DROP TABLE _m035_rule_set_to_group;

-- =========================================================================
-- 3. Mark deprecated i 4 rule_set per profilo
--
-- I set restano in tabella (non li eliminiamo) ma vengono nascosti dalla
-- UI di selezione e badge "deprecated" nelle pagine che li visualizzano.
-- L'operatore puo' ancora vederli/usarli da pagina /rule-sets/ se serve.
-- =========================================================================

UPDATE rule_sets
SET is_deprecated = 1
WHERE code IN ('standard', 'esteso', 'h24', 'nessuno');

-- Aggiorno descrizioni per chiarire la deprecazione
UPDATE rule_sets
SET description = '[DEPRECATO M035] ' || COALESCE(description, '') ||
                  ' Da usare match_customer_groups con i gruppi built-in ' ||
                  '(contract_standard/extended/h24/inactive) invece del set ' ||
                  'per profilo. Le regole sono state migrate automaticamente.'
WHERE code IN ('standard', 'esteso', 'h24', 'nessuno');

COMMIT;
