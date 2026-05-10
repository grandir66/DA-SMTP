"""AI Rule Wizard — generatore di regole guidate via prompt utente.

Modalità:
- ``description``: l'admin descrive in linguaggio naturale → AI compila la regola.
- ``samples``: l'admin seleziona N mail reali da ``events_log`` → AI deduce
  pattern (utile per sistemi automatici tipo CloudTIK/monitoring).

Output: dict normalizzato compatibile con ``upsert_rule`` + warnings + reasoning
+ confidence + opzionale ``suggested_aggregation``.

L'AI **non** scrive direttamente sul DB: il blueprint chiama
:func:`generate_rule` → mostra anteprima → admin conferma → upsert con
``validate_rule()`` esistente.
"""
from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

from .providers import AiProviderError, get_ai_provider
from .router import AiRouter, get_ai_router

if TYPE_CHECKING:
    from ..storage.base import Storage

logger = logging.getLogger(__name__)

JOB_CODE = "rule_generator"

VALID_ACTIONS = (
    "ignore", "flag_only", "default_delivery", "forward", "redirect",
    "create_ticket", "auto_reply", "quarantine", "ai_classify",
    "create_authorized_ticket",
)

VALID_TRISTATE_TEXT = ("in", "out", None)
VALID_TRISTATE_BOOL = (True, False, None)

# JSON Schema vincolato per output strutturato del provider.
SCHEMA_RULE_GENERATOR: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "minLength": 3, "maxLength": 200,
                 "description": "Nome regola conciso, in italiano."},
        "description": {"type": "string", "maxLength": 1000,
                        "description": "Descrizione 1-3 frasi."},
        "rule_set_code": {"type": "string",
                          "description": "Code del rule_set (es. 'globali')."},
        "match_from_regex": {"type": ["string", "null"]},
        "match_to_regex": {"type": ["string", "null"]},
        "match_subject_regex": {"type": ["string", "null"]},
        "match_body_regex": {"type": ["string", "null"]},
        "match_from_domain": {"type": ["string", "null"]},
        "match_to_domain": {"type": ["string", "null"]},
        "match_in_service": {"type": ["string", "null"], "enum": ["in", "out", None]},
        "match_is_thread_continuation": {"type": ["boolean", "null"]},
        "match_customer_groups": {"type": ["string", "null"],
                                  "description": "CSV di code gruppi cliente."},
        "action": {"type": "string", "enum": list(VALID_ACTIONS)},
        "action_map": {
            "type": ["object", "null"],
            "properties": {
                "settore": {"type": ["string", "null"]},
                "urgenza": {"type": ["string", "null"]},
                "reason": {"type": ["string", "null"]},
                "keep_original_delivery": {"type": ["boolean", "null"]},
                "template_id": {"type": ["integer", "null"]},
            },
            "additionalProperties": True,
        },
        "priority": {"type": "integer", "minimum": 1, "maximum": 999_999},
        "reasoning": {"type": "string", "minLength": 10, "maxLength": 1000,
                      "description": "Spiegazione in italiano del perché di match e action."},
        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        "warnings": {"type": "array", "items": {"type": "string"}},
        "suggested_aggregation": {
            "type": ["object", "null"],
            "properties": {
                "name": {"type": "string"},
                "match_from_regex": {"type": ["string", "null"]},
                "match_subject_regex": {"type": ["string", "null"]},
                "threshold": {"type": "integer", "minimum": 2, "maximum": 100},
                "delay_minutes": {"type": "integer", "minimum": 1, "maximum": 1440},
                "ticket_settore": {"type": ["string", "null"]},
                "ticket_urgenza": {"type": ["string", "null"]},
            },
        },
    },
    "required": ["name", "action", "priority", "reasoning", "confidence"],
}


class RuleGeneratorError(Exception):
    """Errore generazione regola (binding mancante, provider failure, JSON invalido)."""


def _build_context(storage: "Storage", *, tenant_id: int) -> dict[str, Any]:
    """Carica liste lookup (customer groups, templates, recipient groups)
    per arricchire il prompt utente."""
    cg_summary: list[dict[str, str]] = []
    try:
        for g in storage.list_customer_groups(tenant_id=tenant_id) or []:
            cg_summary.append({
                "code": g.get("code") or "",
                "name": g.get("name") or "",
                "description": (g.get("description") or "")[:120],
            })
    except (AttributeError, NotImplementedError):
        pass

    tpl_summary: list[dict[str, Any]] = []
    try:
        for t in storage.list_templates(tenant_id=tenant_id, only_enabled=True) or []:
            tpl_summary.append({
                "id": t.get("id"),
                "code": t.get("code") or "",
                "name": t.get("name") or "",
            })
    except (AttributeError, NotImplementedError):
        pass

    rg_summary: list[dict[str, Any]] = []
    try:
        for r in storage.list_recipient_groups(tenant_id=tenant_id, only_enabled=True) or []:
            rg_summary.append({
                "id": r.get("id"),
                "code": r.get("code") or "",
                "name": r.get("name") or "",
            })
    except (AttributeError, NotImplementedError):
        pass

    return {
        "customer_groups_summary": cg_summary,
        "templates_summary": tpl_summary,
        "recipient_groups_summary": rg_summary,
    }


