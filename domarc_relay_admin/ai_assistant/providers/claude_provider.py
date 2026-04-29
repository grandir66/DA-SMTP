"""Provider Claude API (Anthropic SDK).

Usa structured output via ``tool_use`` per garanzia JSON. Prompt caching
attivo by default per ridurre costi sui system prompt ripetitivi.

Catalogo prezzi (per 1M token, 2026-04, da aggiornare se cambiano):

- claude-haiku-4-5         : $1.00 input / $5.00 output
- claude-sonnet-4-6        : $3.00 input / $15.00 output
- claude-opus-4-7          : $15.00 input / $75.00 output
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

from .base import AiProvider, AiProviderError, AiResponse

logger = logging.getLogger(__name__)


# Prezzi $/1M token. Modificabile se Anthropic aggiorna.
_PRICES_PER_M_TOKENS = {
    "claude-haiku-4-5":      {"input": 1.00, "output": 5.00},
    "claude-haiku-4-5-20251001": {"input": 1.00, "output": 5.00},
    "claude-sonnet-4-6":     {"input": 3.00, "output": 15.00},
    "claude-opus-4-7":       {"input": 15.00, "output": 75.00},
}


def _calc_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    pricing = _PRICES_PER_M_TOKENS.get(model)
    if not pricing:
        return 0.0
    return (
        (input_tokens / 1_000_000) * pricing["input"]
        + (output_tokens / 1_000_000) * pricing["output"]
    )


class ClaudeProvider(AiProvider):
    """Provider Anthropic Claude."""

    kind = "claude"

    def __init__(self, *, name: str, api_key_env: str = "ANTHROPIC_API_KEY",
                 endpoint: str | None = None):
        self.name = name
        self._api_key_env = api_key_env
        api_key = os.environ.get(api_key_env, "").strip()
        if not api_key:
            raise AiProviderError(
                f"API key mancante: env var '{api_key_env}' non impostata. "
                "Configura il provider in /etc/domarc-smtp-relay-admin/secrets.env"
            )
        try:
            import anthropic  # type: ignore[import-not-found]
        except ImportError as exc:
            raise AiProviderError(
                "Pacchetto 'anthropic' non installato. Lancia: "
                "/opt/domarc-smtp-relay-admin/.venv/bin/pip install anthropic"
            ) from exc
        client_kwargs: dict[str, Any] = {"api_key": api_key}
        if endpoint:
            client_kwargs["base_url"] = endpoint
        self._client = anthropic.Anthropic(**client_kwargs)

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
        prompt_caching: bool = True,
    ) -> AiResponse:
        t0 = time.monotonic()
        msg_kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "timeout": timeout_ms / 1000.0,
        }
        # Prompt caching (Anthropic): la system prompt viene cached se ha la beta header
        if prompt_caching and system:
            msg_kwargs["system"] = [
                {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}
            ]
        elif system:
            msg_kwargs["system"] = system

        # Structured output via tool_use
        if json_schema:
            msg_kwargs["tools"] = [{
                "name": "respond_structured",
                "description": "Restituisci la risposta strutturata.",
                "input_schema": json_schema,
            }]
            msg_kwargs["tool_choice"] = {"type": "tool", "name": "respond_structured"}

        msg_kwargs["messages"] = [{"role": "user", "content": user}]

        try:
            resp = self._client.messages.create(**msg_kwargs)
        except Exception as exc:  # noqa: BLE001
            latency_ms = int((time.monotonic() - t0) * 1000)
            err_msg = f"{type(exc).__name__}: {exc}"
            logger.warning("Claude error after %dms: %s", latency_ms, err_msg)
            return AiResponse(
                raw_text="", parsed_json=None, model=model,
                latency_ms=latency_ms, error=err_msg, finish_reason="error",
            )

        latency_ms = int((time.monotonic() - t0) * 1000)
        input_tokens = getattr(resp.usage, "input_tokens", 0) if resp.usage else 0
        output_tokens = getattr(resp.usage, "output_tokens", 0) if resp.usage else 0
        cost = _calc_cost(model, input_tokens, output_tokens)
        finish = getattr(resp, "stop_reason", None) or "stop"

        # Estrai output: tool_use se presente, altrimenti text
        raw_text = ""
        parsed_json: dict[str, Any] | None = None
        for block in resp.content or []:
            block_type = getattr(block, "type", None)
            if block_type == "tool_use" and getattr(block, "name", "") == "respond_structured":
                parsed_json = dict(getattr(block, "input", {}) or {})
                raw_text = json.dumps(parsed_json, ensure_ascii=False)
                finish = "tool_use"
                break
            if block_type == "text":
                raw_text += getattr(block, "text", "") or ""

        # Fallback: se json_schema richiesto ma niente tool_use, prova a parsare il text
        if json_schema and parsed_json is None and raw_text:
            try:
                parsed_json = json.loads(raw_text)
            except (TypeError, ValueError):
                parsed_json = None

        return AiResponse(
            raw_text=raw_text,
            parsed_json=parsed_json,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost,
            latency_ms=latency_ms,
            model=model,
            finish_reason=finish,
        )

    def health(self) -> dict[str, Any]:
        try:
            resp = self.complete(
                system="Sei un assistente. Rispondi solo con la parola OK.",
                user="ping",
                model=self._default_health_model(),
                max_tokens=10,
                timeout_ms=5000,
            )
            return {
                "ok": resp.error is None and "OK" in resp.raw_text.upper(),
                "model": resp.model,
                "latency_ms": resp.latency_ms,
                "error": resp.error,
            }
        except AiProviderError as exc:
            return {"ok": False, "error": str(exc)}

    def _default_health_model(self) -> str:
        # Modello più economico per ping
        return "claude-haiku-4-5"

    def list_available_models(self) -> list[str]:
        return [
            "claude-haiku-4-5",
            "claude-sonnet-4-6",
            "claude-opus-4-7",
        ]
