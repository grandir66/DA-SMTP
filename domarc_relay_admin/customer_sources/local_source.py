"""Customer source backend `local`: legge dalla tabella autoritativa `customers`
(post Migration 028, rinominata da `customers_pg_cache`).

Differenza rispetto al vecchio backend `postgres`:
  - Nessun thread di sync interno (la tabella e' alimentata da SyncEngine
    via customer_sync_sources, non da questo modulo).
  - Nessuna dipendenza da psycopg2 in runtime read.
  - Schema output identico (Customer dataclass) -> il listener non si
    accorge del cambio.

Il backend `postgres` legacy resta nel registry per compat: chi ha
`customer_source.backend=postgres` continua a funzionare durante il
deploy, ma a regime tutti useranno `local`.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any

from .base import Customer, CustomerSource

logger = logging.getLogger(__name__)


class LocalCustomerSource(CustomerSource):
    """Backend customer source che legge dalla tabella `customers` locale."""

    def __init__(self, app_config_or_storage) -> None:
        # Compat: accetta sia AppConfig (con .db_path) che SqliteStorage
        # (che usa attributo privato `_path` di tipo Path).
        if hasattr(app_config_or_storage, "db_path"):
            self._db_path = app_config_or_storage.db_path
            self._storage = None
        else:
            self._storage = app_config_or_storage
            # SqliteStorage ha attributo `_path` (Path)
            p = getattr(self._storage, "_path", None) \
                or getattr(self._storage, "db_path", None) \
                or getattr(self._storage, "_db_path", None)
            self._db_path = str(p) if p is not None else None
        if self._db_path is None:
            raise RuntimeError("LocalCustomerSource: db_path non risolvibile dall'oggetto storage/config")

    # ============================================================ Pubblica

    def list_customers(self) -> list[Customer]:
        rows = self._fetch_rows()
        return [self._row_to_customer(r) for r in rows]

    def get_by_codcli(self, codcli: str) -> Customer | None:
        rows = self._fetch_rows(codcli=codcli)
        if not rows:
            return None
        return self._row_to_customer(rows[0])

    def health(self) -> dict[str, Any]:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        try:
            r_count = conn.execute("SELECT COUNT(*) FROM customers").fetchone()[0]
            r_active = conn.execute(
                "SELECT COUNT(*) FROM customers WHERE contract_active=1"
            ).fetchone()[0]
            r_last_sync = conn.execute(
                "SELECT MAX(last_synced_at) FROM customers"
            ).fetchone()[0]
            r_sources = conn.execute(
                "SELECT COUNT(*) FROM customer_sync_sources WHERE enabled=1"
            ).fetchone()[0]
            r_last_run = conn.execute(
                """SELECT id, source_id, status, started_at, finished_at,
                          n_inserted, n_updated, n_errored
                     FROM customer_sync_runs
                    ORDER BY started_at DESC LIMIT 1"""
            ).fetchone()
            last_run = dict(r_last_run) if r_last_run else None
        finally:
            conn.close()

        age_seconds: int | None = None
        if r_last_sync:
            try:
                last_dt = datetime.fromisoformat(str(r_last_sync).replace("Z", "+00:00"))
                if last_dt.tzinfo is None:
                    last_dt = last_dt.replace(tzinfo=timezone.utc)
                age_seconds = int((datetime.now(timezone.utc) - last_dt).total_seconds())
            except (ValueError, TypeError):
                pass

        return {
            "backend": "local",
            "cache_count": r_count,
            "active_count": r_active,
            "last_sync": r_last_sync,
            "last_sync_age_seconds": age_seconds,
            "enabled_sources": r_sources,
            "last_run": last_run,
        }

    # ============================================================ Helpers

    def _fetch_rows(self, codcli: str | None = None) -> list[dict]:
        conn = sqlite3.connect(self._db_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        try:
            if codcli:
                rows = conn.execute(
                    "SELECT * FROM customers WHERE codcli = ?",
                    (codcli.strip().upper(),)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM customers ORDER BY ragione_sociale"
                ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def _row_to_customer(self, r: dict) -> Customer:
        try:
            domains = json.loads(r.get("domains_json") or "[]")
        except (TypeError, ValueError):
            domains = []
        try:
            aliases = json.loads(r.get("aliases_json") or "[]")
        except (TypeError, ValueError):
            aliases = []
        try:
            sh = json.loads(r.get("service_hours_json") or "{}") if r.get("service_hours_json") else {}
        except (TypeError, ValueError):
            sh = {}
        return Customer(
            codice_cliente=r["codcli"],
            ragione_sociale=r.get("ragione_sociale") or "",
            domains=domains,
            aliases=aliases,
            is_active=bool(r.get("contract_active", 1)),
            tipologia_servizio=r.get("tipologia_servizio") or "standard",
            holidays=(sh.get("holidays") or []),
            schedule=(sh.get("schedule") or None),
            contract_expiry_date=r.get("contract_expiry"),
            contract_type=r.get("contract_type"),
            profile_description=None,
        )
