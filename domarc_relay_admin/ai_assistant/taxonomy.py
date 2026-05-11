"""Classificatore tassonomico delle mail (job `email_taxonomy`).

Output: una categoria macro + sub-categoria libera + confidence + rationale.
Diversamente da `classify_email` (urgenza/intent/suggested_action), questa
funzione SERVE SOLO ad etichettare per KPI. Niente azioni applicate.

Categorie macro fisse — costruite per uso aziendale tipico:
  - newsletter_marketing: newsletter, promozioni, contenuti marketing
  - notifica_automatica:  alert, backup status, monitoring, report periodici
  - transazionale:        fatture, ordini, ricevute, conferme acquisto
  - richiesta_assistenza: utente chiede aiuto / problema tecnico
  - comunicazione_commerciale: preventivi, offerte, vendor outreach
  - comunicazione_personale:  scambio umano, conversazione interna
  - documento_allegato:   mail prevalentemente con allegato (PDF, fattura)
  - pec_legale:           PEC, comunicazioni legali, raccomandate
  - phishing_spam_sospetto: pattern sospetti, spam, phishing
  - bounce_dsn:           bounce / Delivery Status Notification (RFC 3464)
  - autoresponder:        out-of-office, vacation reply, auto-responder
  - altro:                non classificabile in modo netto
"""
from __future__ import annotations

import hashlib
import json
import logging
from typing import TYPE_CHECKING, Any

from .pii_redactor import redact_event
from .providers.base import AiProviderError, AiResponse
from .router import AiRouter

if TYPE_CHECKING:
    from ..storage import Storage

logger = logging.getLogger(__name__)


TAXONOMY_CATEGORIES = [
    ("newsletter_marketing",      "Newsletter, marketing, promozioni"),
    ("notifica_automatica",       "Alert, backup, monitoring, report automatici"),
    ("transazionale",             "Fatture, ordini, conferme, ricevute"),
    ("richiesta_assistenza",      "Utente chiede aiuto / problema tecnico"),
    ("comunicazione_commerciale", "Preventivi, offerte, vendor"),
    ("comunicazione_personale",   "Scambio umano, conversazione personale/interna"),
    ("documento_allegato",        "Mail prevalentemente con allegato (PDF, fattura)"),
    ("pec_legale",                "PEC / comunicazioni legali"),
    ("phishing_spam_sospetto",    "Pattern sospetti, spam, phishing"),
    ("bounce_dsn",                "Bounce / DSN / Delivery Status Notification"),
    ("autoresponder",             "Out-of-office, vacation reply"),
    ("altro",                     "Non classificabile chiaramente"),
]
VALID_CATEGORY_CODES = {c for c, _ in TAXONOMY_CATEGORIES}


SCHEMA_TAXONOMY = {
    "type": "object",
    "properties": {
        "category": {
            "type": "string",
            "enum": list(VALID_CATEGORY_CODES),
            "description": "Una delle categorie macro",
        },
        "subcategory": {
            "type": "string",
            "description": "Sub-categoria libera, max 40 char (es. 'cloudtik_backup_alert', 'preventivo_software', 'fattura_fornitore'). Vuoto se non utile.",
        },
        "confidence": {
            "type": "number",
            "minimum": 0.0,
            "maximum": 1.0,
        },
        "rationale": {
            "type": "string",
            "description": "1-2 frasi che spiegano la scelta. Italiano.",
        },
    },
    "required": ["category", "confidence"],
}


_SYSTEM_PROMPT = """\
Sei un classificatore tassonomico di email aziendali italiane. Ricevi i metadati
e il testo redatto di una mail (PII anonimizzata con token tipo [EMAIL_1]).

Devi assegnare UNA delle seguenti categorie:

{categories}

Linee guida:
- Usa SOLO i codici delle categorie sopra (campo `category`).
- Se non sei certo, usa "altro" con confidence bassa (<0.5).
- "subcategory" e' libero, italiano, max 40 chars: usa snake_case per pattern
  ricorrenti (es. "cloudtik_backup", "fattura_elettronica", "vendor_x_offerta").
- "rationale" deve essere conciso: 1-2 frasi in italiano.
- Non applicare giudizi morali; classifica per FORMA e CONTENUTO della mail.
- Una mail puo' essere "phishing_spam_sospetto" anche senza prove definitive,
  basta che il pattern sembri sospetto (spoofed sender, urgenza fake, link strani).
"""


_USER_PROMPT_TEMPLATE = """\
Mail da classificare (PII redatta):

From: {from_addr}
To: {to_addr}
Subject: {subject}

Body (max 2000 char):
{body_text}
"""


