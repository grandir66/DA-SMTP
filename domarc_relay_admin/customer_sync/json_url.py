"""Provider JSON URL: HTTP GET/POST verso webservice REST + JSONPath.

config_json:
    {
      "url": "https://crm.example.com/api/customers",
      "method": "GET",                          # default GET
      "headers": {"Authorization": "Bearer ..."},
      "timeout": 30,                            # secondi
      "verify_ssl": true                        # default true
    }

query_or_path:
    JSONPath che estrae l'array di record dalla risposta.
    Esempi:
      "$.data[*]"     -> {"data": [...]}
      "$.customers"   -> {"customers": [...]}
      "$"             -> tutta la risposta e' gia' un array (default)
"""
from __future__ import annotations

import json
import logging
from typing import Any, Iterator

from .base import CustomerSyncProvider, FetchedRecord, ProviderConnectionError

logger = logging.getLogger(__name__)


def _import_jsonpath():
    try:
        from jsonpath_ng import parse as jp_parse  # type: ignore
        return jp_parse
    except ImportError as exc:
        raise ProviderConnectionError(
            "jsonpath_ng non installato (richiesto per provider json_url). "
            "Installa con: pip install 'jsonpath-ng'."
        ) from exc


def _import_requests():
    try:
        import requests  # type: ignore
        return requests
    except ImportError as exc:
        raise ProviderConnectionError("requests non installato (dovrebbe esserci)") from exc


class JsonUrlProvider(CustomerSyncProvider):

    def __init__(self, *, config: dict[str, Any], jsonpath: str | None) -> None:
        self._config = config or {}
        self._jsonpath = jsonpath or "$"

    def _do_request(self) -> Any:
        requests = _import_requests()
        cfg = self._config
        url = cfg.get("url")
        if not url:
            raise ProviderConnectionError("JSON URL: campo 'url' mancante")
        method = (cfg.get("method") or "GET").upper()
        headers = cfg.get("headers") or {}
        if isinstance(headers, str):
            try:
                headers = json.loads(headers)
            except (TypeError, ValueError):
                headers = {}
        timeout = float(cfg.get("timeout", 30))
        verify = cfg.get("verify_ssl")
        if verify is None:
            verify = True
        body = cfg.get("body")
        if body and isinstance(body, str):
            try:
                body = json.loads(body)
            except (TypeError, ValueError):
                pass
        resp = requests.request(method, url, headers=headers, timeout=timeout,
                                verify=bool(verify), json=body if body else None)
        resp.raise_for_status()
        return resp.json()

    def _extract_records(self, data: Any) -> list[dict]:
        jp_parse = _import_jsonpath()
        if not self._jsonpath or self._jsonpath.strip() in ("$", "$."):
            if isinstance(data, list):
                return data
            if isinstance(data, dict):
                return [data]
            return []
        expr = jp_parse(self._jsonpath)
        matches = [m.value for m in expr.find(data)]
        # Se il JSONPath punta a un array singolo, srotolalo.
        if len(matches) == 1 and isinstance(matches[0], list):
            return matches[0]
        return matches

    def fetch(self) -> Iterator[FetchedRecord]:
        data = self._do_request()
        for rec in self._extract_records(data):
            if isinstance(rec, dict):
                yield rec
            else:
                yield {"value": rec}

    def test_connection(self) -> dict[str, Any]:
        try:
            data = self._do_request()
            recs = self._extract_records(data)
            return {"ok": True, "message": f"OK: {len(recs)} record dalla risposta",
                    "sample_count": len(recs)}
        except ProviderConnectionError as exc:
            return {"ok": False, "message": str(exc)}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": "JSON URL fallito", "error": str(exc)[:500]}

    def describe_schema(self) -> list[str]:
        try:
            data = self._do_request()
            recs = self._extract_records(data)
            if not recs:
                return []
            first = recs[0]
            if isinstance(first, dict):
                return list(first.keys())
        except Exception as exc:  # noqa: BLE001
            logger.warning("describe_schema json_url fallito: %s", exc)
        return []
