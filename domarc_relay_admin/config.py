"""Configurazione Domarc SMTP Relay Admin.

Strategia: env vars per i valori sensibili (segreti, DSN), file YAML opzionale per
i campi strutturati (customer_source, log levels, ecc). Tutti i nomi env hanno
prefisso `DOMARC_RELAY_` (vincolo branding — vedi memory feedback_branding_smtp_relay).
"""
from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def _env_or(name: str, default: str | None) -> str | None:
    v = os.environ.get(name)
    return v if v is not None else default


def _env_bool(name: str, default: bool = False) -> bool:
    v = os.environ.get(name, "").strip().lower()
    if not v:
        return default
    return v in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    v = os.environ.get(name)
    if not v:
        return default
    try:
        return int(v)
    except ValueError:
        return default


@dataclass
class CustomerSourceConfig:
    """Config dell'adapter customer source. Quale backend e parametri specifici."""
    backend: str = "yaml"   # yaml | sqlite | rest | stormshield
    # Per ogni backend, parametri:
    yaml_path: str | None = None
    sqlite_path: str | None = None       # path al DB customer (separato da admin.db se vuoi)
    rest_base_url: str | None = None
    rest_api_key_env: str | None = None
    rest_list_endpoint: str = "/customers"
    rest_by_codcli_endpoint: str = "/customers/{codcli}"
    rest_by_email_endpoint: str = "/customers/by-email/{email}"
    stormshield_base_url: str | None = None
    stormshield_api_key_env: str = "STORMSHIELD_RELAY_API_KEY"
    stormshield_verify_tls: bool = True


@dataclass
class AppConfig:
    """Config principale dell'admin web."""
    # Server
    bind_host: str = "127.0.0.1"
    bind_port: int = 8443
    secret_key: str = field(default_factory=lambda: secrets.token_hex(32))
    debug: bool = False

    # Database admin (rules/templates/orari/codici/eventi/users)
    db_backend: str = "sqlite"   # sqlite | postgres
    db_path: str = "/var/lib/domarc-smtp-relay/admin.db"
    db_dsn: str | None = None     # solo se backend=postgres

    # Customer source pluggable
    customer_source: CustomerSourceConfig = field(default_factory=CustomerSourceConfig)

    # Telemetry/heartbeat (D3 — primitive nel codice, attivazione decisa commercialmente)
    telemetry_url: str | None = None
    telemetry_interval_min: int = 15

    # Update notifier (D3 — controlla un manifest pubblico per nuove versioni)
    update_manifest_url: str = "https://domarc.it/smtp-relay/latest.json"
    update_check_interval_h: int = 24

    # Body retention (default 6h — vedi feature body_retention_hours del manager)
    body_retention_hours: int = 6
    body_max_size_kb: int = 256


def load_config(yaml_path: str | None = None) -> AppConfig:
    """Carica config combinando env vars + YAML opzionale. Env ha priorità."""
    cfg = AppConfig()

    # YAML opzionale (path da DOMARC_RELAY_CONFIG o argomento esplicito)
    yaml_path = yaml_path or os.environ.get("DOMARC_RELAY_CONFIG")
    if yaml_path and Path(yaml_path).exists():
        try:
            import yaml
            with open(yaml_path, "r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh) or {}
            _apply_yaml(cfg, data)
        except Exception as exc:  # noqa: BLE001
            import logging
            logging.warning("Errore caricamento YAML config %s: %s", yaml_path, exc)

    # Env vars (sempre overridano YAML)
    cfg.bind_host = _env_or("DOMARC_RELAY_BIND_HOST", cfg.bind_host) or cfg.bind_host
    cfg.bind_port = _env_int("DOMARC_RELAY_BIND_PORT", cfg.bind_port)
    sk = _env_or("DOMARC_RELAY_SECRET_KEY", None)
    if sk:
        cfg.secret_key = sk
    cfg.debug = _env_bool("DOMARC_RELAY_DEBUG", cfg.debug)

    cfg.db_backend = (_env_or("DOMARC_RELAY_DB_BACKEND", cfg.db_backend) or cfg.db_backend).lower()
    cfg.db_path = _env_or("DOMARC_RELAY_DB_PATH", cfg.db_path) or cfg.db_path
    cfg.db_dsn = _env_or("DOMARC_RELAY_DB_DSN", cfg.db_dsn)

    csb = _env_or("DOMARC_RELAY_CUSTOMER_SOURCE", cfg.customer_source.backend)
    if csb:
        cfg.customer_source.backend = csb.lower()
    cfg.customer_source.yaml_path = _env_or("DOMARC_RELAY_CUSTOMERS_YAML", cfg.customer_source.yaml_path)
    cfg.customer_source.stormshield_base_url = _env_or(
        "DOMARC_RELAY_STORMSHIELD_URL", cfg.customer_source.stormshield_base_url,
    )
    # Cert self-signed in dev: env DOMARC_RELAY_STORMSHIELD_VERIFY_TLS=0 disabilita verify
    cfg.customer_source.stormshield_verify_tls = _env_bool(
        "DOMARC_RELAY_STORMSHIELD_VERIFY_TLS", cfg.customer_source.stormshield_verify_tls,
    )

    cfg.telemetry_url = _env_or("DOMARC_RELAY_TELEMETRY_URL", cfg.telemetry_url)
    cfg.body_retention_hours = _env_int("DOMARC_RELAY_BODY_RETENTION_HOURS", cfg.body_retention_hours)
    cfg.body_max_size_kb = _env_int("DOMARC_RELAY_BODY_MAX_SIZE_KB", cfg.body_max_size_kb)

    if cfg.db_backend not in ("sqlite", "postgres"):
        raise ValueError(f"db_backend deve essere 'sqlite' o 'postgres', non '{cfg.db_backend}'")
    if cfg.db_backend == "postgres" and not cfg.db_dsn:
        raise ValueError("db_backend=postgres richiede DOMARC_RELAY_DB_DSN")

    return cfg


def _apply_yaml(cfg: AppConfig, data: dict[str, Any]) -> None:
    """Applica i campi YAML al dataclass (best-effort, ignora chiavi sconosciute)."""
    server = data.get("server") or {}
    cfg.bind_host = server.get("bind_host", cfg.bind_host)
    cfg.bind_port = int(server.get("bind_port", cfg.bind_port))

    db = data.get("database") or {}
    cfg.db_backend = (db.get("backend") or cfg.db_backend).lower()
    cfg.db_path = db.get("path", cfg.db_path)
    cfg.db_dsn = db.get("dsn", cfg.db_dsn)

    cs = data.get("customer_source") or {}
    if cs.get("backend"):
        cfg.customer_source.backend = cs["backend"]
    yaml_section = cs.get("yaml") or {}
    if yaml_section.get("path"):
        cfg.customer_source.yaml_path = yaml_section["path"]
    rest = cs.get("rest") or {}
    if rest.get("base_url"):
        cfg.customer_source.rest_base_url = rest["base_url"]
        cfg.customer_source.rest_api_key_env = rest.get("api_key_env")
    storm = cs.get("stormshield") or {}
    if storm.get("base_url"):
        cfg.customer_source.stormshield_base_url = storm["base_url"]
        cfg.customer_source.stormshield_api_key_env = storm.get("api_key_env", cfg.customer_source.stormshield_api_key_env)
        cfg.customer_source.stormshield_verify_tls = bool(storm.get("verify_tls", True))

    tel = data.get("telemetry") or {}
    if tel.get("url"):
        cfg.telemetry_url = tel["url"]
    if tel.get("interval_min"):
        cfg.telemetry_interval_min = int(tel["interval_min"])
