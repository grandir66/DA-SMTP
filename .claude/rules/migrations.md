---
applies_to: domarc_relay_admin/migrations/*.sqlite.sql
---

# Migration SQLite — direttive

## Idempotenza

- Ogni statement deve essere eseguibile 2+ volte senza errori.
- `CREATE TABLE IF NOT EXISTS`, `CREATE INDEX IF NOT EXISTS`, `INSERT OR IGNORE`.
- Per ALTER (SQLite non supporta `IF NOT EXISTS` su colonna): gestire la mini-migration in Python dentro `storage/sqlite_impl.py` con try/except che cattura `sqlite3.OperationalError` su "duplicate column name".

## Numerazione

- File `NNN_descrizione_concisa.sqlite.sql`, numerazione sequenziale a 3 cifre.
- Mai gap: se l'ultima è `036`, la prossima è `037`.
- Conflitto di merge su numero: aggiornare il file proprio al numero successivo, MAI sovrascrivere quello di un altro.

## Cosa NON fare

- **Mai** `DROP TABLE` o `DROP COLUMN` (SQLite < 3.35 non supporta drop column clean; rollback opaco).
- **Mai** modificare una migration già rilasciata (cambia hash → desync con `_migrations`). Per correzioni: nuova migration successiva.
- **Mai** seed di dati con SELECT … WHERE NOT EXISTS (race-prone). Usa `INSERT OR IGNORE` con chiave naturale UNIQUE.

## Schema convention

- Ogni tabella nuova: `id INTEGER PRIMARY KEY AUTOINCREMENT`, `tenant_id INTEGER NOT NULL DEFAULT 1`, `created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP`, `updated_at TIMESTAMP` se mutevole.
- Timestamp in UTC (`CURRENT_TIMESTAMP` di SQLite è UTC ISO).
- Boolean → `INTEGER NOT NULL DEFAULT 0` (0/1).
- Tristate UI (sì/no/ignora) → `INTEGER` nullable, NULL = "ignora", 0 = no, 1 = sì.

## Documentazione

- Header del file con: numero, data, scopo in 1 riga.
- Se la migration richiede backfill non banale: commento `-- POST-APPLY:` con riferimento alla funzione Python che lo esegue.
