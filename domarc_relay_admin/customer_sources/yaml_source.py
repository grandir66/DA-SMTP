"""Customer source da file YAML.

Format atteso:

    customers:
      - codice_cliente: "ACME001"
        ragione_sociale: "ACME S.p.A."
        tipologia_servizio: "standard"
        contract_active: true
        domains: ["acme.it", "acme.com"]
        aliases: ["info@acme.it"]
        notes: "Cliente premium"

Ricarica automatica al cambio del file (mtime check con cache 5s).
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from .base import Customer, CustomerSource

logger = logging.getLogger(__name__)


class YamlCustomerSource(CustomerSource):
    def __init__(self, yaml_path: str):
        self._path = Path(yaml_path)
        self._cache: list[Customer] = []
        self._mtime: float = 0.0
        self._last_check: float = 0.0
        self._reload()

    def _reload(self) -> None:
        if not self._path.exists():
            self._cache = []
            return
        try:
            import yaml
            with self._path.open("r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh) or {}
            customers_raw = data.get("customers", []) or []
            self._cache = [
                Customer(
                    codice_cliente=str(c.get("codice_cliente") or "").strip().upper(),
                    ragione_sociale=c.get("ragione_sociale"),
                    tipologia_servizio=c.get("tipologia_servizio", "standard"),
                    contract_active=bool(c.get("contract_active", True)),
                    domains=list(c.get("domains") or []),
                    aliases=list(c.get("aliases") or []),
                    notes=c.get("notes"),
                    holidays=c.get("holidays"),
                    schedule_overrides=c.get("schedule_overrides"),
                )
                for c in customers_raw if c.get("codice_cliente")
            ]
            self._mtime = self._path.stat().st_mtime
            logger.info("YamlCustomerSource: caricati %d clienti da %s", len(self._cache), self._path)
        except Exception as exc:  # noqa: BLE001
            logger.error("YamlCustomerSource: errore parse %s: %s", self._path, exc)
            # Mantiene cache precedente in caso di errore parser

    def _maybe_reload(self) -> None:
        """Reload se file cambiato (controllo mtime ogni 5s max)."""
        now = time.monotonic()
        if now - self._last_check < 5.0:
            return
        self._last_check = now
        if not self._path.exists():
            return
        try:
            mtime = self._path.stat().st_mtime
            if mtime > self._mtime:
                self._reload()
        except OSError:
            pass

    def list_customers(self) -> list[Customer]:
        self._maybe_reload()
        return list(self._cache)

    def get_by_codcli(self, codcli: str) -> Customer | None:
        self._maybe_reload()
        codcli = (codcli or "").strip().upper()
        return next((c for c in self._cache if c.codice_cliente == codcli), None)

    def health(self) -> dict[str, Any]:
        return {
            "backend": "yaml",
            "path": str(self._path),
            "exists": self._path.exists(),
            "count": len(self._cache),
            "mtime": self._mtime,
        }
