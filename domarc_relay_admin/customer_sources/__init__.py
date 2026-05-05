"""Adapter pluggabili per l'anagrafica clienti.

5 backend disponibili:
- local        — tabella `customers` locale, alimentata da customer_sync (M028).
                 DEFAULT post 2026-05.
- yaml         — file YAML statico, gestione manuale via SSH/git.
- sqlite       — DB locale + UI CRUD (legacy install).
- rest         — REST API verso CRM proprietario.
- stormshield  — Stormshield Manager (uso interno Domarc legacy).
- postgres     — legacy: thread di sync hardcoded verso PG solution+stormshield.
                 Sostituito da `local` + sorgente sync `Postgres solution Domarc`.
                 Mantenuto come compat: se l'admin viene avviato con
                 backend=postgres, NON parte piu' il thread di sync (la sorgente
                 e' alimentata da SyncEngine via customer_sync_sources).

Tutti implementano `CustomerSource` astratto.
"""
from __future__ import annotations

import logging

from .base import Customer, CustomerSource

logger = logging.getLogger(__name__)

__all__ = ["Customer", "CustomerSource", "get_customer_source"]


def get_customer_source(config, storage=None) -> CustomerSource:
    """Factory: ritorna l'adapter customer source in base a config.customer_source.backend.

    `storage` opzionale: se passato, il backend `postgres` legge la config dai
    settings dell'admin (UI Integrations) invece che dalle env vars.
    """
    backend = config.customer_source.backend
    if backend == "local":
        from .local_source import LocalCustomerSource
        return LocalCustomerSource(storage if storage is not None else config)
    if backend == "yaml":
        from .yaml_source import YamlCustomerSource
        return YamlCustomerSource(config.customer_source.yaml_path or "/etc/domarc-smtp-relay/customers.yaml")
    if backend == "sqlite":
        from .sqlite_source import SqliteCustomerSource
        return SqliteCustomerSource(config.customer_source.sqlite_path or config.db_path)
    if backend == "rest":
        from .rest_source import RestCustomerSource
        return RestCustomerSource(config.customer_source)
    if backend == "stormshield":
        from .stormshield_source import StormshieldCustomerSource
        return StormshieldCustomerSource(config.customer_source)
    if backend == "postgres":
        # Legacy: post-M028 il thread di sync e' deprecato. Il dato runtime
        # viene letto dalla tabella `customers` (riempita da SyncEngine via
        # la sorgente seed "Postgres solution Domarc"). Mantiene compat per
        # chi ha config.backend=postgres senza dover toccare i settings.
        logger.info("customer_source backend=postgres -> delego a LocalCustomerSource "
                    "(la sorgente legacy alimenta `customers` via customer_sync_sources)")
        from .local_source import LocalCustomerSource
        return LocalCustomerSource(storage if storage is not None else config)
    raise ValueError(f"customer_source backend non supportato: {backend}")
