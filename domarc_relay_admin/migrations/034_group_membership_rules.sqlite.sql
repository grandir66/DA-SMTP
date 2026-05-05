-- 034: Group membership rules — auto-assegnazione clienti a gruppi tramite mapping.
--
-- Scopo: rendere il sistema self-contained e indipendente dal gestionale
-- specifico. I gruppi cliente "interni" (built-in) sono concetti del prodotto
-- (vip, secondary, do_not_follow, contract_active, ecc.); le regole di
-- appartenenza mappano valori dei campi del gestionale (qualsiasi schema) a
-- questi gruppi.
--
-- Vantaggi:
--   - Le regole SMTP usano gruppi standardizzati (vip, do_not_follow, ...)
--     indipendentemente dal nome/struttura dei campi nel gestionale.
--   - Bundle "regole + gruppi" portabile tra installazioni.
--   - Cambia gestionale -> rifai solo il mapping, regole intoccate.
--   - Override manuale prevale sempre (is_auto_assigned=0 mai cancellato).

BEGIN;

-- =========================================================================
-- 1. ALTER customer_groups: flag is_builtin per i gruppi del prodotto
-- =========================================================================

ALTER TABLE customer_groups ADD COLUMN is_builtin INTEGER NOT NULL DEFAULT 0;

-- =========================================================================
-- 2. ALTER customer_group_members: flag is_auto_assigned
-- =========================================================================

ALTER TABLE customer_group_members ADD COLUMN is_auto_assigned INTEGER NOT NULL DEFAULT 0;
ALTER TABLE customer_group_members ADD COLUMN auto_rule_id INTEGER;
-- Le membership manuali (is_auto_assigned=0) NON vengono mai toccate
-- dall'auto-assignment del SyncEngine. Solo le auto (is_auto_assigned=1)
-- vengono ricalcolate ad ogni sync.

CREATE INDEX IF NOT EXISTS idx_cgm_auto
    ON customer_group_members(group_id, is_auto_assigned);

-- =========================================================================
-- 3. Tabella group_membership_rules
-- =========================================================================

