"""Adapter pluggabili per l'anagrafica clienti (D5 del piano standalone).

4 backend day-1:
- yaml         — file YAML statico, gestione manuale via SSH/git
- sqlite       — DB locale + UI CRUD (default install)
- rest         — REST API verso CRM proprietario
- stormshield  — Stormshield Manager (uso interno Domarc)

Tutti implementano `CustomerSource` astratto.
"""
from __future__ import annotations

from .base import Customer, CustomerSource

__all__ = ["Customer", "CustomerSource", "get_customer_source"]


def get_customer_source(config) -> CustomerSource:
    """Factory: ritorna l'adapter customer source in base a config.customer_source.backend."""
    backend = config.customer_source.backend
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
    raise ValueError(f"customer_source backend non supportato: {backend}")
