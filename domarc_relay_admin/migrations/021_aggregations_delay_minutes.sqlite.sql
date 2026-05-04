-- Migration 021: campo `delay_minutes` per error_aggregations.
--
-- Semantica:
--   * `delay_minutes IS NULL` (default) → comportamento legacy count-based:
--       apre ticket quando `current_count >= threshold` entro `window_hours`.
--   * `delay_minutes` valorizzato → comportamento timer-based:
--       apre ticket solo se la fingerprint NON viene resettata (reset_*_regex)
--       entro N minuti dalla prima occorrenza. `threshold` e `window_hours`
--       vengono ignorati. Pensato per alert monitoring (Cloudtik, syslog,
--       ICMP probes) che spesso si auto-risolvono.

ALTER TABLE error_aggregations ADD COLUMN delay_minutes INTEGER;
