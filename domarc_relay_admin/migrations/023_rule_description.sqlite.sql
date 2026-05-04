-- Migration 023: campo `description` per documentare uso/funzione di ogni regola.
-- Visualizzato nella UI del form rule come textarea + tooltip nelle liste.
-- Nessun impatto sul listener (campo solo documentale).

ALTER TABLE rules ADD COLUMN description TEXT;
