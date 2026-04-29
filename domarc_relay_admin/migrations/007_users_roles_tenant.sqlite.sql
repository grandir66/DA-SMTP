-- Migration 007 — sistema utenti con 4 ruoli + scoping per tenant.
--
-- Nuovi ruoli (gerarchia: readonly < tech < admin < superadmin):
--   superadmin  — gestisce tutti i tenant, switch contesto, CRUD utenti globale
--   admin       — gestisce un singolo tenant (CRUD utenti del proprio tenant,
--                 regole, template, profili, settings)
--   tech        — operativo sul tenant: edit regole/template/orari, ack occurrences
--   readonly    — visualizza dashboard, eventi, regole/template (solo lettura)
--
-- Migration legacy:
--   role='admin'    → 'superadmin' (era l'unico admin globale pre-migration)
--   role='operator' → 'tech'
--   role='viewer'   → 'readonly'
--
-- users.tenant_id: NULLABLE.
--   superadmin: tenant_id=NULL (vede tutto)
--   admin/tech/readonly: tenant_id valorizzato (limitato al loro tenant)

BEGIN;

-- 1. Aggiungi tenant_id su users
ALTER TABLE users ADD COLUMN tenant_id INTEGER REFERENCES tenants(id);

-- 2. Indici utili
CREATE INDEX IF NOT EXISTS idx_users_tenant ON users(tenant_id);
CREATE INDEX IF NOT EXISTS idx_users_role ON users(role);

-- 3. Mappa ruoli legacy → nuovi
UPDATE users SET role = 'superadmin' WHERE role = 'admin';
UPDATE users SET role = 'tech'       WHERE role = 'operator';
UPDATE users SET role = 'readonly'   WHERE role = 'viewer';

-- 4. user_tenant_roles: anche qui mappa ruoli legacy
UPDATE user_tenant_roles SET role = 'admin'    WHERE role = 'admin';
UPDATE user_tenant_roles SET role = 'tech'     WHERE role = 'operator';
UPDATE user_tenant_roles SET role = 'readonly' WHERE role = 'viewer';

COMMIT;
