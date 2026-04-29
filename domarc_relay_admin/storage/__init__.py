"""Storage layer dell'admin web — DAO astratto + 2 implementazioni (SQLite/Postgres).

Strategia (D6 del piano standalone):
- SQLite default per setup PMI / single-tenant / piccolo MSP (zero infrastruttura)
- PostgreSQL opt-in per MSP grandi che vogliono HA/replication

Schema duplicato in 2 dialetti: vedi `migrations/00X_*.sqlite.sql` e `00X_*.pg.sql`.

Uso tipico:
    from domarc_relay_admin.storage import get_storage
    storage = get_storage(config)   # ritorna SqliteStorage o PostgresStorage
    rules = storage.list_rules(tenant_id=1)
"""
from __future__ import annotations

from .base import Storage

__all__ = ["Storage", "get_storage"]


def get_storage(config) -> Storage:
    """Factory: ritorna l'implementazione storage in base a config.db_backend."""
    if config.db_backend == "sqlite":
        from .sqlite_impl import SqliteStorage
        return SqliteStorage(config.db_path)
    if config.db_backend == "postgres":
        from .postgres_impl import PostgresStorage
        return PostgresStorage(config.db_dsn)
    raise ValueError(f"db_backend non supportato: {config.db_backend}")
