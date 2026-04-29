"""Modulo AI Assistant per Domarc SMTP Relay.

Architettura:

- :mod:`providers`: adapter pluggabili (Claude API, DGX Spark locale).
- :mod:`router`: routing per ``job_code`` con A/B traffic split, versioning,
  cache in-memory.
- :mod:`pii_redactor`: pipeline redazione PII prima di inviare al provider.
- :mod:`decisions`: wrapper per loggare ogni inferenza in ``ai_decisions``.
- :mod:`prompts`: template Jinja2 per ogni job_code.

Pattern factory identico a :mod:`domarc_relay_admin.customer_sources`.

L'intero modulo è governato da:
- ``setting.ai_enabled``: master switch (default false).
- ``setting.ai_shadow_mode``: se true, le decisioni vengono loggate ma non
  applicate (default true per la prima messa in produzione).
- ``setting.ai_daily_budget_usd``: budget giornaliero, fail-safe oltre.
"""
from .providers import get_ai_provider, AiProviderError
from .router import AiRouter, get_ai_router

__all__ = [
    "AiRouter",
    "get_ai_router",
    "get_ai_provider",
    "AiProviderError",
]
