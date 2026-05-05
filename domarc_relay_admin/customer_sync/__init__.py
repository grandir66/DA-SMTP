"""Customer sync agnostico (Migration 028).

Package separato da `customer_sources/` (che resta runtime read-locale).
Qui vivono i provider che alimentano in BATCH la tabella `customers` da
fonti esterne eterogenee con field-mapping configurabile.

Architettura:
  source row in DB (customer_sync_sources)
        │
        ▼
  factory get_provider(kind, config_json, query_or_path)
        │
        ▼
  CustomerSyncProvider.fetch() -> Iterator[dict raw]
        │
        ▼
  mapper.apply(raw, mapping_json) -> dict canonico
        │
        ▼
  storage.upsert_customer_record(...)
        │
        ▼
  on_missing: flag/delete/keep su codcli scomparsi

Provider day-1: postgres, mssql, csv_file, json_url.
"""
from __future__ import annotations

from .base import CustomerSyncProvider, ProviderConnectionError, FetchedRecord

__all__ = [
    "CustomerSyncProvider",
    "ProviderConnectionError",
    "FetchedRecord",
    "get_provider",
    "PROVIDER_KINDS",
]


PROVIDER_KINDS = ("postgres", "mssql", "csv_file", "json_url")


def get_provider(kind: str, *, config: dict, query_or_path: str | None = None,
                 storage=None) -> CustomerSyncProvider:
    """Factory: ritorna provider concreto in base al kind.

    `storage` opzionale: serve al PostgresProvider in modalita' legacy
    (sentinel `_use_legacy_pgconfig=true`) per riusare PgConfig.from_settings.
    """
    if kind == "postgres":
        from .postgres import PostgresProvider
        return PostgresProvider(config=config, query=query_or_path, storage=storage)
    if kind == "mssql":
        from .mssql import MSSQLProvider
        return MSSQLProvider(config=config, query=query_or_path)
    if kind == "csv_file":
        from .csv_file import CsvFileProvider
        return CsvFileProvider(config=config)
    if kind == "json_url":
        from .json_url import JsonUrlProvider
        return JsonUrlProvider(config=config, jsonpath=query_or_path)
    raise ValueError(f"Customer sync provider non supportato: {kind!r}")
