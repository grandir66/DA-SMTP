---
applies_to: domarc_relay_admin/storage/**/*.py, domarc_relay_admin/customer_sources/**/*.py, services/smtp_listener/relay/storage.py
---

# DB access — direttive

## SQL injection prevention

- **SEMPRE** placeholder parametrizzati (`?` per SQLite, `%s` per psycopg2), MAI f-string sui valori.
- Esempio sbagliato: `cur.execute(f"SELECT * FROM customers WHERE id={cid}")` — VIETATO.
- Esempio corretto: `cur.execute("SELECT * FROM customers WHERE id=?", (cid,))`.
- Nomi tabella/colonna dinamici: whitelist hardcoded, mai concatenare input utente.

## Transazioni

- Operazioni multi-statement (DELETE+INSERT massivi, sync clienti): `BEGIN IMMEDIATE` esplicito, COMMIT a fine, ROLLBACK in except.
- Lettura singola: autocommit di default va bene.
- Mai annidare due BEGIN sulla stessa connessione SQLite (errore lock).

## PG read-only

- I PG `solution` (clienti) e `stormshield` (settings/aliases/domains) sul 192.168.4.41 sono **read-only** dal relay.
- Mai INSERT/UPDATE/DELETE su quei DB. Le scritture solo sull'admin.db locale.
- La tabella autoritativa è `customers` in admin.db (M028), popolata dai provider `customer_sync/`.

## Tenant

- Quasi ogni query: filtrare per `tenant_id=?` (default 1). Mai assumere "single tenant" nei nuovi DAO.
- Tabelle nuove: includere `tenant_id INTEGER NOT NULL DEFAULT 1`.

## N+1 e performance

- Loop di N elementi che fa N query → riscrivi con `WHERE id IN (?, ?, …)` e una sola query.
- Per join complessi: scrivere SQL esplicito, non ORM artigianale.
- Index sui campi di filtro frequente. Aggiungi `CREATE INDEX IF NOT EXISTS` in migration.

## Connection management

- Connessione SQLite: ottenuta via `SqliteStorage._connect()`, mai aprire `sqlite3.connect(...)` ad-hoc nei route.
- PG: pool via `psycopg2.pool` se serve concorrenza, altrimenti connection per-request chiusa in `finally`.
- Sempre `cur.close()` (o context manager `with conn.cursor() as cur:`).
