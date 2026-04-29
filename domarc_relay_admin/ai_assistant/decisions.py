"""Wrapper per le inferenze IA: orchestra redactor → router → provider → log decisione.

Punto di ingresso principale: :func:`classify_email`. Pattern simile per
:func:`summarize`, :func:`critical_classify` (one-liner che cambiano solo
``job_code`` e schema output).
"""
from __future__ import annotations

import hashlib
import logging
from datetime import date
from typing import TYPE_CHECKING, Any

from .pii_redactor import redact_event
from .providers import AiProviderError, AiResponse, get_ai_provider
from .router import AiRouter

if TYPE_CHECKING:
    from ..storage.base import Storage

logger = logging.getLogger(__name__)


# JSON Schemas per structured output
SCHEMA_CLASSIFY = {
    "type": "object",
    "properties": {
        "intent": {
            "type": "string",
            "enum": ["problema_tecnico", "richiesta_info", "spam", "errore_sistema",
                     "comunicazione_commerciale", "auto_notification", "altro"],
            "description": "Tipo principale della mail.",
        },
        "urgenza": {
            "type": "string",
            "enum": ["BASSA", "NORMALE", "ALTA", "CRITICA"],
            "description": "Urgenza percepita.",
        },
        "contains_error_indicators": {
            "type": "boolean",
            "description": "True se la mail contiene indicatori di errore tecnico (failure, error, timeout, ecc.).",
        },
        "summary": {
            "type": "string",
            "description": "Sintesi 1-2 frasi in italiano (max 200 char).",
        },
        "suggested_action": {
            "type": "string",
            "enum": ["create_ticket", "auto_reply", "flag_only", "ignore", "forward_to_commerciale"],
            "description": "Azione suggerita.",
        },
        "confidence": {
            "type": "number",
            "minimum": 0.0, "maximum": 1.0,
            "description": "Confidenza nella classificazione.",
        },
    },
    "required": ["intent", "urgenza", "summary", "suggested_action", "confidence"],
}


def _budget_remaining(storage: "Storage", tenant_id: int) -> tuple[float, float]:
    """Returns ``(spent_today_usd, daily_budget_usd)``."""
    today = date.today().isoformat()
    settings = {s["key"]: s["value"] for s in storage.list_settings()}
    budget = float(settings.get("ai_daily_budget_usd", "50") or 50)
    spent = storage.sum_ai_decisions_cost_today(tenant_id=tenant_id, day=today)
    return spent, budget


def _is_master_enabled(storage: "Storage") -> bool:
    settings = {s["key"]: s["value"] for s in storage.list_settings()}
    return (settings.get("ai_enabled", "false") or "").lower() == "true"


def _is_shadow_mode(storage: "Storage") -> bool:
    settings = {s["key"]: s["value"] for s in storage.list_settings()}
    return (settings.get("ai_shadow_mode", "true") or "").lower() == "true"


def _min_confidence_threshold(storage: "Storage") -> float:
    settings = {s["key"]: s["value"] for s in storage.list_settings()}
    try:
        return float(settings.get("ai_apply_min_confidence", "0.85") or 0.85)
    except (TypeError, ValueError):
        return 0.85


