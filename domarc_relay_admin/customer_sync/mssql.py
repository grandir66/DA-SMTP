"""Provider MSSQL via pyodbc (per gestionali Microsoft, es. Domarc 4.4).

config_json:
    {
      "server": "192.168.x.x\\\\SQLEXPRESS",   # o "host,port"
      "database": "DBNAME",
      "user": "...",
      "password": "...",
      "driver": "ODBC Driver 18 for SQL Server",  # opzionale, default
      "encrypt": "yes",
      "trust_server_certificate": "yes"
    }

query: SELECT che ritorna 1 riga per cliente.

Richiede:
  - python: pyodbc (in pyproject opzionale, gruppo 'mssql')
  - sistema: ODBC Driver Microsoft (msodbcsql18 + unixodbc)
"""
from __future__ import annotations

import logging
from typing import Any, Iterator

from .base import CustomerSyncProvider, FetchedRecord, ProviderConnectionError

logger = logging.getLogger(__name__)


def _import_pyodbc():
    try:
        import pyodbc
        return pyodbc
    except ImportError as exc:
        raise ProviderConnectionError(
            "pyodbc non installato (richiesto per provider mssql). "
            "Installa con: pip install 'domarc-smtp-relay-admin[mssql]' "
            "+ driver ODBC Microsoft."
        ) from exc


class MSSQLProvider(CustomerSyncProvider):

    def __init__(self, *, config: dict[str, Any], query: str | None) -> None:
        self._config = config or {}
        self._query = query

    def _build_connstr(self) -> str:
        cfg = self._config
        driver = cfg.get("driver") or "ODBC Driver 18 for SQL Server"
        parts = [f"Driver={{{driver}}}"]
        if cfg.get("server"):
            parts.append(f"Server={cfg['server']}")
        if cfg.get("database"):
            parts.append(f"Database={cfg['database']}")
        if cfg.get("user"):
            parts.append(f"Uid={cfg['user']}")
        if cfg.get("password") is not None:
            parts.append(f"Pwd={cfg['password']}")
        if cfg.get("encrypt"):
            parts.append(f"Encrypt={cfg['encrypt']}")
        if cfg.get("trust_server_certificate"):
            parts.append(f"TrustServerCertificate={cfg['trust_server_certificate']}")
        if cfg.get("trusted_connection"):
            parts.append(f"Trusted_Connection={cfg['trusted_connection']}")
        parts.append("Connection Timeout=10")
        return ";".join(parts)

    def fetch(self) -> Iterator[FetchedRecord]:
        if not self._query:
            raise ValueError("MSSQLProvider richiede query SQL")
        pyodbc = _import_pyodbc()
        conn = pyodbc.connect(self._build_connstr())
        try:
            cur = conn.cursor()
            cur.execute(self._query)
            cols = [c[0] for c in cur.description]
            for row in cur:
                yield {col: val for col, val in zip(cols, row)}
        finally:
            conn.close()

    def test_connection(self) -> dict[str, Any]:
        try:
            pyodbc = _import_pyodbc()
            conn = pyodbc.connect(self._build_connstr())
            try:
                cur = conn.cursor()
                cur.execute("SELECT 1")
                cur.fetchone()
                if self._query:
                    cur.execute(f"SELECT COUNT(*) FROM ({self._query}) AS _q")
                    sample_count = cur.fetchone()[0]
                else:
                    sample_count = None
                return {"ok": True, "message": "Connessione MSSQL OK",
                        "sample_count": sample_count}
            finally:
                conn.close()
        except ProviderConnectionError as exc:
            return {"ok": False, "message": str(exc)}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": "Connessione MSSQL fallita",
                    "error": str(exc)[:500]}

    def describe_schema(self) -> list[str]:
        if not self._query:
            return []
        try:
            pyodbc = _import_pyodbc()
            conn = pyodbc.connect(self._build_connstr())
            try:
                cur = conn.cursor()
                # Tecnica analoga al postgres: avvolgo la query in una subquery
                # con TOP 0 per leggere solo i metadata.
                wrapped = f"SELECT TOP 0 * FROM ({self._query}) AS _q"
                cur.execute(wrapped)
                return [c[0] for c in cur.description]
            finally:
                conn.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning("describe_schema mssql fallito: %s", exc)
            return []
