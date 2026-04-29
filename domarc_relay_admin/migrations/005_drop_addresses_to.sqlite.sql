-- Migration 005 — rimozione `addresses_to` (destinatari noti).
--
-- L'anagrafica indirizzi del relay ha senso solo per i **mittenti** esterni
-- (riconoscimento, mappatura a codcli, blacklist). I destinatari sono i
-- nostri clienti e vivono già in `customers` (PG) + `routes` SMTP. Tenere
-- una tabella destinatari separata genera duplicazione e confusione.

DROP INDEX IF EXISTS idx_addresses_to_tenant;
DROP INDEX IF EXISTS idx_addresses_to_domain;
DROP TABLE IF EXISTS addresses_to;
