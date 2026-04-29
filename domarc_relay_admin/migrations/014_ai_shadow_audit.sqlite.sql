-- Migration 014 — uscita controllata da shadow mode (F3 AI Assistant).
--
-- Aggiunge:
-- 1. Setting `ai_apply_min_confidence` (default 0.85): solo decisioni con
--    confidence >= soglia vengono applicate in live mode (le altre tornano
--    shadow per safety).
-- 2. Tabella `ai_shadow_audit` per il log delle transizioni shadow ↔ live
--    con conteggio delle decisioni viste prima dello switch (per assicurare
--    osservazione minima prima di passare in produzione).

INSERT OR IGNORE INTO settings (key, value, description) VALUES
    ('ai_apply_min_confidence', '0.85',
     'Soglia minima confidence per applicare le decisioni IA in live mode (0..1). Sotto soglia: trattate come shadow.'),
    ('ai_shadow_min_decisions_before_live', '50',
     'N. minimo di decisioni shadow osservate prima di poter passare a live mode (anti-rush).');

CREATE TABLE IF NOT EXISTS ai_shadow_audit (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       INTEGER NOT NULL DEFAULT 1 REFERENCES tenants(id),
    transition      TEXT NOT NULL CHECK (transition IN ('shadow_to_live', 'live_to_shadow')),
    actor           TEXT,
    decisions_seen  INTEGER NOT NULL DEFAULT 0,
    avg_confidence  REAL,
    notes           TEXT,
    at              TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_ai_shadow_audit_at ON ai_shadow_audit(at DESC);
