"""Router: lookup binding (job_code → provider+model+config) e dispatch.

Cache in-memory dei bindings attivi, invalidata sui write da admin UI.
Supporta traffic split A/B su più bindings attivi per lo stesso job_code.
"""
from __future__ import annotations

import logging
import random
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

if TYPE_CHECKING:
    from ..storage.base import Storage

logger = logging.getLogger(__name__)


@dataclass
class ResolvedBinding:
    binding_id: int
    job_code: str
    provider_id: int
    provider_name: str
    provider_kind: str
    model_id: str
    system_prompt: str          # già renderizzato con i context vars
    user_prompt: str            # già renderizzato
    temperature: float
    max_tokens: int
    timeout_ms: int
    fallback_provider_id: int | None
    fallback_model_id: str | None
    version: int


class AiRouter:
    """Stateful router.

    - cache `bindings_by_job` invalidata via :meth:`invalidate_cache`
    - `pick_binding(job_code)` rispetta `traffic_split` se più bindings attivi
    - `render_prompts(binding, ctx)` sostituisce le variabili Jinja2 dai
      template del binding (oppure dal file `prompts/<job_code>.j2` se il
      binding non ha template proprio)
    """

    def __init__(self, storage: "Storage", *, tenant_id: int = 1):
        self._storage = storage
        self._tenant_id = tenant_id
        self._cache_lock = threading.Lock()
        self._bindings_cache: dict[str, list[dict]] | None = None
        self._providers_cache: dict[int, dict] | None = None
        prompts_dir = Path(__file__).parent / "prompts"
        self._jinja = Environment(
            loader=FileSystemLoader(str(prompts_dir)),
            autoescape=select_autoescape(disabled_extensions=("j2",)),
            trim_blocks=True, lstrip_blocks=True,
        )

    def invalidate_cache(self) -> None:
        with self._cache_lock:
            self._bindings_cache = None
            self._providers_cache = None
        logger.debug("AiRouter cache invalidata")

    def _ensure_cache(self) -> None:
        with self._cache_lock:
            if self._bindings_cache is not None:
                return
            bindings = self._storage.list_ai_job_bindings(
                tenant_id=self._tenant_id, only_enabled=True,
            )
            providers = {p["id"]: p for p in self._storage.list_ai_providers(
                tenant_id=self._tenant_id,
            )}
            by_job: dict[str, list[dict]] = {}
            for b in bindings:
                by_job.setdefault(b["job_code"], []).append(b)
            self._bindings_cache = by_job
            self._providers_cache = providers

    def pick_binding(self, job_code: str) -> ResolvedBinding | None:
        self._ensure_cache()
        candidates = (self._bindings_cache or {}).get(job_code, [])
        if not candidates:
            return None
        # Traffic split: somma traffic_split, pick weighted random
        total = sum(int(c.get("traffic_split") or 100) for c in candidates)
        if total <= 0:
            return None
        pick = random.uniform(0, total)
        running = 0.0
        chosen = candidates[0]
        for c in candidates:
            running += int(c.get("traffic_split") or 100)
            if pick <= running:
                chosen = c
                break

        provider = (self._providers_cache or {}).get(chosen.get("provider_id"))
        if not provider:
            logger.warning("AiRouter: provider_id=%s non trovato per binding %s",
                           chosen.get("provider_id"), chosen.get("id"))
            return None

        return ResolvedBinding(
            binding_id=int(chosen["id"]),
            job_code=job_code,
            provider_id=int(chosen["provider_id"]),
            provider_name=str(provider.get("name") or ""),
            provider_kind=str(provider.get("kind") or ""),
            model_id=str(chosen.get("model_id") or provider.get("default_model") or ""),
            system_prompt=str(chosen.get("system_prompt_template") or ""),
            user_prompt=str(chosen.get("user_prompt_template") or ""),
            temperature=float(chosen.get("temperature", 0.0) or 0.0),
            max_tokens=int(chosen.get("max_tokens", 1024) or 1024),
            timeout_ms=int(chosen.get("timeout_ms") or 5000),
            fallback_provider_id=chosen.get("fallback_provider_id"),
            fallback_model_id=chosen.get("fallback_model_id"),
            version=int(chosen.get("version", 1) or 1),
        )

    def render_prompts(self, binding: ResolvedBinding,
                       context: dict[str, Any]) -> tuple[str, str]:
        """Renderizza i prompt template Jinja2.

        Se il binding ha system_prompt_template/user_prompt_template stringa
        non vuota, li renderizza come template inline. Altrimenti carica
        ``prompts/<job_code>.j2`` (template di default per il job).
        """
        sys_tmpl = binding.system_prompt
        usr_tmpl = binding.user_prompt
        if not sys_tmpl or not usr_tmpl:
            try:
                file_tmpl = self._jinja.get_template(f"{binding.job_code}.j2")
                rendered_default = file_tmpl.render(**context)
                # Convenzione: il file ha 2 sezioni "## SYSTEM" e "## USER"
                if "## USER" in rendered_default:
                    sys_part, usr_part = rendered_default.split("## USER", 1)
                    sys_tmpl = sys_tmpl or sys_part.replace("## SYSTEM", "").strip()
                    usr_tmpl = usr_tmpl or usr_part.strip()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Template default per %s non disponibile: %s",
                               binding.job_code, exc)

        sys_rendered = self._jinja.from_string(sys_tmpl).render(**context) if sys_tmpl else ""
        usr_rendered = self._jinja.from_string(usr_tmpl).render(**context) if usr_tmpl else ""
        return sys_rendered, usr_rendered


_router_singleton: AiRouter | None = None
_router_lock = threading.Lock()


def get_ai_router(storage: "Storage", *, tenant_id: int = 1) -> AiRouter:
    """Singleton globale dell'AiRouter."""
    global _router_singleton
    with _router_lock:
        if _router_singleton is None:
            _router_singleton = AiRouter(storage, tenant_id=tenant_id)
        return _router_singleton


def reset_router() -> None:
    """Per test: forza ri-creazione del singleton."""
    global _router_singleton
    with _router_lock:
        _router_singleton = None