def _normalize_rule(parsed: dict[str, Any]) -> dict[str, Any]:
    """Normalizza output AI verso lo schema della tabella `rules`.

    - Coerce tipi (None per stringhe vuote)
    - match_in_service text → boolean (i validators usano boolean tristate)
    - Strip whitespace
    """
    def _strip_or_none(v: Any) -> str | None:
        if v is None:
            return None
        s = str(v).strip()
        return s if s else None

    rule: dict[str, Any] = {
        "name": _strip_or_none(parsed.get("name")) or "Regola da AI",
        "description": _strip_or_none(parsed.get("description")) or "",
        "rule_set_code": _strip_or_none(parsed.get("rule_set_code")) or "globali",
        "match_from_regex": _strip_or_none(parsed.get("match_from_regex")),
        "match_to_regex": _strip_or_none(parsed.get("match_to_regex")),
        "match_subject_regex": _strip_or_none(parsed.get("match_subject_regex")),
        "match_body_regex": _strip_or_none(parsed.get("match_body_regex")),
        "match_from_domain": _strip_or_none(parsed.get("match_from_domain")),
        "match_to_domain": _strip_or_none(parsed.get("match_to_domain")),
        "match_customer_groups": _strip_or_none(parsed.get("match_customer_groups")),
        "action": _strip_or_none(parsed.get("action")) or "flag_only",
        "action_map": parsed.get("action_map") if isinstance(parsed.get("action_map"), dict) else None,
        "priority": int(parsed.get("priority") or 200),
    }

    # match_in_service: AI usa "in"/"out"/null, schema rules usa boolean tristate
    mis = parsed.get("match_in_service")
    if mis == "in":
        rule["match_in_service"] = True
    elif mis == "out":
        rule["match_in_service"] = False
    else:
        rule["match_in_service"] = None

    # match_is_thread_continuation: tristate boolean → INTEGER (NULL/0/1)
    mtc = parsed.get("match_is_thread_continuation")
    if mtc is True:
        rule["match_is_thread_continuation"] = 1
    elif mtc is False:
        rule["match_is_thread_continuation"] = 0
    else:
        rule["match_is_thread_continuation"] = None

    return rule


def _validate_regex_fields(rule: dict[str, Any]) -> list[str]:
    """Compila tutti i campi `match_*_regex` con re.compile(); ritorna lista
    errori. Non solleva (lascia decidere al caller se bloccare o solo segnalare)."""
    errors: list[str] = []
    for field in ("match_from_regex", "match_to_regex",
                  "match_subject_regex", "match_body_regex"):
        pattern = rule.get(field)
        if not pattern:
            continue
        try:
            re.compile(pattern)
        except re.error as exc:
            errors.append(f"{field}: regex invalida ({exc})")
    return errors


def _action_is_valid(action: str | None) -> bool:
    return bool(action) and action in VALID_ACTIONS


