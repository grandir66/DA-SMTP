"""Factory pluggabile per provider IA.

Pattern identico a :mod:`domarc_relay_admin.customer_sources`. Lookup per
``provider_id`` nel DB → istanzia il provider corretto.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from .base import AiProvider, AiProviderError, AiResponse

if TYPE_CHECKING:
    from ...storage.base import Storage


def build_provider(provider_row: dict) -> AiProvider:
    """Costruisce un provider dal record DB.

    Args:
        provider_row: dict da `ai_providers` (id, name, kind, endpoint, api_key_env, default_model).

    Raises:
        AiProviderError: se il kind non è riconosciuto o la config non è valida.
    """
    kind = (provider_row.get("kind") or "").lower()
    name = provider_row.get("name") or "unnamed"
    if kind == "claude":
        from .claude_provider import ClaudeProvider
        return ClaudeProvider(
            name=name,
            api_key_env=provider_row.get("api_key_env") or "ANTHROPIC_API_KEY",
            endpoint=provider_row.get("endpoint") or None,
        )
    if kind in ("openai_compat", "local_http"):
        from .local_http_provider import LocalHttpProvider
        endpoint = provider_row.get("endpoint")
        if not endpoint:
            raise AiProviderError(f"Provider {name} (kind={kind}) richiede 'endpoint'")
        return LocalHttpProvider(
            name=name,
            endpoint=endpoint,
            api_key_env=provider_row.get("api_key_env"),
            default_model=provider_row.get("default_model") or "llama-3.1-8b-instruct",
        )
    raise AiProviderError(f"Kind provider non supportato: {kind!r}")


def get_ai_provider(storage: "Storage", provider_id: int) -> AiProvider:
    """Carica un provider attivo dal DB e ne istanzia il client.

    Restituisce l'istanza per il caller. Solleva :class:`AiProviderError` se
    il provider non esiste, è disabilitato o ha config invalida.
    """
    rows = [r for r in storage.list_ai_providers() if r["id"] == provider_id]
    if not rows:
        raise AiProviderError(f"Provider id={provider_id} non trovato")
    row = rows[0]
    if not row.get("enabled"):
        raise AiProviderError(f"Provider id={provider_id} ({row.get('name')}) disabilitato")
    return build_provider(row)


__all__ = ["AiProvider", "AiProviderError", "AiResponse", "build_provider", "get_ai_provider"]