CREATE TABLE IF NOT EXISTS group_membership_rules (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       INTEGER NOT NULL DEFAULT 1,
    target_group_id INTEGER NOT NULL REFERENCES customer_groups(id) ON DELETE CASCADE,

    -- Campo nel record cliente da osservare (post-mapping canonico OPPURE
    -- nome originale dal record raw del gestionale).
    -- Es: 'tipologia_servizio', 'contract_active', 'priority_flag',
    -- 'cluster', 'is_vip'.
    source_field    TEXT NOT NULL,

    -- Tipo di match
    -- 'equals'      -> source_field == match_value (case-insensitive)
    -- 'contains'    -> match_value e' substring di source_field
    -- 'in_list'     -> source_field e' in match_value (CSV "A,B,C")
    -- 'regex'       -> regex Python su source_field
    -- 'truthy'      -> source_field e' valore "vero" (1, true, Y, S, ...)
    -- 'falsy'       -> source_field e' valore "falso" (0, false, N, ...)
    -- 'not_empty'   -> source_field ha un valore non null/empty
    match_type      TEXT NOT NULL DEFAULT 'equals',

    match_value     TEXT,        -- ignorato per truthy/falsy/not_empty

    -- Scope opzionale: se valorizzato, la rule si applica solo ai record
    -- che arrivano da una specifica customer_sync_source. NULL = globale.
    source_id       INTEGER REFERENCES customer_sync_sources(id) ON DELETE CASCADE,

    -- Priorita' di valutazione tra rules che puntano allo STESSO gruppo
    -- (numero piu' basso = prima). Non rilevante tra gruppi diversi.
    priority        INTEGER NOT NULL DEFAULT 100,

    description     TEXT,
    enabled         INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    created_by      TEXT,
    updated_at      TEXT
);

CREATE INDEX IF NOT EXISTS idx_gmr_tenant_enabled
    ON group_membership_rules(tenant_id, enabled);
CREATE INDEX IF NOT EXISTS idx_gmr_target
    ON group_membership_rules(target_group_id);

-- =========================================================================
-- 4. Seed 8 gruppi built-in
--
-- Questi sono i "concetti del prodotto" su cui le regole SMTP possono
-- contare a prescindere dal gestionale. L'operatore configura le
-- group_membership_rules per popolarli automaticamente al sync.
-- =========================================================================

INSERT OR IGNORE INTO customer_groups
    (tenant_id, code, name, description, color, enabled, is_builtin, created_by)
VALUES
    (1, 'vip', 'Clienti VIP',
     'Top tier: clienti strategici con priorita' || ' alta. ' ||
     'Tipico match: priority_flag=A, cluster=TOP, tipologia=PREMIUM.',
     '#dc2626', 1, 1, 'system'),

    (1, 'secondary', 'Clienti secondari',
     'Tier inferiore al VIP, gestione ordinaria. Tipico match: ' ||
     'priority_flag=B/C, tipologia=BASE.',
     '#f59e0b', 1, 1, 'system'),

    (1, 'do_not_follow', 'Clienti da non seguire',
     'Esclusi dalle regole automatiche (no auto-reply, no ticket auto). ' ||
     'Tipico match: flag exclude, status=DISMESSO/SOSPESO. Le regole ' ||
     'SMTP possono usare questo gruppo per fare opt-out esplicito.',
     '#64748b', 1, 1, 'system'),

    (1, 'contract_active', 'Con contratto attivo',
     'Clienti con contratto in essere. Match canonico: contract_active=1. ' ||
     'Override gestionale possibile via membership manuale.',
     '#15803d', 1, 1, 'system'),

    (1, 'contract_inactive', 'Senza contratto attivo',
     'Clienti censiti ma senza contratto valido. Match: contract_active=0. ' ||
     'Solitamente template auto-reply "always_billable_no_contract".',
     '#6b7280', 1, 1, 'system'),

    (1, 'contract_standard', 'Contratto Standard',
     'Clienti con tipologia STD (lun-ven 08:30-13:00 + 14:30-17:30). ' ||
     'Match: tipologia_servizio=STD.',
     '#3b82f6', 1, 1, 'system'),

    (1, 'contract_extended', 'Contratto Esteso',
     'Clienti con tipologia EXT (lun-ven 06:30-19:30 + sab mattina). ' ||
     'Match: tipologia_servizio=EXT.',
     '#a16207', 1, 1, 'system'),

    (1, 'contract_h24', 'Contratto H24',
     'Clienti con tipologia H24 (servizio 24/7). ' ||
     'Match: tipologia_servizio=H24.',
     '#b91c1c', 1, 1, 'system');

-- =========================================================================
-- 5. Seed delle 4 group_membership_rules ovvie sui campi canonici
--
-- Sono i preset "facili" che funzionano subito su qualsiasi gestionale
-- correttamente mappato. Poi l'operatore aggiunge rules custom su
-- campi specifici del proprio gestionale (priority_flag, cluster, ecc.).
-- =========================================================================

INSERT INTO group_membership_rules
    (tenant_id, target_group_id, source_field, match_type, match_value,
     priority, description, enabled, created_by)
SELECT 1, cg.id, 'contract_active', 'truthy', NULL, 10,
       'Auto: assegna se contract_active e'' true', 1, 'system'
  FROM customer_groups cg WHERE cg.code='contract_active';

INSERT INTO group_membership_rules
    (tenant_id, target_group_id, source_field, match_type, match_value,
     priority, description, enabled, created_by)
SELECT 1, cg.id, 'contract_active', 'falsy', NULL, 10,
       'Auto: assegna se contract_active e'' false', 1, 'system'
  FROM customer_groups cg WHERE cg.code='contract_inactive';

INSERT INTO group_membership_rules
    (tenant_id, target_group_id, source_field, match_type, match_value,
     priority, description, enabled, created_by)
SELECT 1, cg.id, 'tipologia_servizio', 'equals', 'STD', 10,
       'Auto: assegna se tipologia_servizio=STD', 1, 'system'
  FROM customer_groups cg WHERE cg.code='contract_standard';

INSERT INTO group_membership_rules
    (tenant_id, target_group_id, source_field, match_type, match_value,
     priority, description, enabled, created_by)
SELECT 1, cg.id, 'tipologia_servizio', 'equals', 'EXT', 10,
       'Auto: assegna se tipologia_servizio=EXT', 1, 'system'
  FROM customer_groups cg WHERE cg.code='contract_extended';

INSERT INTO group_membership_rules
    (tenant_id, target_group_id, source_field, match_type, match_value,
     priority, description, enabled, created_by)
SELECT 1, cg.id, 'tipologia_servizio', 'equals', 'H24', 10,
       'Auto: assegna se tipologia_servizio=H24', 1, 'system'
  FROM customer_groups cg WHERE cg.code='contract_h24';

COMMIT;
