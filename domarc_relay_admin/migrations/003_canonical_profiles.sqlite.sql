-- Migration 003 — allineamento profili orari canonici (4) ai valori del gestionale Domarc.
--
-- Il gestionale (manager) espone in `customer_availability_types` 4 profili canonici:
--   STD = Standard           lun-ven 08:30-13:00 + 14:30-17:30
--   EXT = Esteso             lun-ven 06:30-19:30, sab 06:30-13:00
--   H24 = 24/7
--   NO  = Nessuna copertura  (mai in servizio, autorizzazione sempre)
--
-- La migration 001 aveva creato 3 profili con nomi/orari approssimativi
-- (`standard` 8-13/14-18, `extended` 8-20 + sab 9-13, `h24`). Qui:
--   1. Aggiunge colonna `code` UNIQUE (built-in identificati per code, non nome)
--   2. Aggiunge colonna `details` (descrizione lunga del profilo)
--   3. Aggiunge colonna `requires_authorization_always` (flag profilo NO)
--   4. Aggiunge colonna `authorize_outside_hours` (flag autorizzabile fuori orario)
--   5. Aggiorna i 3 built-in con code/orari/flag corretti
--   6. Inserisce 4° profilo `NO` se mancante

BEGIN;

ALTER TABLE service_hours_profiles ADD COLUMN code TEXT;
ALTER TABLE service_hours_profiles ADD COLUMN details TEXT;
ALTER TABLE service_hours_profiles ADD COLUMN requires_authorization_always INTEGER NOT NULL DEFAULT 0;
ALTER TABLE service_hours_profiles ADD COLUMN authorize_outside_hours INTEGER NOT NULL DEFAULT 1;
ALTER TABLE service_hours_profiles ADD COLUMN exclude_holidays INTEGER NOT NULL DEFAULT 1;

-- STD: standard
UPDATE service_hours_profiles SET
    code = 'STD',
    name = 'Standard',
    description = 'Lun-Ven 08:30-13:00 + 14:30-17:30, festività escluse',
    details = 'Finestra di servizio standard. Lun-Ven mattina e pomeriggio.',
    schedule = '{"mon":[["08:30","13:00"],["14:30","17:30"]],"tue":[["08:30","13:00"],["14:30","17:30"]],"wed":[["08:30","13:00"],["14:30","17:30"]],"thu":[["08:30","13:00"],["14:30","17:30"]],"fri":[["08:30","13:00"],["14:30","17:30"]],"sat":[],"sun":[]}',
    exclude_holidays = 1,
    requires_authorization_always = 0,
    authorize_outside_hours = 1
 WHERE id = 1;

-- EXT: esteso
UPDATE service_hours_profiles SET
    code = 'EXT',
    name = 'Esteso',
    description = 'Lun-Ven 06:30-19:30 · Sab 06:30-13:00, festività escluse',
    details = 'Finestra di servizio estesa rispetto a STD.',
    schedule = '{"mon":[["06:30","19:30"]],"tue":[["06:30","19:30"]],"wed":[["06:30","19:30"]],"thu":[["06:30","19:30"]],"fri":[["06:30","19:30"]],"sat":[["06:30","13:00"]],"sun":[]}',
    exclude_holidays = 1,
    requires_authorization_always = 0,
    authorize_outside_hours = 1
 WHERE id = 2;

-- H24: 24/7, festività trattate come fuori orario (autorizzazione richiesta)
UPDATE service_hours_profiles SET
    code = 'H24',
    name = 'H24',
    description = '24/7, festività trattate come fuori orario (autorizzazione richiesta)',
    details = 'Copertura continua 24 ore su 24, 7 giorni su 7.',
    schedule = '{"mon":[["00:00","23:59"]],"tue":[["00:00","23:59"]],"wed":[["00:00","23:59"]],"thu":[["00:00","23:59"]],"fri":[["00:00","23:59"]],"sat":[["00:00","23:59"]],"sun":[["00:00","23:59"]]}',
    exclude_holidays = 1,
    requires_authorization_always = 0,
    authorize_outside_hours = 1
 WHERE id = 3;

-- NO: nessuna copertura
INSERT OR IGNORE INTO service_hours_profiles
    (id, tenant_id, code, name, description, details, schedule, holidays,
     holidays_auto, is_builtin, enabled, exclude_holidays,
     requires_authorization_always, authorize_outside_hours, updated_by)
VALUES
(4, NULL, 'NO', 'Nessuna copertura',
 'Mai in servizio · ogni richiesta richiede autorizzazione preventiva firmata',
 'Nessuna finestra di servizio attiva.',
 '{"mon":[],"tue":[],"wed":[],"thu":[],"fri":[],"sat":[],"sun":[]}',
 '[]', 0, 1, 1, 0, 1, 1, 'system_seed');

-- Indice unique su (tenant_id, code) per built-in/custom
CREATE UNIQUE INDEX IF NOT EXISTS idx_service_hours_profiles_tenant_code
    ON service_hours_profiles(tenant_id, code) WHERE code IS NOT NULL;

COMMIT;
