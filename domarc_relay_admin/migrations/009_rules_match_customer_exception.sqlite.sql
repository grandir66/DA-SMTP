-- Migration 009 — rules: match su "cliente noto" + "ha eccezione attiva oggi".
--
-- Aggiunge 2 flag tristate su rules per supportare scenari tipici:
--
--   match_known_customer (NULL=any, 1=solo se mittente è in client_domains, 0=solo non censiti)
--     Serve per regole tipo "auto_reply per non censiti" o "ticket solo se cliente noto".
--
--   match_has_exception_today (NULL=any, 1=solo se cliente ha schedule_exception per oggi,
--                              0=nessuna eccezione attiva)
--     Serve per regole "rispetta sempre le eccezioni anche per clienti senza contratto"
--     (es. apertura straordinaria, chiusura per ponte impostata in service_hours).
--
-- Il rule engine del listener leggerà questi flag dal payload /api/v1/relay/rules/active;
-- valori sconosciuti (legacy listener) vengono ignorati (compat in avanti).

ALTER TABLE rules ADD COLUMN match_known_customer INTEGER;
ALTER TABLE rules ADD COLUMN match_has_exception_today INTEGER;