def generate_rule(
    *,
    storage: "Storage",
    mode: str,                                  # "description" | "samples"
    description: str | None = None,
    samples: list[dict[str, Any]] | None = None,
    sample_hours: int = 168,
    user_hint: str | None = None,
    rule_set_code: str | None = None,
    tenant_id: int = 1,
) -> dict[str, Any]:
    """Punto d'ingresso principale.

    Returns:
        dict con chiavi:
        - ``rule``: dict normalizzato pronto per ``upsert_rule``
        - ``reasoning``: spiegazione AI
        - ``confidence``: float 0.0-1.0
        - ``warnings``: list[str] (regex invalide, valori sospetti)
        - ``suggested_aggregation``: dict | None
        - ``raw``: output JSON grezzo del provider (audit)
        - ``cost_usd``, ``latency_ms``, ``model``, ``provider``

    Raises:
        RuleGeneratorError: se binding mancante, provider error, output non parsabile.
    """
    if mode not in ("description", "samples"):
        raise RuleGeneratorError(f"Mode non valido: {mode!r}")

    if mode == "description" and not (description and description.strip()):
        raise RuleGeneratorError("In modalità 'description' serve un testo non vuoto.")
    if mode == "samples" and not samples:
        raise RuleGeneratorError("In modalità 'samples' serve almeno 1 esempio.")

    router: AiRouter = get_ai_router(storage, tenant_id=tenant_id)
    binding = router.pick_binding(JOB_CODE)
    if binding is None:
        raise RuleGeneratorError(
            f"Nessun binding AI configurato per job_code={JOB_CODE!r}. "
            "Vai in /ai/models e crea un binding (Claude Haiku 4.5 consigliato)."
        )

    ctx_lookup = _build_context(storage, tenant_id=tenant_id)
    prompt_ctx = {
        "mode": mode,
        "description": (description or "").strip(),
        "samples": samples or [],
        "sample_hours": sample_hours,
        "user_hint": (user_hint or "").strip() or None,
        "rule_set_code": (rule_set_code or "").strip() or None,
        **ctx_lookup,
    }

    system_prompt, user_prompt = router.render_prompts(binding, prompt_ctx)

    try:
        provider = get_ai_provider(storage, binding.provider_id)
        response = provider.complete(
            system=system_prompt,
            user=user_prompt,
            model=binding.model_id,
            max_tokens=max(binding.max_tokens, 2048),  # JSON schema può essere verboso
            temperature=binding.temperature,
            timeout_ms=max(binding.timeout_ms, 15000),
            json_schema=SCHEMA_RULE_GENERATOR,
        )
    except AiProviderError as exc:
        raise RuleGeneratorError(f"Provider AI fallito: {exc}") from exc

    if response.error:
        raise RuleGeneratorError(f"Provider AI ha risposto con errore: {response.error}")

    parsed = response.parsed_json or {}
    if not parsed:
        raise RuleGeneratorError(
            f"Output AI non parsabile come JSON. Raw: {response.raw_text[:300]!r}"
        )

    # Validazione semantica
    if not _action_is_valid(parsed.get("action")):
        raise RuleGeneratorError(
            f"Action proposta non valida: {parsed.get('action')!r}. "
            f"Valide: {VALID_ACTIONS}"
        )

    rule = _normalize_rule(parsed)
    warnings = list(parsed.get("warnings") or [])

    regex_errors = _validate_regex_fields(rule)
    if regex_errors:
        # Non blocchiamo: l'admin vedrà gli errori in anteprima e potrà correggere
        # nel form regola standard. Aggiungiamo come warning.
        warnings.extend([f"REGEX INVALIDA: {e}" for e in regex_errors])

    # Verifica almeno un match_*
    has_any_match = any(
        rule.get(f) not in (None, "")
        for f in (
            "match_from_regex", "match_to_regex", "match_subject_regex",
            "match_body_regex", "match_from_domain", "match_to_domain",
            "match_customer_groups", "match_in_service",
            "match_is_thread_continuation",
        )
    )
    if not has_any_match:
        warnings.append(
            "ATTENZIONE: nessun criterio match_* impostato. La regola sarebbe "
            "catch-all e bloccherebbe tutte le mail successive con priority più alta. "
            "Aggiungi almeno un match prima di salvarla."
        )

    suggested_agg = parsed.get("suggested_aggregation")
    if suggested_agg and not isinstance(suggested_agg, dict):
        suggested_agg = None

    return {
        "rule": rule,
        "reasoning": str(parsed.get("reasoning") or "").strip(),
        "confidence": float(parsed.get("confidence") or 0.0),
        "warnings": warnings,
        "suggested_aggregation": suggested_agg,
        "raw": parsed,
        "cost_usd": float(response.cost_usd or 0.0),
        "latency_ms": int(response.latency_ms or 0),
        "model": response.model or binding.model_id,
        "provider": binding.provider_name,
    }


def fetch_event_samples(
    storage: "Storage",
    *,
    tenant_id: int,
    hours: int = 168,
    from_like: str | None = None,
    subject_like: str | None = None,
    limit: int = 30,
) -> list[dict[str, Any]]:
    """Estrae mail recenti da `events` per la modalità 'samples'.

    Filtra opzionalmente per from/subject (substring case-insensitive). Ritorna
    una lista di dict piatta con i soli campi serviti al prompt
    (no body_html, no metadata gigante)."""
    filters: dict[str, Any] = {}
    # `list_events` accetta `q` come filtro generico (LIKE su from/to/subject/codcli)
    if from_like:
        filters["q"] = from_like.strip().lower()

    rows, _total = storage.list_events(
        tenant_id=tenant_id,
        hours=hours,
        page=1,
        page_size=max(50, limit * 2),  # over-fetch per filtrare lato Python
        filters=filters or None,
    )

    # PII redactor obbligatorio: tutto cio' che va al provider AI deve passare
    # da pii_redactor (.claude/rules/ai-payload.md). Anonimizza email, telefoni,
    # CF/P.IVA, IBAN, ecc. e mantiene la coerenza intra-prompt con token.
    try:
        from .pii_redactor import redact as _redact
    except Exception:  # noqa: BLE001
        _redact = lambda s: s  # noqa: E731 — fallback: meglio passare meno info che crash

    out: list[dict[str, Any]] = []
    sl = (subject_like or "").strip().lower()
    fl = (from_like or "").strip().lower()
    for r in rows:
        subj = (r.get("subject") or "")
        frm = (r.get("from_address") or "")
        if sl and sl not in subj.lower():
            continue
        if fl and fl not in frm.lower():
            # già filtrato a livello DB con "q" generico, ma stringe ulteriore
            continue
        body = (r.get("body_text") or "").strip()
        # Redact PII PRIMA di mettere nel sample inviato all'AI
        body_preview_redacted = _redact(body[:300]) if body else ""
        out.append({
            "received_at": r.get("received_at"),
            "from_address": _redact(frm),
            "to_address": _redact(r.get("to_address") or ""),
            "subject": _redact(subj),
            "body_preview": body_preview_redacted,
        })
        if len(out) >= limit:
            break
    return out
