-- Migration 020: aggiunge `contract_type` a customers_pg_cache
--
-- Motivazione: l'inventario tabelle PG (2026-05-04) ha mostrato che
-- `customer_contract_types` è popolata su solution (7 righe: STD/ADV/...) e
-- viene già JOINata in `_load_customer_settings` per estrarre `contract.code`,
-- ma il valore veniva scartato all'INSERT perché la colonna mancava.
--
-- Aggiungo la colonna come TEXT NULL: NULL = sconosciuto (cliente senza
-- riga in customer_settings o settings.contract_type_id NULL).

ALTER TABLE customers_pg_cache ADD COLUMN contract_type TEXT;

CREATE INDEX IF NOT EXISTS idx_customers_pg_cache_contract_type
    ON customers_pg_cache(contract_type);
