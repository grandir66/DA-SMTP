"""Customer source via Stormshield Manager API.

Quando il relay-admin gira ACCANTO al manager Domarc esistente (uso interno),
legge l'anagrafica clienti via il preesistente endpoint:

    GET /api/v1/relay/customers/active
    Header: X-API-Key: <RELAY_API_KEY>

Riusa il payload già consumato dal listener relay (servizio omonimo).
Cache locale 5 min.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any

import httpx

from .base import Customer, CustomerSource

logger = logging.getLogger(__name__)


class StormshieldCustomerSource(CustomerSource):
    def __init__(self, cs_config):
        self._cfg = cs_config
        if not cs_config.stormshield_base_url:
            raise ValueError("StormshieldCustomerSource richiede stormshield_base_url")
        self._api_key = os.environ.get(cs_config.stormshield_api_key_env, "").strip()
        if not self._api_key:
            logger.warning("StormshieldCustomerSource: %s non valorizzata",
                          cs_config.stormshield_api_key_env)
        verify = bool(cs_config.stormshield_verify_tls)
        self._client = httpx.Client(
            base_url=cs_config.stormshield_base_url.rstrip("/"),
            headers={"X-API-Key": self._api_key, "Accept": "application/json"},
            timeout=10.0,
            verify=verify,
        )
        self._cache: list[Customer] = []
        self._cache_ts: float = 0.0
        self._cache_ttl: float = 300.0
        self._lock = threading.Lock()
        self._last_error: str | None = None

    def _refresh(self) -> None:
        try:
            r = self._client.get("/api/v1/relay/customers/active")
            r.raise_for_status()
            data = r.json()
            raw = data.get("customers", []) or []
            customers: list[Customer] = []
            for c in raw:
                cc = str(c.get("codcli") or c.get("codice_cliente") or "").strip().upper()
                if not cc:
                    continue
                sh = c.get("service_hours") or {}
                ct = c.get("contract_type") or {}
                customers.append(Customer(
                    codice_cliente=cc,
                    ragione_sociale=c.get("ragione_sociale"),
                    tipologia_servizio=(sh.get("profile") or "standard"),
                    profile_description=sh.get("profile_description"),
                    is_active=bool(c.get("is_active", c.get("contract_active", True))),
                    contract_type=(ct.get("description") if isinstance(ct, dict) else None),
                    contract_expiry_date=c.get("contract_expiry_date"),
                    domains=list(c.get("domains") or []),
                    aliases=list(c.get("aliases") or []),
                    holidays=sh.get("holidays"),
                    schedule=sh.get("schedule"),
                ))
            self._cache = customers
            self._cache_ts = time.monotonic()
            self._last_error = None
            logger.info("StormshieldCustomerSource: %d clienti abilitati caricati", len(self._cache))
        except Exception as exc:  # noqa: BLE001
            self._last_error = str(exc)
            logger.warning("StormshieldCustomerSource refresh fallito: %s", exc)

    def _maybe_refresh(self) -> None:
        with self._lock:
            if time.monotonic() - self._cache_ts > self._cache_ttl or not self._cache:
                self._refresh()

    def list_customers(self) -> list[Customer]:
        self._maybe_refresh()
        return list(self._cache)

    def get_by_codcli(self, codcli: str) -> Customer | None:
        codcli = (codcli or "").strip().upper()
        self._maybe_refresh()
        return next((c for c in self._cache if c.codice_cliente == codcli), None)

    def health(self) -> dict[str, Any]:
        return {
            "backend": "stormshield",
            "base_url": self._cfg.stormshield_base_url,
            "count": len(self._cache),
            "last_refresh": self._cache_ts,
            "last_error": self._last_error,
        }
