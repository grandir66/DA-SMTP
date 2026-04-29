"""Implementazione PostgreSQL di `Storage`. Stub per v1.0 — implementazione completa
in fase successiva (target: MSP grandi che vogliono HA).

Per ora ritorna NotImplementedError sui metodi: chi vuole PG deve impegnarsi
a finanziare/contribuire l'implementazione completa (D6 del piano: SQLite default,
PG opt-in).
"""
from __future__ import annotations

from typing import Any

from .base import Storage


class PostgresStorage(Storage):
    def __init__(self, dsn: str):
        self._dsn = dsn
        # Lazy import psycopg2 (extras [postgres] del pyproject)
        try:
            import psycopg2  # noqa: F401
        except ImportError as exc:
            raise RuntimeError(
                "PostgresStorage richiede psycopg2-binary. "
                "Installa con: pip install 'domarc-smtp-relay-admin[postgres]'"
            ) from exc
        raise NotImplementedError(
            "PostgresStorage non ancora implementato in v1.0. "
            "Per ora usa SQLite (db_backend=sqlite). "
            "Vedi roadmap: services/smtp_relay/admin/docs/ROADMAP.md"
        )

    # Tutti i metodi astratti rilanciano NotImplementedError dal __init__
    # Quando implementeremo PG, sarà un mirror 1:1 di SqliteStorage con SQL Postgres.
    def list_tenants(self, **kw): raise NotImplementedError
    def get_tenant(self, tenant_id): raise NotImplementedError
    def upsert_tenant(self, data): raise NotImplementedError
    def delete_tenant(self, tenant_id): raise NotImplementedError
    def list_users(self): raise NotImplementedError
    def get_user_by_username(self, username): raise NotImplementedError
    def upsert_user(self, data): raise NotImplementedError
    def delete_user(self, user_id): raise NotImplementedError
    def log_login(self, **kw): raise NotImplementedError
    def list_rules(self, **kw): raise NotImplementedError
    def get_rule(self, rule_id): raise NotImplementedError
    def upsert_rule(self, data, **kw): raise NotImplementedError
    def delete_rule(self, rule_id): raise NotImplementedError
    def toggle_rule(self, rule_id): raise NotImplementedError
    def list_templates(self, **kw): raise NotImplementedError
    def get_template(self, template_id): raise NotImplementedError
    def upsert_template(self, data, **kw): raise NotImplementedError
    def delete_template(self, template_id): raise NotImplementedError
    def list_events(self, **kw): raise NotImplementedError
    def get_event(self, event_id): raise NotImplementedError
    def insert_event(self, data): raise NotImplementedError
    def purge_expired_bodies(self): raise NotImplementedError
    def health(self): return {"ok": False, "backend": "postgres", "error": "not implemented"}
    def schema_version(self): raise NotImplementedError
    def apply_migrations(self): raise NotImplementedError
    def list_service_hours(self, **kw): raise NotImplementedError
    def get_service_hours(self, codcli, tenant_id): raise NotImplementedError
    def upsert_service_hours(self, data, **kw): raise NotImplementedError
    def delete_service_hours(self, codcli, tenant_id): raise NotImplementedError
    def list_profiles(self, **kw): raise NotImplementedError
    def get_profile(self, profile_id): raise NotImplementedError
    def list_auth_codes(self, **kw): raise NotImplementedError
    def issue_auth_code(self, **kw): raise NotImplementedError
    def delete_auth_code(self, code_id): raise NotImplementedError
    def list_aggregations(self, **kw): raise NotImplementedError
    def get_aggregation(self, agg_id): raise NotImplementedError
    def upsert_aggregation(self, data, **kw): raise NotImplementedError
    def delete_aggregation(self, agg_id): raise NotImplementedError
    def list_occurrences(self, **kw): raise NotImplementedError
