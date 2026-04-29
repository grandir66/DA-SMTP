"""Storage astratto per l'admin web Domarc SMTP Relay.

Definisce l'interfaccia DAO che ogni backend deve implementare. Le 2 implementazioni
concrete (`SqliteStorage`, `PostgresStorage`) sono intercambiabili dal punto di
vista del codice business: l'app usa solo questa interfaccia.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class Storage(ABC):
    """Interfaccia astratta DAO admin web."""

    # =========================================================== TENANTS ===

    @abstractmethod
    def list_tenants(self, *, only_enabled: bool | None = None,
                     search: str | None = None) -> list[dict[str, Any]]: ...

    @abstractmethod
    def get_tenant(self, tenant_id: int) -> dict[str, Any] | None: ...

    @abstractmethod
    def upsert_tenant(self, data: dict[str, Any]) -> int: ...

    @abstractmethod
    def delete_tenant(self, tenant_id: int) -> None: ...

    # =========================================================== USERS =====

    @abstractmethod
    def list_users(self) -> list[dict[str, Any]]: ...

    @abstractmethod
    def get_user_by_username(self, username: str) -> dict[str, Any] | None: ...

    @abstractmethod
    def upsert_user(self, data: dict[str, Any]) -> int: ...

    @abstractmethod
    def delete_user(self, user_id: int) -> None: ...

    @abstractmethod
    def log_login(self, *, username: str, ip: str | None, ua: str | None,
                  outcome: str) -> None: ...

    # =========================================================== RULES =====

    @abstractmethod
    def list_rules(self, *, tenant_id: int | None = None,
                   only_enabled: bool | None = None,
                   action: str | None = None,
                   search: str | None = None) -> list[dict[str, Any]]: ...

    @abstractmethod
    def get_rule(self, rule_id: int) -> dict[str, Any] | None: ...

    @abstractmethod
    def upsert_rule(self, data: dict[str, Any], *, tenant_id: int,
                    created_by: str | None = None) -> int: ...

    @abstractmethod
    def delete_rule(self, rule_id: int) -> None: ...

    @abstractmethod
    def toggle_rule(self, rule_id: int) -> bool: ...

    # ----- Rule Engine v2 — gerarchia padre/figlio (migration 010+) -----
    # Implementazioni concrete in sqlite_impl. Postgres backend porta i metodi
    # in Fase 1 della roadmap rule engine v2.

    def list_top_level_items(self, *, tenant_id: int | None = None,
                             only_enabled: bool | None = None) -> list[dict[str, Any]]:
        raise NotImplementedError

    def list_group_children(self, group_id: int, *,
                            only_enabled: bool | None = None) -> list[dict[str, Any]]:
        raise NotImplementedError

    def list_rules_grouped(self, *, tenant_id: int | None = None,
                           only_enabled: bool | None = None) -> list[dict[str, Any]]:
        raise NotImplementedError

    def flatten_rules_for_listener(self, *, tenant_id: int | None = None) -> list[dict[str, Any]]:
        raise NotImplementedError

    def get_rule_with_inheritance(self, rule_id: int) -> dict[str, Any] | None:
        raise NotImplementedError

    def promote_rule_to_group(self, rule_id: int, group_label: str,
                              *, group_priority: int | None = None,
                              created_by: str | None = None) -> int:
        raise NotImplementedError

    def detect_groupable_rules(self, *, tenant_id: int | None = None,
                               min_cluster_size: int = 2) -> list[dict[str, Any]]:
        raise NotImplementedError

    # ----- Privacy bypass list (migration 011) -----

    def set_address_privacy_bypass(self, kind: str, addr_id: int, *,
                                    on: bool, reason: str | None = None,
                                    actor: str | None = None) -> None:
        raise NotImplementedError

    def list_addresses_privacy_bypass(self, kind: str, *,
                                       tenant_id: int | None = None) -> list[dict[str, Any]]:
        raise NotImplementedError

    def list_privacy_bypass_domains(self, *, tenant_id: int | None = None,
                                     only_enabled: bool | None = None) -> list[dict[str, Any]]:
        raise NotImplementedError

    def upsert_privacy_bypass_domain(self, *, tenant_id: int, domain: str,
                                      scope: str = "both", reason: str | None = None,
                                      enabled: bool = True,
                                      actor: str | None = None,
                                      domain_id: int | None = None) -> int:
        raise NotImplementedError

    def delete_privacy_bypass_domain(self, domain_id: int, *,
                                      actor: str | None = None) -> None:
        raise NotImplementedError

    def list_privacy_bypass_active(self, *, tenant_id: int | None = None) -> dict[str, Any]:
        raise NotImplementedError

    def list_privacy_bypass_audit(self, *, tenant_id: int | None = None,
                                   limit: int = 200) -> list[dict[str, Any]]:
        raise NotImplementedError

    # ----- AI Assistant (migration 012) -----

    def list_ai_providers(self, *, tenant_id: int | None = None) -> list[dict[str, Any]]:
        raise NotImplementedError

    def upsert_ai_provider(self, data: dict[str, Any], *, tenant_id: int = 1,
                            actor: str | None = None) -> int:
        raise NotImplementedError

    def delete_ai_provider(self, provider_id: int) -> None:
        raise NotImplementedError

    def list_ai_jobs(self) -> list[dict[str, Any]]:
        raise NotImplementedError

    def list_ai_job_bindings(self, *, tenant_id: int | None = None,
                              job_code: str | None = None,
                              only_enabled: bool | None = None) -> list[dict[str, Any]]:
        raise NotImplementedError

    def upsert_ai_job_binding(self, data: dict[str, Any], *, tenant_id: int = 1,
                               actor: str | None = None,
                               new_version: bool = False) -> int:
        raise NotImplementedError

    def insert_ai_decision(self, data: dict[str, Any]) -> int:
        raise NotImplementedError

    def list_ai_decisions(self, *, tenant_id: int | None = None,
                           job_code: str | None = None,
                           hours: int | None = None,
                           limit: int = 100) -> list[dict[str, Any]]:
        raise NotImplementedError

    def get_ai_decision(self, decision_id: int) -> dict[str, Any] | None:
        raise NotImplementedError

    def sum_ai_decisions_cost_today(self, *, tenant_id: int = 1,
                                     day: str | None = None) -> float:
        raise NotImplementedError

    def list_ai_pii_dictionary(self, *, tenant_id: int | None = None) -> list[dict[str, Any]]:
        raise NotImplementedError

    def upsert_ai_pii_dictionary_entry(self, *, tenant_id: int, kind: str,
                                         value: str, replacement: str,
                                         source: str = "manual") -> int:
        raise NotImplementedError

    def list_ai_error_clusters(self, *, tenant_id: int | None = None,
                                states: tuple[str, ...] | None = None,
                                limit: int = 200) -> list[dict[str, Any]]:
        raise NotImplementedError

    def get_ai_error_cluster(self, cluster_id: int) -> dict[str, Any] | None:
        raise NotImplementedError

    def upsert_ai_error_cluster(self, data: dict[str, Any]) -> int:
        raise NotImplementedError

    def insert_ai_shadow_audit(self, *, tenant_id: int, transition: str,
                                actor: str | None = None,
                                decisions_seen: int = 0,
                                avg_confidence: float | None = None,
                                notes: str | None = None) -> int:
        raise NotImplementedError

    def list_ai_shadow_audit(self, *, tenant_id: int | None = None,
                              limit: int = 50) -> list[dict[str, Any]]:
        raise NotImplementedError

    # ----- Migration 013: API keys (cifrate) + module install log -----

    def list_api_keys(self, *, tenant_id: int | None = None,
                       only_enabled: bool | None = None) -> list[dict[str, Any]]:
        raise NotImplementedError

    def get_api_key(self, key_id: int) -> dict[str, Any] | None:
        raise NotImplementedError

    def upsert_api_key(self, *, tenant_id: int, name: str, env_var_name: str,
                        value_encrypted: bytes, masked_preview: str,
                        description: str | None = None, enabled: bool = True,
                        actor: str | None = None,
                        key_id: int | None = None) -> int:
        raise NotImplementedError

    def delete_api_key(self, key_id: int) -> None:
        raise NotImplementedError

    def toggle_api_key(self, key_id: int) -> bool:
        raise NotImplementedError

    def list_module_install_log(self, *, module_code: str | None = None,
                                  limit: int = 50) -> list[dict[str, Any]]:
        raise NotImplementedError

    def insert_module_install_log(self, *, module_code: str, operation: str,
                                    status: str = "running",
                                    output: str | None = None,
                                    return_code: int | None = None,
                                    duration_ms: int | None = None,
                                    actor: str | None = None) -> int:
        raise NotImplementedError

    def update_module_install_log(self, log_id: int, *, status: str,
                                    output: str | None = None,
                                    return_code: int | None = None,
                                    duration_ms: int | None = None) -> None:
        raise NotImplementedError

    # =========================================================== TEMPLATES =

    @abstractmethod
    def list_templates(self, *, tenant_id: int | None = None,
                       only_enabled: bool | None = None) -> list[dict[str, Any]]: ...

    @abstractmethod
    def get_template(self, template_id: int) -> dict[str, Any] | None: ...

    @abstractmethod
    def upsert_template(self, data: dict[str, Any], *, tenant_id: int) -> int: ...

    @abstractmethod
    def delete_template(self, template_id: int) -> None: ...

    # =========================================================== EVENTS ====

    @abstractmethod
    def list_events(self, *, tenant_id: int | None = None,
                    hours: int = 24,
                    page: int = 1, page_size: int = 50,
                    filters: dict[str, Any] | None = None) -> tuple[list[dict[str, Any]], int]:
        """Ritorna (events, total_count). Tuple per supportare paginazione."""

    @abstractmethod
    def get_event(self, event_id: int) -> dict[str, Any] | None: ...

    @abstractmethod
    def insert_event(self, data: dict[str, Any]) -> int: ...

    @abstractmethod
    def purge_expired_bodies(self) -> int: ...

    # =========================================================== HEALTH ====

    @abstractmethod
    def health(self) -> dict[str, Any]:
        """Ritorna un dict con stato DB, schema_version, last_migration_at, ecc.
        Usato dall'endpoint /health e /diagnostic.
        """

    @abstractmethod
    def schema_version(self) -> int:
        """Ritorna la versione corrente dello schema (numero della migration più alta applicata)."""

    @abstractmethod
    def apply_migrations(self) -> int:
        """Applica tutte le migration pendenti. Ritorna il numero applicate."""

    # ====================================================== SERVICE HOURS ===

    @abstractmethod
    def list_service_hours(self, *, tenant_id: int | None = None,
                           search: str | None = None) -> list[dict[str, Any]]: ...

    @abstractmethod
    def get_service_hours(self, codice_cliente: str, tenant_id: int) -> dict[str, Any] | None: ...

    @abstractmethod
    def upsert_service_hours(self, data: dict[str, Any], *, tenant_id: int) -> str: ...

    @abstractmethod
    def delete_service_hours(self, codice_cliente: str, tenant_id: int) -> None: ...

    @abstractmethod
    def list_profiles(self, *, tenant_id: int | None = None) -> list[dict[str, Any]]: ...

    @abstractmethod
    def get_profile(self, profile_id: int) -> dict[str, Any] | None: ...

    # ======================================================= AUTH CODES =====

    @abstractmethod
    def list_auth_codes(self, *, tenant_id: int | None = None,
                        only_active: bool = False,
                        codice_cliente: str | None = None,
                        limit: int = 200) -> list[dict[str, Any]]: ...

    @abstractmethod
    def issue_auth_code(self, *, tenant_id: int, codice_cliente: str | None,
                        rule_id: int | None, ttl_hours: int,
                        note: str | None = None) -> dict[str, Any]: ...

    @abstractmethod
    def delete_auth_code(self, code_id: int) -> None: ...

    # ====================================================== AGGREGATIONS ====

    @abstractmethod
    def list_aggregations(self, *, tenant_id: int | None = None,
                          only_enabled: bool | None = None) -> list[dict[str, Any]]: ...

    @abstractmethod
    def get_aggregation(self, agg_id: int) -> dict[str, Any] | None: ...

    @abstractmethod
    def upsert_aggregation(self, data: dict[str, Any], *, tenant_id: int) -> int: ...

    @abstractmethod
    def delete_aggregation(self, agg_id: int) -> None: ...

    @abstractmethod
    def list_occurrences(self, *, tenant_id: int | None = None,
                         aggregation_id: int | None = None,
                         filter_state: str = "all",
                         limit: int = 200) -> list[dict[str, Any]]: ...
