"""Provider locale per endpoint OpenAI-compatible (DGX Spark, Ollama, vLLM, NIM).

Implementazione attivata in F4 della roadmap. Stub funzionante per F1: chiama
il `/v1/chat/completions` standard senza streaming, structured output via
``response_format={"type":"json_object"}``.

Costi: 0 (self-hosted), latenza tipica < 200ms su DGX Spark con modelli 8B.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

from .base import AiProvider, AiProviderError, AiResponse

logger = logging.getLogger(__name__)


class LocalHttpProvider(AiProvider):
    """Endpoint OpenAI-compatible (es. http://dgx.local:8000/v1)."""

    kind = "local_http"

    def __init__(self, *, name: str, endpoint: str,
                 api_key_env: str | None = None,
                 default_model: str = "llama-3.1-8b-instruct"):
        self.name = name
        self._endpoint = endpoint.rstrip("/")
        self._api_key = os.environ.get(api_key_env, "") if api_key_env else ""
        self._default_model = default_model
        try:
            import httpx  # type: ignore[import-not-found]
        except ImportError as exc:
            raise AiProviderError(
                "Pacchetto 'httpx' non installato (è già dipendenza dell'admin)."
            ) from exc
        self._httpx = httpx

    def _client(self, timeout_ms: int):
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return self._httpx.Client(
            base_url=self._endpoint,
            timeout=timeout_ms / 1000.0,
            headers=headers,
        )

    def complete(
        self,
        *,
        system: str,
        user: str,
        model: str,
        max_tokens: int = 1024,
        temperature: float = 0.0,
        timeout_ms: int = 5000,
        json_schema: dict[str, Any] | None = None,
        prompt_caching: bool = True,  # ignorato (non supportato lato OpenAI-compat)
    ) -> AiResponse:
        t0 = time.monotonic()
        body: dict[str, Any] = {
            "model": model or self._default_model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if json_schema:
            # Versione semplice OpenAI-compat: forza JSON object response
            body["response_format"] = {"type": "json_object"}

        try:
            with self._client(timeout_ms) as cli:
                resp = cli.post("/chat/completions", json=body)
        except Exception as exc:  # noqa: BLE001
            latency_ms = int((time.monotonic() - t0) * 1000)
            err_msg = f"{type(exc).__name__}: {exc}"
            logger.warning("LocalHttp error after %dms: %s", latency_ms, err_msg)
            return AiResponse(raw_text="", model=model, latency_ms=latency_ms,
                              error=err_msg, finish_reason="error")

        latency_ms = int((time.monotonic() - t0) * 1000)
        if resp.status_code >= 400:
            return AiResponse(raw_text="", model=model, latency_ms=latency_ms,
                              error=f"HTTP {resp.status_code}: {resp.text[:200]}",
                              finish_reason="error")

        data = resp.json()
        try:
            choice = data["choices"][0]
            raw_text = choice["message"].get("content", "") or ""
            finish = choice.get("finish_reason", "stop")
        except (KeyError, IndexError, TypeError) as exc:
            return AiResponse(raw_text="", model=model, latency_ms=latency_ms,
                              error=f"Risposta non OpenAI-compat: {exc}",
                              finish_reason="error")
        usage = data.get("usage") or {}
        input_tokens = int(usage.get("prompt_tokens", 0) or 0)
        output_tokens = int(usage.get("completion_tokens", 0) or 0)

        parsed_json: dict[str, Any] | None = None
        if json_schema and raw_text:
            try:
                parsed_json = json.loads(raw_text)
            except (TypeError, ValueError):
                parsed_json = None

        return AiResponse(
            raw_text=raw_text, parsed_json=parsed_json,
            input_tokens=input_tokens, output_tokens=output_tokens,
            cost_usd=0.0,  # self-hosted
            latency_ms=latency_ms, model=model, finish_reason=finish,
        )

    def health(self) -> dict[str, Any]:
        t0 = time.monotonic()
        try:
            with self._client(2000) as cli:
                resp = cli.get("/models")
            latency_ms = int((time.monotonic() - t0) * 1000)
            if resp.status_code == 200:
                return {"ok": True, "model": self._default_model,
                        "latency_ms": latency_ms, "error": None}
            return {"ok": False, "error": f"HTTP {resp.status_code}",
                    "latency_ms": latency_ms}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}

    def list_available_models(self) -> list[str]:
        try:
            with self._client(2000) as cli:
                resp = cli.get("/models")
            data = resp.json()
            return [m["id"] for m in data.get("data", [])]
        except Exception:  # noqa: BLE001
            return [self._default_model]
