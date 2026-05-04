-- Migration 022: H24 — codici permanenti cliente, audit usage, mailbox di rientro
-- multi-brand, settings, ALTER su authorization_codes per threading conversazione.
--
-- Vedi piano /tmp/h24-feature-spec-for-relay-session.md (Fase A).
-- Tutte le DDL sono idempotenti.

-- ============================================================================
-- 1. Codici PERMANENTI per cliente (riusabili, accesso H24 contrattuale)
-- ============================================================================
CREATE TABLE IF NOT EXISTS customer_h24_codes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       INTEGER NOT NULL DEFAULT 1 REFERENCES tenants(id),
    code            TEXT    NOT NULL UNIQUE,
    codice_cliente  TEXT    NOT NULL,
    label           TEXT,
    enabled         INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    created_by      TEXT,
    revoked_at      TEXT,
    revoked_by      TEXT,
    revoked_reason  TEXT,
    note            TEXT
);

CREATE INDEX IF NOT EXISTS idx_h24_codes_codcli
    ON customer_h24_codes(codice_cliente)
    WHERE enabled = 1 AND revoked_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_h24_codes_active
    ON customer_h24_codes(code)
    WHERE enabled = 1 AND revoked_at IS NULL;

-- ============================================================================
-- 2. Audit trail utilizzi codici permanenti
-- `reported_to_manager_at` predisposto per rendicontazione futura (vedi Fase E):
-- finché manager non espone endpoint H24, il campo resta NULL e i record si
-- accumulano. Quando l'endpoint sarà attivo, un loop scheduler farà flush
-- batch e popolerà reported_to_manager_at.
-- ============================================================================
CREATE TABLE IF NOT EXISTS customer_h24_codes_usage (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    h24_code_id              INTEGER NOT NULL REFERENCES customer_h24_codes(id) ON DELETE CASCADE,
    used_at                  TEXT NOT NULL DEFAULT (datetime('now')),
    event_uuid               TEXT,
    ticket_id                TEXT,
    from_address             TEXT,
    subject                  TEXT,
    inbound_alias            TEXT,
    reported_to_manager_at   TEXT,
    note                     TEXT
);

CREATE INDEX IF NOT EXISTS idx_h24_usage_code
    ON customer_h24_codes_usage(h24_code_id, used_at DESC);

CREATE INDEX IF NOT EXISTS idx_h24_usage_unreported
    ON customer_h24_codes_usage(used_at)
    WHERE reported_to_manager_at IS NULL;

-- ============================================================================
-- 3. Mailbox di rientro multi-brand
-- source_domain = dominio del MITTENTE della richiesta originale
-- (parsed.from_domain lato listener) → h24_alias da inserire nel mailto:
-- ============================================================================
CREATE TABLE IF NOT EXISTS smtp_relay_h24_targets (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       INTEGER NOT NULL DEFAULT 1 REFERENCES tenants(id),
    source_domain   TEXT    NOT NULL,
    h24_alias       TEXT    NOT NULL,
    urgent_fee_eur  INTEGER,
    note            TEXT,
    enabled         INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE (tenant_id, source_domain)
);

CREATE INDEX IF NOT EXISTS idx_h24_targets_active
    ON smtp_relay_h24_targets(source_domain)
    WHERE enabled = 1;

-- ============================================================================
-- 4. ALTER authorization_codes — aggiunta event_uuid per threading
-- (event_uuid del messaggio che ha generato il codice → consente al worker
-- di ritrovare l'evento originale e mantenere conversation_id sul nuovo
-- ticket H24)
-- ============================================================================
ALTER TABLE authorization_codes ADD COLUMN event_uuid TEXT;

CREATE INDEX IF NOT EXISTS idx_authcodes_event
    ON authorization_codes(event_uuid)
    WHERE event_uuid IS NOT NULL;

-- ============================================================================
-- 5. Settings H24 — chiavi standard con default safe
-- (admin può modificare via UI /settings; il listener legge via sync)
-- ============================================================================
INSERT OR IGNORE INTO settings (key, value, description) VALUES
  ('h24.default_inbound_alias', 'h24@domarc.it',
   'Indirizzo di rientro H24 di fallback quando il dominio mittente non è in smtp_relay_h24_targets.'),
  ('h24.default_urgent_fee_eur', '250',
   'Importo intervento urgente a pagamento (default in EUR + IVA).'),
  ('h24.code_one_shot_ttl_hours', '24',
   'TTL massimo codici monouso. Cap difensivo applicato anche se la regola chiede di più.'),
  ('h24.permanent_code_prefix', 'H24-',
   'Prefisso default per codici permanenti auto-generati.'),
  ('h24.subject_extract_regex', '',
   'Override regex estrazione codice. Vuoto = usa default hardcoded nel modulo h24_code_extractor (sicuro). Settare solo se hai un pattern custom.');
