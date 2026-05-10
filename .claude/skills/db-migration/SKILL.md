---
name: db-migration
description: Crea una nuova migration SQLite idempotente per schema admin.db
---

# DB Migration (SQLite admin.db)

Le migration sono file `domarc_relay_admin/migrations/NNN_descrizione.sqlite.sql`, numerate progressivamente, applicate **una sola volta** al primo `init_db=True` di `create_app()` (chiama `apply_migrations`). La tabella `_migrations` traccia le versioni applicate.

## Comando standard

```bash
# 1. Trova il prossimo numero
ls domarc_relay_admin/migrations/*.sqlite.sql | sort | tail -3
# Esempio output: 036_thread_tracking_rfc2822.sqlite.sql → next = 037

# 2. Crea il file
NEXT="037"
SLUG="describe_what_it_does"
cat > "domarc_relay_admin/migrations/${NEXT}_${SLUG}.sqlite.sql" <<'EOF'
-- Migration NNN: <descrizione concisa>
-- Data: YYYY-MM-DD
-- Scopo: <perché serve questa migration>

-- Esempio: aggiunta colonna idempotente
-- (usare CREATE TABLE IF NOT EXISTS, INSERT OR IGNORE, ALTER con try/except in mini-migration Python se serve)

CREATE TABLE IF NOT EXISTS nuova_tabella (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id INTEGER NOT NULL DEFAULT 1,
    nome TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_nuova_tabella_tenant ON nuova_tabella(tenant_id);

-- Per ALTER su tabella esistente (SQLite non supporta IF NOT EXISTS su column):
-- gestire la mini-migration in Python dentro storage/sqlite_impl.py con try/except
EOF

# 3. Applica su DB di sviluppo / test
.venv/bin/pytest tests/test_migrations.py -x   # se esiste

# 4. Su VM produzione: il restart NON riapplica automaticamente migration già in _migrations.
#    Per ricreare DB pulito (solo dev): rm /var/lib/domarc-smtp-relay-admin/admin.db && systemctl restart domarc-smtp-relay-admin
```

## Quando NON usare

- Modifica dati runtime (settings, regole, customer): farlo via UI o seed script in `scripts/`, NON via migration.
- Schema del listener (`relay.db`): le tabelle del listener sono gestite da `services/smtp_listener/relay/storage.py:_init_schema()`, non da `migrations/`.

## Anti-regressione

- **Idempotenza assoluta**: ogni statement deve poter essere eseguito 2+ volte senza errori (`IF NOT EXISTS`, `INSERT OR IGNORE`, ecc.).
- Mai `DROP TABLE` o `ALTER TABLE … DROP COLUMN` in migration (SQLite non supporta DROP COLUMN < 3.35 cleanly; rompe rollback).
- Numerazione sequenziale: se due dev creano `037_x.sqlite.sql` e `037_y.sqlite.sql` in parallelo, allinea a `037` e `038` PRIMA del merge.
- Ogni nuova tabella eredita `tenant_id INTEGER NOT NULL DEFAULT 1` per multi-tenant futuro.
- Seed di dati iniziali via `INSERT OR IGNORE` con chiave naturale (mai SELECT … WHERE NOT EXISTS, è race-prone).
- Verifica post-apply: `sqlite3 admin.db ".schema nuova_tabella"` e `SELECT * FROM _migrations ORDER BY id DESC LIMIT 5;`.
