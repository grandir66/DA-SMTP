"""Caricamento configurazione del relay da YAML + env vars.

Risolve segreti tramite riferimenti `<env:VAR_NAME>` nei valori YAML, in modo che il file
di config possa essere committato senza esporre password/chiavi.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

_ENV_REF = re.compile(r"^<env:([A-Z_][A-Z0-9_]*)>$")


def _resolve_env(value: Any) -> Any:
    if isinstance(value, str):
        match = _ENV_REF.match(value)
        if match:
            var_name = match.group(1)
            resolved = os.environ.get(var_name)
            if resolved is None:
                raise ValueError(f"Variabile d'ambiente '{var_name}' richiesta ma non impostata")
            return resolved
        return value
    if isinstance(value, dict):
        return {k: _resolve_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_env(v) for v in value]
    return value


@dataclass
class ManagerConfig:
    base_url: str
    api_key: str
    timeout_sec: int = 10
    sync_interval_sec: int = 300
    cache_grace_ttl_sec: int = 1800
    ca_bundle: str | None = None
    backend: str = "stormshield"
    verify_tls: bool = True


@dataclass
class StarttlsConfig:
    enabled: bool = False
    cert_path: str | None = None
    key_path: str | None = None


@dataclass
class ListenerConfig:
    bind_host: str = "127.0.0.1"
    bind_port: int = 2525
    hostname: str = "localhost"
    data_size_limit_mb: int = 20
    max_recipients: int = 50
    session_timeout_sec: int = 60
    starttls: StarttlsConfig = field(default_factory=StarttlsConfig)
    accepted_domains: list[str] = field(default_factory=list)


@dataclass
class StorageConfig:
    sqlite_path: str = "/var/lib/stormshield-smtp-relay/relay.db"


@dataclass
class OutboundConfig:
    default_smarthost: str = "localhost"
    default_smarthost_port: int = 25
    default_tls: str = "opportunistic"
    helo_hostname: str = "localhost"
    timeout_sec: int = 20
    max_attempts: int = 6
    backoff_seconds: list[int] = field(default_factory=lambda: [60, 300, 1800, 3600, 14400, 86400])


@dataclass
class SchedulerConfig:
    outbound_drain_interval_sec: int = 5
    dispatch_drain_interval_sec: int = 10
    events_flush_interval_sec: int = 30


@dataclass
class RateLimitConfig:
    per_from_domain_per_hour: int = 100


@dataclass
class RelayConfig:
    manager: ManagerConfig
    listener: ListenerConfig
    storage: StorageConfig
    outbound: OutboundConfig
    scheduler: SchedulerConfig
    rate_limit: RateLimitConfig
    routes_files: list[str] = field(default_factory=list)


def load_config(path: str | Path) -> RelayConfig:
    cfg_path = Path(path)
    if not cfg_path.exists():
        raise FileNotFoundError(f"File di configurazione non trovato: {cfg_path}")

    with cfg_path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)

    if not isinstance(raw, dict) or "relay" not in raw:
        raise ValueError("Struttura YAML invalida: root deve contenere chiave 'relay'")

    data = _resolve_env(raw["relay"])

    manager_raw = data.get("manager", {})
    listener_raw = data.get("listener", {})
    storage_raw = data.get("storage", {})
    outbound_raw = data.get("outbound", {})
    scheduler_raw = data.get("scheduler", {})
    rate_raw = data.get("rate_limit", {})
    starttls_raw = listener_raw.get("starttls", {})

    manager = ManagerConfig(
        base_url=manager_raw["base_url"],
        api_key=manager_raw["api_key"],
        timeout_sec=manager_raw.get("timeout_sec", 10),
        sync_interval_sec=manager_raw.get("sync_interval_sec", 300),
        cache_grace_ttl_sec=manager_raw.get("cache_grace_ttl_sec", 1800),
        ca_bundle=manager_raw.get("ca_bundle"),
        backend=manager_raw.get("backend", "stormshield"),
        verify_tls=bool(manager_raw.get("verify_tls", True)),
    )

    listener = ListenerConfig(
        bind_host=listener_raw.get("bind_host", "127.0.0.1"),
        bind_port=int(listener_raw.get("bind_port", 2525)),
        hostname=listener_raw.get("hostname", "localhost"),
        data_size_limit_mb=int(listener_raw.get("data_size_limit_mb", 20)),
        max_recipients=int(listener_raw.get("max_recipients", 50)),
        session_timeout_sec=int(listener_raw.get("session_timeout_sec", 60)),
        starttls=StarttlsConfig(
            enabled=bool(starttls_raw.get("enabled", False)),
            cert_path=starttls_raw.get("cert_path"),
            key_path=starttls_raw.get("key_path"),
        ),
        accepted_domains=[d.lower() for d in listener_raw.get("accepted_domains", [])],
    )

    storage = StorageConfig(sqlite_path=storage_raw.get("sqlite_path", StorageConfig.sqlite_path))

    outbound = OutboundConfig(
        default_smarthost=outbound_raw.get("default_smarthost", "localhost"),
        default_smarthost_port=int(outbound_raw.get("default_smarthost_port", 25)),
        default_tls=outbound_raw.get("default_tls", "opportunistic"),
        helo_hostname=outbound_raw.get("helo_hostname", listener.hostname),
        timeout_sec=int(outbound_raw.get("timeout_sec", 20)),
        max_attempts=int(outbound_raw.get("max_attempts", 6)),
        backoff_seconds=list(outbound_raw.get("backoff_seconds", [60, 300, 1800, 3600, 14400, 86400])),
    )

    scheduler = SchedulerConfig(
        outbound_drain_interval_sec=int(scheduler_raw.get("outbound_drain_interval_sec", 5)),
        dispatch_drain_interval_sec=int(scheduler_raw.get("dispatch_drain_interval_sec", 10)),
        events_flush_interval_sec=int(scheduler_raw.get("events_flush_interval_sec", 30)),
    )

    rate_limit = RateLimitConfig(
        per_from_domain_per_hour=int(rate_raw.get("per_from_domain_per_hour", 100)),
    )

    return RelayConfig(
        manager=manager,
        listener=listener,
        storage=storage,
        outbound=outbound,
        scheduler=scheduler,
        rate_limit=rate_limit,
        routes_files=list(data.get("routes_files", [])),
    )
