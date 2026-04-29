"""Customer source via REST API (CRM proprietario).

Si aspetta un'API che fornisca:
- GET <list_endpoint> → {"customers": [...]} o lista flat di customer dict
- GET <by_codcli_endpoint formattato con {codcli}> → singolo customer
- (opz.) GET <by_email_endpoint formattato con {email}> → match diretto

API key passata come `X-API-Key` header (env var configurabile).
Cache locale 5 min per ridurre carico sul CRM.
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


class RestCustomerSource(CustomerSource):
    def __init__(self, cs_config):
        self._cfg = cs_config
        if not cs_config.rest_base_url:
            raise ValueError("RestCustomerSource richiede rest_base_url")
        api_key_env = cs_config.rest_api_key_env or "DOMARC_RELAY_REST_API_KEY"
        self._api_key = os.environ.get(api_key_env, "").strip()
        if not self._api_key:
            logger.warning("RestCustomerSource: env var %s non valorizzata", api_key_env)
        self._client = httpx.Client(
            base_url=cs_config.rest_base_url.rstrip("/"),
            headers={"X-API-Key": self._api_key, "Accept": "application/json"},
            timeout=10.0,
        )
        self._cache: list[Customer] = []
        self._cache_ts: float = 0.0
        self._cache_ttl: float = 300.0
        self._lock = threading.Lock()
        self._last_error: str | None = None

    def _refresh(self) -> None:
        try:
            r = self._client.get(self._cfg.rest_list_endpoint)
            r.raise_for_status()
            data = r.json()
            raw = data.get("customers", data) if isinstance(data, dict) else data
            self._cache = [
                Customer(
                    codice_cliente=str(c.get("codice_cliente") or c.get("codcli") or "").strip().upper(),
                    ragione_sociale=c.get("ragione_sociale"),
                    tipologia_servizio=c.get("tipologia_servizio", "standard"),
                    contract_active=bool(c.get("contract_active", True)),
                    domains=list(c.get("domains") or []),
                    aliases=list(c.get("aliases") or []),
                    notes=c.get("notes"),
                    holidays=c.get("holidays"),
                    schedule_overrides=c.get("schedule_overrides"),
                )
                for c in raw if c.get("codice_cliente") or c.get("codcli")
            ]
            self._cache_ts = time.monotonic()
            self._last_error = None
            logger.info("RestCustomerSource: %d clienti caricati", len(self._cache))
        except Exception as exc:  # noqa: BLE001
            self._last_error = str(exc)
            logger.warning("RestCustomerSource refresh fallito: %s", exc)

    def _maybe_refresh(self) -> None:
        with self._lock:
            if time.monotonic() - self._cache_ts > self._cache_ttl or not self._cache:
                self._refresh()

    def list_customers(self) -> list[Customer]:
        self._maybe_refresh()
        return list(self._cache)

    def get_by_codcli(self, codcli: str) -> Customer | None:
        codcli = (codcli or "").strip().upper()
        # Prova endpoint singolo (più efficiente)
        try:
            r = self._client.get(self._cfg.rest_by_codcli_endpoint.format(codcli=codcli))
            if r.status_code == 200:
                c = r.json()
                return Customer(
                    codice_cliente=codcli,
                    ragione_sociale=c.get("ragione_sociale"),
                    tipologia_servizio=c.get("tipologia_servizio", "standard"),
                    contract_active=bool(c.get("contract_active", True)),
                    domains=list(c.get("domains") or []),
                    aliases=list(c.get("aliases") or []),
                    notes=c.get("notes"),
                    holidays=c.get("holidays"),
                    schedule_overrides=c.get("schedule_overrides"),
                )
        except Exception as exc:  # noqa: BLE001
            logger.debug("REST get_by_codcli %s fallito: %s", codcli, exc)
        # Fallback: cerca nella cache
        self._maybe_refresh()
        return next((c for c in self._cache if c.codice_cliente == codcli), None)

    def health(self) -> dict[str, Any]:
        return {
            "backend": "rest",
            "base_url": self._cfg.rest_base_url,
            "count": len(self._cache),
            "last_refresh": self._cache_ts,
            "last_error": self._last_error,
        }