def classify_taxonomy(
    *,
    storage: "Storage",
    router: AiRouter,
    event: dict[str, Any],
    event_uuid: str | None = None,
    customer_context: dict[str, Any] | None = None,
    tenant_id: int = 1,
    model_id_override: str | None = None,
) -> dict[str, Any]:
    """Classifica una mail in una categoria macro per KPI.

    Salva sempre in `ai_decisions` (job_code='email_taxonomy'). Mai applicata
    a livello pipeline: e' un job di osservazione.
    """
    customer_context = customer_context or {}

    # Master switch + budget — stessa logica di classify_email
    settings_dict = {s["key"]: s["value"] for s in storage.list_settings()}
    if (settings_dict.get("ai_enabled", "true") or "true").lower() != "true":
        return {"error": "ai_disabled", "skipped": True}

    binding = router.pick_binding("email_taxonomy")
    if binding is None:
        return {"error": "no_binding_configured", "skipped": True}
    if model_id_override:
        binding = type(binding)(**{**binding.__dict__, "model_id": model_id_override})

    # PII redaction
    redacted, _ = redact_event(event, storage=storage, tenant_id=tenant_id)

    # Build prompt
    categories_block = "\n".join(
        f"- {code}: {label}" for code, label in TAXONOMY_CATEGORIES
    )
    system_prompt = _SYSTEM_PROMPT.format(categories=categories_block)
    body_text = (redacted.get("body_text") or "")[:2000]
    user_prompt = _USER_PROMPT_TEMPLATE.format(
        from_addr=(event.get("from_address") or "—"),
        to_addr=(redacted.get("to_address") or event.get("to_address") or "—"),
        subject=(redacted.get("subject") or event.get("subject") or "(no subject)"),
        body_text=body_text or "(no body)",
    )

    prompt_hash = hashlib.sha256(
        (system_prompt + "||" + user_prompt).encode("utf-8")
    ).hexdigest()[:32]

    # Call provider
    response: AiResponse
    error_msg: str | None = None
    try:
        from .providers import get_ai_provider
        provider = get_ai_provider(storage, binding.provider_id)
        response = provider.complete(
            system=system_prompt, user=user_prompt,
            model=binding.model_id, max_tokens=binding.max_tokens,
            temperature=binding.temperature, timeout_ms=binding.timeout_ms,
            json_schema=SCHEMA_TAXONOMY,
        )
        if response.error:
            error_msg = response.error
    except AiProviderError as exc:
        response = AiResponse(raw_text="", model=binding.model_id,
                               latency_ms=0, error=str(exc),
                               finish_reason="error")
        error_msg = str(exc)

    parsed = response.parsed_json or {}
    category = (parsed.get("category") or "").strip()
    if category not in VALID_CATEGORY_CODES:
        category = "altro"
    subcategory = (parsed.get("subcategory") or "").strip()[:40] or None
    confidence = parsed.get("confidence")
    rationale = (parsed.get("rationale") or "").strip()[:500] or None

    # Salva sempre in ai_decisions (audit), MAI applicato (taxonomy = solo log)
    decision_id = None
    try:
        decision_id = storage.insert_ai_decision({
            "tenant_id": tenant_id,
            "event_uuid": event_uuid,
            "job_code": "email_taxonomy",
            "binding_id": binding.binding_id,
            "provider": getattr(binding, "provider_name", None),
            "model": response.model or binding.model_id,
            "prompt_hash": prompt_hash,
            "pii_redactions_count": 0,
            "classification": category,
            "urgenza_proposta": None,
            "intent": subcategory,  # riusa la colonna intent per sub-categoria
            "summary": rationale,   # riusa summary per rationale
            "suggested_actions_json": None,
            "raw_output_json": json.dumps(parsed) if parsed else None,
            "confidence": confidence,
            "latency_ms": response.latency_ms,
            "input_tokens": response.input_tokens,
            "output_tokens": response.output_tokens,
            "cost_usd": response.cost_usd,
            "applied": 0,           # MAI applicato (taxonomy = solo log)
            "shadow_mode": 1,       # convenzione: taxonomy e' sempre "shadow"
            "error": error_msg,
            "fallback_used": 0,
        })
    except Exception as exc:  # noqa: BLE001
        logger.warning("insert_ai_decision (taxonomy) failed: %s", exc)

    return {
        "decision_id": decision_id,
        "job_code": "email_taxonomy",
        "category": category,
        "subcategory": subcategory,
        "confidence": confidence,
        "rationale": rationale,
        "model": response.model or binding.model_id,
        "latency_ms": response.latency_ms,
        "cost_usd": response.cost_usd,
        "error": error_msg,
        "applied": False,
        "shadow_mode": True,
    }
