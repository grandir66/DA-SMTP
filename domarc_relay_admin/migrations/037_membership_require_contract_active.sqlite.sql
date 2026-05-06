-- Migration 037 — group_membership_rules.require_contract_active
--
-- Le rules che derivano un gruppo "contract_*" dalla tipologia_servizio
-- (es. tipologia_servizio in_list 'STD,standard' → contract_standard) devono
-- escludere le anagrafiche con contract_active=0 — clienti senza contratto
-- attivo non sono "veri" clienti del segmento.
--
-- Aggiungo un flag boolean alla rule per richiedere contract_active=1 come
-- pre-filtro AND. evaluate_membership_rules in sqlite_impl.py salta la rule
-- per i record con contract_active=0 quando questo flag è ON.
--
-- Le rules contract_active=truthy/falsy (1, 2) restano con flag=0: sono già
-- pre-filtrate dalla loro stessa logica.

ALTER TABLE group_membership_rules
    ADD COLUMN require_contract_active INTEGER NOT NULL DEFAULT 0;

-- Default: tutte le rules che usano source_field='tipologia_servizio'
-- richiedono contract_active=1.
UPDATE group_membership_rules
SET require_contract_active = 1
WHERE source_field = 'tipologia_servizio';
