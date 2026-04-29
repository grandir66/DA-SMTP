"""Customer source da tabella SQLite locale (gestita via UI CRUD).

La tabella vive nello stesso DB dell'admin (default) o in un DB dedicato.
Schema:
    customers (codice_cliente PK, ragione_sociale, tipologia_servizio,
               contract_active, domains TEXT JSON, aliases TEXT JSON, notes,
               holidays TEXT JSON, schedule_overrides TEXT JSON)

Default config: path uguale a `db_path` dell'admin (un solo file).
"""
from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any

from .base import Customer, CustomerSource

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS customers (
    codice_cliente       TEXT PRIMARY KEY,
    ragione_sociale      TEXT,
    tipologia_servizio   TEXT NOT NULL DEFAULT 'standard',
    contract_active      INTEGER NOT NULL DEFAULT 1,
    domains              TEXT NOT NULL DEFAULT '[]',
    aliases              TEXT NOT NULL DEFAULT '[]',
    notes                TEXT,
    holidays             TEXT,
    schedule_overrides   TEXT,
    created_at           TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at           TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


class SqliteCustomerSource(CustomerSource):
    def __init__(self, db_path: str):
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self):
        conn = sqlite3.connect(str(self._path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            conn.commit()

    def _row_to_customer(self, row) -> Customer:
        return Customer(
            codice_cliente=str(row["codice_cliente"]),
            ragione_sociale=row["ragione_sociale"],
            tipologia_servizio=row["tipologia_servizio"] or "standard",
            contract_active=bool(row["contract_active"]),
            domains=json.loads(row["domains"] or "[]"),
            aliases=json.loads(row["aliases"] or "[]"),
            notes=row["notes"],
            holidays=json.loads(row["holidays"]) if row["holidays"] else None,
            schedule_overrides=json.loads(row["schedule_overrides"]) if row["schedule_overrides"] else None,
        )

    def list_customers(self) -> list[Customer]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM customers ORDER BY codice_cliente").fetchall()
            return [self._row_to_customer(r) for r in rows]

    def get_by_codcli(self, codcli: str) -> Customer | None:
        codcli = (codcli or "").strip().upper()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM customers WHERE codice_cliente = ?", (codcli,)
            ).fetchone()
            return self._row_to_customer(row) if row else None

    def upsert_customer(self, c: Customer) -> None:
        """Solo per backend SQLite: scrittura via UI."""
        with self._connect() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO customers
                   (codice_cliente, ragione_sociale, tipologia_servizio,
                    contract_active, domains, aliases, notes, holidays, schedule_overrides,
                    updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))""",
                (c.codice_cliente.upper(), c.ragione_sociale, c.tipologia_servizio,
                 1 if c.contract_active else 0,
                 json.dumps(c.domains), json.dumps(c.aliases),
                 c.notes,
                 json.dumps(c.holidays) if c.holidays is not None else None,
                 json.dumps(c.schedule_overrides) if c.schedule_overrides is not None else None),
            )
            conn.commit()

    def delete_customer(self, codcli: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM customers WHERE codice_cliente = ?",
                         ((codcli or "").strip().upper(),))
            conn.commit()

    def health(self) -> dict[str, Any]:
        with self._connect() as conn:
            n = conn.execute("SELECT COUNT(*) FROM customers").fetchone()[0]
        return {"backend": "sqlite", "path": str(self._path), "count": int(n)}