def classify_email(
    *,
    storage: "Storage",
    router: AiRouter,
    event: dict[str, Any],
    event_uuid: str | None = None,
    customer_context: dict[str, Any] | None = None,
    tenant_id: int = 1,
) -> dict[str, Any]:
    """Classifica un evento mail tramite il job ``classify_email``.

    Returns:
        dict serializzabile JSON con: ``decision_id, classification, urgenza,
        intent, summary, suggested_action, confidence, applied, shadow_mode,
        latency_ms, cost_usd, error``.
    """
    customer_context = customer_context or {}

    # Master switch
    if not _is_master_enabled(storage):
        return {"error": "ai_disabled", "skipped": True}

    # Budget check
    spent, budget = _budget_remaining(storage, tenant_id)
    if spent >= budget:
        return {"error": "budget_exhausted", "spent_usd": spent, "budget_usd": budget,
                "skipped": True}

    binding = router.pick_binding("classify_email")
    if binding is None:
        return {"error": "no_binding_configured", "skipped": True}

    # PII redaction
    redacted_event, redaction_result = redact_event(event, storage=storage,
                                                     tenant_id=tenant_id)

    # Render prompt
    ctx = {
        "from_domain": (event.get("from_address") or "").rpartition("@")[2],
        "to_address": event.get("to_address") or "",
        "subject": redacted_event.get("subject") or "",
        "body_text": redacted_event.get("body_text") or "",
        "customer_known": bool(customer_context.get("codcli")),
        "contract_active": bool(customer_context.get("contract_active")),
    }
    system_prompt, user_prompt = router.render_prompts(binding, ctx)
    prompt_hash = hashlib.sha256(
        (system_prompt + "||" + user_prompt).encode("utf-8")
    ).hexdigest()[:32]

    # Call provider
    error_msg: str | None = None
    fallback_used = False
    response: AiResponse
    try:
        provider = get_ai_provider(storage, binding.provider_id)
        response = provider.complete(
            system=system_prompt, user=user_prompt,
            model=binding.model_id, max_tokens=binding.max_tokens,
            temperature=binding.temperature, timeout_ms=binding.timeout_ms,
            json_schema=SCHEMA_CLASSIFY,
        )
        if response.error:
            error_msg = response.error
            # Fallback se configurato
            if binding.fallback_provider_id and binding.fallback_model_id:
                try:
                    fb_prov = get_ai_provider(storage, binding.fallback_provider_id)
                    response = fb_prov.complete(
                        system=system_prompt, user=user_prompt,
                        model=binding.fallback_model_id,
                        max_tokens=binding.max_tokens,
                        temperature=binding.temperature,
                        timeout_ms=binding.timeout_ms,
                        json_schema=SCHEMA_CLASSIFY,
                    )
                    if not response.error:
                        error_msg = None
                        fallback_used = True
                except AiProviderError as exc:
                    error_msg = f"primary+fallback failed: {error_msg} | {exc}"
    except AiProviderError as exc:
        response = AiResponse(raw_text="", model=binding.model_id,
                               latency_ms=0, error=str(exc),
                               finish_reason="error")
        error_msg = str(exc)

    parsed = response.parsed_json or {}
    shadow_global = _is_shadow_mode(storage)
    min_conf = _min_confidence_threshold(storage)
    confidence = parsed.get("confidence")
    try:
        confidence_value = float(confidence) if confidence is not None else 0.0
    except (TypeError, ValueError):
        confidence_value = 0.0

    # F3: la decisione viene applicata SOLO se TUTTE queste sono vere:
    # - master switch attivo (ai_enabled=true) — già verificato sopra
    # - shadow mode globale OFF
    # - confidence >= soglia (default 0.85)
    # - nessun errore IA
    # - decisione contiene un suggested_action valido
    will_apply = (
        not shadow_global
        and not error_msg
        and confidence_value >= min_conf
        and bool((parsed or {}).get("suggested_action"))
    )
    # Per audit: se la decisione "non viene applicata pur essendo in live mode",
    # la flagghiamo come shadow (col motivo) — più chiaro nei log che "shadow=true".
    effective_shadow = shadow_global or not will_apply

    decision_id = storage.insert_ai_decision({
        "tenant_id": tenant_id,
        "event_uuid": event_uuid,
        "job_code": "classify_email",
        "binding_id": binding.binding_id,
        "provider": binding.provider_name,
        "model": response.model,
        "prompt_hash": prompt_hash,
        "pii_redactions_count": redaction_result.count,
        "classification": parsed.get("intent"),
        "urgenza_proposta": parsed.get("urgenza"),
        "intent": parsed.get("intent"),
        "summary": parsed.get("summary"),
        "suggested_actions_json": parsed,
        "raw_output_json": parsed if parsed else {"raw": response.raw_text},
        "confidence": confidence,
        "latency_ms": response.latency_ms,
        "input_tokens": response.input_tokens,
        "output_tokens": response.output_tokens,
        "cost_usd": response.cost_usd,
        "applied": 1 if will_apply else 0,
        "shadow_mode": 1 if effective_shadow else 0,
        "error": error_msg,
        "fallback_used": 1 if fallback_used else 0,
    })

    # Reason di shadow per debug / dashboard
    shadow_reason = None
    if shadow_global:
        shadow_reason = "global_shadow_mode"
    elif error_msg:
        shadow_reason = "provider_error"
    elif confidence_value < min_conf:
        shadow_reason = f"low_confidence({confidence_value:.2f}<{min_conf:.2f})"
    elif not (parsed or {}).get("suggested_action"):
        shadow_reason = "no_suggested_action"

    return {
        "decision_id": decision_id,
        "classification": parsed.get("intent"),
        "urgenza": parsed.get("urgenza"),
        "intent": parsed.get("intent"),
        "summary": parsed.get("summary"),
        "suggested_action": parsed.get("suggested_action"),
        "confidence": confidence,
        "applied": will_apply,
        "shadow_mode": effective_shadow,
        "shadow_reason": shadow_reason,
        "latency_ms": response.latency_ms,
        "cost_usd": response.cost_usd,
        "fallback_used": fallback_used,
        "pii_redactions": redaction_result.count,
        "error": error_msg,
    }
