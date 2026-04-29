-- Migration 015 — AI Error Aggregator F2: fingerprint hex deterministico.
--
-- La migration 012 ha aggiunto `fingerprint_embedding BLOB` (riservato a F4
-- con sentence-transformers). Per F2 base usiamo un fingerprint deterministico
-- (hash SHA256 del subject normalizzato + body excerpt) che cattura varianti
-- semantiche minime senza dipendenze pesanti. Quando il modello embedding
-- viene installato (F4), si potrà migrare gradualmente a similarity > 0.85.

ALTER TABLE ai_error_clusters ADD COLUMN fingerprint_hex TEXT;
CREATE INDEX IF NOT EXISTS idx_ai_clusters_fingerprint ON ai_error_clusters(tenant_id, fingerprint_hex);
