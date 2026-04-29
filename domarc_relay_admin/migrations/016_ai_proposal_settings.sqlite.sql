-- Migration 016 — F3.5 Rule Proposer: settings runtime per il learning loop.
--
-- Il proposer scansiona `ai_decisions`, raggruppa per pattern simili (intent +
-- subject normalizzato + from_domain) e genera proposte di regole statiche
-- in `ai_rule_proposals` quando un cluster soddisfa due criteri:
--
-- 1. Almeno N decisioni coerenti (`ai_proposal_min_decisions`, default 20).
--    Un volume minimo per evitare proposte basate su pochi sample.
--
-- 2. Classificazione dominante consistente (`ai_proposal_consistency_threshold`,
--    default 0.80 = 80%). Cioè ≥80% delle decisioni del cluster condividono
--    lo stesso intent e suggested_action.
--
-- Oltre questi due, viene calcolato un `confidence` aggregato (media delle
-- confidence delle decisioni dominanti) per ranking nella UI proposals.

INSERT OR IGNORE INTO settings (key, value, description) VALUES
    ('ai_proposal_min_decisions', '20',
     'F3.5 Rule Proposer: numero minimo di decisioni IA coerenti per generare una proposta di regola statica.'),
    ('ai_proposal_consistency_threshold', '0.80',
     'F3.5 Rule Proposer: percentuale minima (0..1) di decisioni con stessa classification per emettere proposta.'),
    ('ai_proposal_window_days', '14',
     'F3.5 Rule Proposer: finestra temporale in giorni delle decisioni considerate per la generazione proposte.');
