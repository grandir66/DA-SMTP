"""Interfaccia astratta per provider IA.

Pattern factory analogo a :mod:`domarc_relay_admin.customer_sources.base`.
Permette di sostituire Claude API con DGX Spark locale (o altro endpoint
OpenAI-compatible) senza modificare il chiamante.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


class AiProviderError(Exception):
    """Errore comunicazione provider (rete, auth, schema invalido, timeout)."""


@dataclass
class AiResponse:
    """Risposta strutturata di un provider IA."""
    raw_text: str                                       # output testuale grezzo
    parsed_json: dict[str, Any] | None = None           # output strutturato (se richiesto)
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0
    model: str = ""
    error: str | None = None
    finish_reason: str | None = None                    # "stop", "max_tokens", "tool_use", "error"


class AiProvider(ABC):
    """Interfaccia astratta. Ogni implementazione concreta override ``complete``."""

    name: str = ""
    kind: str = ""        # 'claude' | 'openai_compat' | 'local_http'

    @abstractmethod
    def complete(
        self,
        *,
        system: str,
        user: str,
        model: str,
        max_tokens: int = 1024,
        temperature: float = 0.0,
        timeout_ms: int = 5000,
        json_schema: dict[str, Any] | None = None,      # se valorizzato, structured output
        prompt_caching: bool = True,
    ) -> AiResponse:
        """Esegue una completion. Se ``json_schema`` è valorizzato, il
        provider deve garantire output JSON valido (via tool_use, response_format,
        ecc.) e popolare ``AiResponse.parsed_json``.

        Solleva :class:`AiProviderError` su rete/auth. Su timeout ritorna
        :class:`AiResponse` con ``error`` valorizzato e ``finish_reason="error"``
        (non solleva — il caller decide se applicare fail-safe).
        """

    @abstractmethod
    def health(self) -> dict[str, Any]:
        """Returns ``{"ok": bool, "model": str, "latency_ms": int, "error": str|None}``."""

    @abstractmethod
    def list_available_models(self) -> list[str]:
        """Modelli disponibili per dropdown UI."""
