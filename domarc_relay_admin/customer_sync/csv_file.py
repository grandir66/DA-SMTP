"""Provider CSV file: legge clienti da file CSV su filesystem.

config_json:
    {
      "path": "/var/lib/domarc/customers.csv",   # required, abs path
      "delimiter": ",",                           # default ','
      "encoding": "utf-8",                        # default 'utf-8'
      "has_header": true                          # default true
    }

Se has_header=true: i nomi delle colonne sono presi dalla prima riga,
altrimenti vengono nominate col0, col1, ... e il mapping_json deve
referenziarli con quei nomi.
"""
from __future__ import annotations

import csv
import logging
from pathlib import Path
from typing import Any, Iterator

from .base import CustomerSyncProvider, FetchedRecord, ProviderConnectionError

logger = logging.getLogger(__name__)


class CsvFileProvider(CustomerSyncProvider):

    def __init__(self, *, config: dict[str, Any]) -> None:
        self._config = config or {}

    def _path(self) -> Path:
        p = self._config.get("path")
        if not p:
            raise ProviderConnectionError("CSV: campo 'path' mancante in config")
        path = Path(p)
        if not path.is_absolute():
            raise ProviderConnectionError(f"CSV: path deve essere assoluto: {p}")
        return path

    def _delimiter(self) -> str:
        return (self._config.get("delimiter") or ",")[:1] or ","

    def _encoding(self) -> str:
        return self._config.get("encoding") or "utf-8"

    def _has_header(self) -> bool:
        v = self._config.get("has_header")
        return True if v is None else bool(v)

    def fetch(self) -> Iterator[FetchedRecord]:
        path = self._path()
        if not path.exists():
            raise ProviderConnectionError(f"CSV non trovato: {path}")
        with open(path, "r", encoding=self._encoding(), newline="") as fh:
            if self._has_header():
                reader = csv.DictReader(fh, delimiter=self._delimiter())
                for row in reader:
                    yield {(k or "").strip(): (v or "").strip() if isinstance(v, str) else v
                           for k, v in row.items() if k}
            else:
                reader = csv.reader(fh, delimiter=self._delimiter())
                for row in reader:
                    yield {f"col{i}": (v or "").strip() for i, v in enumerate(row)}

    def test_connection(self) -> dict[str, Any]:
        try:
            path = self._path()
            if not path.exists():
                return {"ok": False, "message": f"File non trovato: {path}"}
            if not path.is_file():
                return {"ok": False, "message": f"Path non e' un file: {path}"}
            count = 0
            with open(path, "r", encoding=self._encoding(), newline="") as fh:
                reader = csv.reader(fh, delimiter=self._delimiter())
                for _ in reader:
                    count += 1
            sample_count = count - (1 if self._has_header() else 0)
            return {"ok": True, "message": f"OK: {sample_count} righe lette",
                    "sample_count": sample_count}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": "Errore lettura CSV",
                    "error": str(exc)[:500]}

    def describe_schema(self) -> list[str]:
        try:
            path = self._path()
            with open(path, "r", encoding=self._encoding(), newline="") as fh:
                reader = csv.reader(fh, delimiter=self._delimiter())
                first = next(reader, [])
                if self._has_header():
                    return [(c or "").strip() for c in first]
                return [f"col{i}" for i in range(len(first))]
        except Exception as exc:  # noqa: BLE001
            logger.warning("describe_schema CSV fallito: %s", exc)
            return []
