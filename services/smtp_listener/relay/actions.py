"""Implementazione delle azioni della pipeline.

Sette azioni supportate, allineate con `ingestion_rules.action`:
  - ignore        : log, no ulteriore processing
  - flag_only     : log + flag (no invio, no ticket)
  - quarantine    : copia MIME in quarantena per review umano
  - auto_reply    : invia auto-reply al mittente
  - create_ticket : enqueue su dispatch_queue (POST a manager API)
  - forward       : enqueue su outbound_queue (smarthost = forward_target)
  - redirect      : enqueue su outbound_queue con RCPT riscritto

`forward`/`redirect` non chiamano direttamente il forwarder: vengono drainati dallo scheduler
con retry. `create_ticket` non chiama direttamente il manager: viene drainato dallo scheduler.
Questo garantisce risposta SMTP <1s e resilienza a fallimenti di rete.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from relay.auto_reply import build_auto_reply, build_auto_reply_db, send_auto_reply
from relay.config import RelayConfig
from relay.parser import ParsedMessage
from relay.storage import Storage

logger = logging.getLogger(__name__)


@dataclass
class ActionResult:
    action: str
    ok: bool
    detail: str
    extra: dict[str, Any] | None = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _enqueue_outbound(
    storage: Storage,
    *,
    event_uuid: str,
    action: str,
    mime_blob: bytes,
    mail_from: str,
    rcpt_to: list[str],
    smarthost: str,
    smarthost_port: int,
    smarthost_tls: str,
) -> int:
    with storage.transaction() as conn:
        cur = conn.execute(
            """INSERT INTO outbound_queue
                   (event_uuid, action, mime_blob, mail_from, rcpt_to_json,
                    smarthost, smarthost_port, smarthost_tls, state, attempts,
                    next_attempt_at, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?)""",
            (
                event_uuid,
                action,
                mime_blob,
                mail_from,
                json.dumps(rcpt_to, ensure_ascii=False),
                smarthost,
                smarthost_port,
                smarthost_tls,
                _now_iso(),
                _now_iso(),
            ),
        )
        return int(cur.lastrowid or 0)


def _enqueue_dispatch(
    storage: Storage,
    *,
    event_uuid: str,
    payload: dict[str, Any],
) -> int:
    with storage.transaction() as conn:
        cur = conn.execute(
            """INSERT INTO dispatch_queue
                   (event_uuid, payload_json, state, attempts, next_attempt_at, created_at)
               VALUES (?, ?, 'pending', 0, ?, ?)""",
            (event_uuid, json.dumps(payload, ensure_ascii=False), _now_iso(), _now_iso()),
        )
        return int(cur.lastrowid or 0)


def do_ai_classify(
    *,
    event_uuid: str,
    parsed: ParsedMessage,
    cfg: RelayConfig,
    storage: Storage,
    backend: Any | None,
    action_map: dict[str, Any],
    ctx: Any,
) -> ActionResult:
    """Action `ai_classify`: chiama l'admin standalone per classificare la mail
    con un provider IA (Claude API o DGX Spark locale).

    Flow:
    1. POST sync (timeout 5s) a `/api/v1/relay/ai/classify` con payload
       redatto dell'evento. L'admin esegue la pipeline ai_assistant
       (PII redactor → router → provider → log decisione).
    2. Se la risposta contiene una decisione valida e ``ai_shadow_mode=false``
       lato admin, applica ``suggested_action`` (forward / create_ticket /
       flag_only). In shadow mode (default attuale) la decisione è solo
       loggata in ``ai_decisions`` e qui torniamo flag_only senza azioni.
    3. Su timeout/errore/budget esaurito → **fail-safe**: redirect a
       ``ai_fallback_forward_to`` (default ``ai-fallback@domarc.it``) +
       create_ticket urgenza ALTA con flag ``ai_unavailable=true``.

    L'admin tiene già traccia di tutto in ``ai_decisions``. Qui registriamo
    solo l'esito a livello pipeline.
    """
    import httpx
    from relay.manager_client import ManagerError

    # Costruisce il payload da inviare all'admin (event redacted lato admin)
    customer_ctx = {}
    if ctx is not None:
        customer_ctx = {
            "codcli": getattr(ctx, "codcli", None),
            "contract_active": getattr(ctx, "contract_active", False),
            "in_service": getattr(ctx, "in_service", None),
            "sector": getattr(ctx, "sector", None),
        }
    payload = {
        "event": {
            "from_address": parsed.from_address,
            "to_address": parsed.primary_to,
            "to_domain": parsed.primary_to_domain,
            "subject": parsed.subject,
            "body_text": (parsed.body_text or "")[:8000],  # cap per evitare timeout
        },
        "event_uuid": event_uuid,
        "customer_context": customer_ctx,
        "tenant_id": int(action_map.get("tenant_id") or 1),
    }
    # M042: override modello AI per questa singola regola (vince sul binding default).
    # Il rule engine inserisce `ai_model_id` nell'action_map quando rule.ai_model_id
    # e' valorizzato. L'admin lo applica al provider chiamato.
    if action_map.get("ai_model_id"):
        payload["ai_model_id_override"] = str(action_map["ai_model_id"]).strip()

    base_url = cfg.manager.base_url.rstrip("/")
    api_key = cfg.manager.api_key
    timeout_sec = float(action_map.get("timeout_ms", 5000) or 5000) / 1000.0

    decision: dict[str, Any] = {}
    error_msg: str | None = None
    try:
        with httpx.Client(timeout=timeout_sec, verify=cfg.manager.verify_tls) as cli:
            resp = cli.post(
                f"{base_url}/api/v1/relay/ai/classify",
                json=payload,
                headers={"X-API-Key": api_key},
            )
        if resp.status_code >= 400:
            error_msg = f"HTTP {resp.status_code}: {resp.text[:200]}"
        else:
            decision = resp.json() or {}
    except httpx.TimeoutException:
        error_msg = "timeout"
    except Exception as exc:  # noqa: BLE001
        error_msg = f"{type(exc).__name__}: {exc}"

    if error_msg:
        logger.warning("ai_classify fallita (%s): fail-safe forward attivato", error_msg)
        return _ai_failsafe(
            event_uuid=event_uuid, parsed=parsed, storage=storage, cfg=cfg,
            reason=error_msg,
        )

    # Master switch / budget exhausted / no_binding sull'admin → tornano `skipped`
    if decision.get("skipped"):
        logger.info("ai_classify skipped (%s): nessuna azione applicata",
                    decision.get("error") or "—")
        extra = {
            "ai_skipped": True,
            "ai_reason": decision.get("error"),
        }
        return ActionResult(action="ai_classify", ok=True,
                             detail=f"skipped: {decision.get('error') or 'unknown'}",
                             extra=extra)

    # Errore di provider con fallback fallito → fail-safe
    if decision.get("error") and not decision.get("decision_id"):
        return _ai_failsafe(
            event_uuid=event_uuid, parsed=parsed, storage=storage, cfg=cfg,
            reason=f"provider error: {decision['error']}",
        )

    # Decisione valida — log nel risultato, ma in shadow_mode NON applichiamo
    extra = {
        "ai_decision_id": decision.get("decision_id"),
        "ai_classification": decision.get("classification") or decision.get("intent"),
        "ai_urgenza": decision.get("urgenza"),
        "ai_summary": decision.get("summary"),
        "ai_suggested_action": decision.get("suggested_action"),
        "ai_confidence": decision.get("confidence"),
        "ai_shadow_mode": decision.get("shadow_mode", True),
        "ai_cost_usd": decision.get("cost_usd"),
        "ai_latency_ms": decision.get("latency_ms"),
        "ai_pii_redactions": decision.get("pii_redactions"),
    }

    if decision.get("shadow_mode", True):
        # Shadow mode: la decisione è solo loggata, non applicata. La mail
        # andrà al default delivery (handled by pipeline).
        return ActionResult(
            action="ai_classify_shadow", ok=True,
            detail=f"shadow: intent={extra['ai_classification']} urgenza={extra['ai_urgenza']} "
                   f"suggested={extra['ai_suggested_action']}",
            extra=extra,
        )

    # F3+ live mode: applichiamo suggested_action come effettivo (placeholder)
    suggested = (decision.get("suggested_action") or "").strip()
    if suggested == "create_ticket":
        sub_action_map = {
            "settore": (action_map.get("settore") or "assistenza"),
            "urgenza": decision.get("urgenza") or "NORMALE",
            "summary_ai": decision.get("summary"),
        }
        res = do_create_ticket(
            event_uuid=event_uuid, parsed=parsed, storage=storage,
            action_map=sub_action_map, codcli=customer_ctx.get("codcli"),
        )
        res.extra = {**(res.extra or {}), **extra, "ai_applied": True}
        return res
    if suggested == "auto_reply":
        # Default a flag_only se template non specificato — non rischiamo loop
        return ActionResult(action="flag_only", ok=True, detail="ai_suggested_auto_reply (no template)",
                             extra={**extra, "ai_applied": False, "ai_skip_reason": "no_template"})
    if suggested == "ignore":
        return ActionResult(action="ignore", ok=True, detail="ai_ignore",
                             extra={**extra, "ai_applied": True})
    # Default: flag_only
    return ActionResult(action="flag_only", ok=True,
                         detail=f"ai_classified: {extra['ai_classification']}",
                         extra={**extra, "ai_applied": True})


def do_ai_taxonomy(
    *,
    event_uuid: str,
    parsed: ParsedMessage,
    cfg: RelayConfig,
    storage: Storage,
    backend: Any | None,
    action_map: dict[str, Any],
    ctx: Any,
) -> ActionResult:
    """Action `ai_taxonomy`: classifica la mail in una categoria macro per KPI.

    Mai applicata: registra la decisione in ai_decisions con job_code=
    'email_taxonomy' e ritorna sempre flag_only. Il pipeline applica
    keep_original_delivery=True quindi la mail arriva al destinatario.
    """
    import httpx
    customer_ctx = {}
    if ctx is not None:
        customer_ctx = {
            "codcli": getattr(ctx, "codcli", None),
            "contract_active": getattr(ctx, "contract_active", False),
            "in_service": getattr(ctx, "in_service", None),
            "sector": getattr(ctx, "sector", None),
        }
    payload = {
        "event": {
            "from_address": parsed.from_address,
            "to_address": parsed.primary_to,
            "to_domain": parsed.primary_to_domain,
            "subject": parsed.subject,
            "body_text": (parsed.body_text or "")[:2000],
        },
        "event_uuid": event_uuid,
        "customer_context": customer_ctx,
        "tenant_id": int(action_map.get("tenant_id") or 1),
    }
    if action_map.get("ai_model_id"):
        payload["ai_model_id_override"] = str(action_map["ai_model_id"]).strip()

    base_url = cfg.manager.base_url.rstrip("/")
    api_key = cfg.manager.api_key
    timeout_sec = float(action_map.get("timeout_ms", 5000) or 5000) / 1000.0

    decision: dict[str, Any] = {}
    error_msg: str | None = None
    try:
        with httpx.Client(timeout=timeout_sec, verify=cfg.manager.verify_tls) as cli:
            resp = cli.post(
                f"{base_url}/api/v1/relay/ai/taxonomy",
                json=payload,
                headers={"X-API-Key": api_key},
            )
        if resp.status_code >= 400:
            error_msg = f"HTTP {resp.status_code}: {resp.text[:200]}"
        else:
            decision = resp.json() or {}
            if decision.get("error"):
                error_msg = decision["error"]
    except httpx.HTTPError as exc:
        error_msg = f"network: {exc}"

    extra = {
        "ai_taxonomy_decision_id": decision.get("decision_id"),
        "ai_taxonomy_category": decision.get("category"),
        "ai_taxonomy_subcategory": decision.get("subcategory"),
        "ai_taxonomy_confidence": decision.get("confidence"),
        "ai_taxonomy_model": decision.get("model"),
        "ai_taxonomy_cost_usd": decision.get("cost_usd"),
        "ai_taxonomy_error": error_msg,
    }
    if error_msg:
        logger.warning("ai_taxonomy fallita: %s — taxonomy non assegnata", error_msg)
        return ActionResult(
            action="ai_taxonomy_error", ok=True,
            detail=f"taxonomy classification failed: {error_msg}",
            extra=extra,
        )
    logger.info("ai_taxonomy: category=%s sub=%s confidence=%.2f",
                 decision.get("category"), decision.get("subcategory") or "—",
                 float(decision.get("confidence") or 0))
    return ActionResult(
        action="ai_taxonomy", ok=True,
        detail=f"category={decision.get('category')} sub={decision.get('subcategory') or '-'}",
        extra=extra,
    )


def _ai_failsafe(
    *,
    event_uuid: str,
    parsed: ParsedMessage,
    storage: Storage,
    cfg: RelayConfig,
    reason: str,
) -> ActionResult:
    """Fail-safe quando l'IA non risponde: forward verso indirizzo di sicurezza
    + ticket ALTA con flag ai_unavailable=true."""
    fallback_to = "ai-fallback@domarc.it"
    try:
        # Cerca il setting in cache locale
        with storage._connect() as conn:  # type: ignore[attr-defined]
            row = conn.execute(
                "SELECT value FROM settings_cache WHERE key = 'ai_fallback_forward_to'"
            ).fetchone()
            if row and row[0]:
                fallback_to = str(row[0])
    except Exception:  # noqa: BLE001
        pass

    # Ticket urgenza ALTA con flag ai_unavailable
    res = do_create_ticket(
        event_uuid=event_uuid, parsed=parsed, storage=storage,
        action_map={
            "settore": "assistenza",
            "urgenza": "ALTA",
            "ai_unavailable": True,
            "ai_unavailable_reason": reason[:200],
            "fallback_forward_to": fallback_to,
        },
        codcli=None,
    )
    res.action = "ai_classify_failsafe"
    res.extra = {
        **(res.extra or {}),
        "ai_unavailable": True,
        "ai_reason": reason,
        "ai_fallback_forward_to": fallback_to,
    }
    return res


def do_ignore(*, event_uuid: str, parsed: ParsedMessage) -> ActionResult:
    return ActionResult(action="ignore", ok=True, detail="ignored")


def do_flag_only(*, event_uuid: str, parsed: ParsedMessage) -> ActionResult:
    return ActionResult(action="flag_only", ok=True, detail="flagged")


def do_quarantine(
    *,
    event_uuid: str,
    parsed: ParsedMessage,
    storage: Storage,
    reason: str = "rule_quarantine",
) -> ActionResult:
    qid = storage.add_quarantine(
        event_uuid=event_uuid,
        mime_blob=parsed.raw,
        reason=reason,
        from_address=parsed.from_address,
        to_address=parsed.primary_to,
    )
    return ActionResult(action="quarantine", ok=True, detail=f"queued id={qid}", extra={"quarantine_id": qid})


def do_auto_reply(
    *,
    event_uuid: str,
    parsed: ParsedMessage,
    cfg: RelayConfig,
    storage: "Storage",
    backend: Any | None = None,
    action_map: dict[str, Any],
    customer_context: dict[str, Any] | None = None,
    rule: dict[str, Any] | None = None,
) -> ActionResult:
    if not parsed.from_address:
        return ActionResult(action="auto_reply", ok=False, detail="mittente sconosciuto, no auto-reply")
    if parsed.is_auto_or_bulk:
        return ActionResult(action="auto_reply", ok=False, detail="messaggio auto/bulk, skip per evitare loop")
    # Anti-loop: skip se il mittente e' un local-part tipicamente non gestito
    # (bounce/postmaster/noreply). Rispondere a queste mailbox causa loop o
    # bounce ricorsivi. Whitelist hardcoded perche' standard universali RFC.
    NOREPLY_LOCAL_PARTS = {
        "noreply", "no-reply", "donotreply", "do-not-reply",
        "mailer-daemon", "postmaster", "bounce", "bounces",
        "abuse", "spam", "phish-report",
    }
    try:
        local = parsed.from_address.split("@", 1)[0].lower()
        if local in NOREPLY_LOCAL_PARTS:
            return ActionResult(action="auto_reply", ok=False,
                                 detail=f"skip auto-reply a mittente di sistema ({local}@...)")
    except Exception:  # noqa: BLE001
        pass

    # Smarthost scelto in base al dominio del destinatario dell'auto-reply (mittente originale)
    rcpt_domain = parsed.from_address.split("@", 1)[-1].lower() if "@" in parsed.from_address else None
    sh = storage.pick_smarthost_for_domain(
        rcpt_domain,
        cfg.outbound.default_smarthost,
        cfg.outbound.default_smarthost_port,
        cfg.outbound.default_tls,
    )

    ctx: dict[str, Any] = {
        "subject": parsed.subject,
        "from_address": parsed.from_address,
        "to_address": parsed.primary_to,
        "received_at": _now_iso(),
        "message_id": parsed.message_id,
        "assistance_email": action_map.get("assistance_email", "assistenza@domarc.it"),
        "next_in_service_at": (customer_context or {}).get("next_in_service_at"),
        "codice_cliente": (customer_context or {}).get("codcli"),
        "auth_code": None,
        "auth_code_valid_until": None,
        "auth_code_ttl_hours": None,
        # H24 Fase D — variabili template (risolute sotto se applicabili)
        "h24_inbound_alias": None,
        "urgent_fee": None,
        "ticket_id": None,
    }

    # H24: risoluzione h24_inbound_alias + urgent_fee multi-brand.
    #
    # Cascade alias:
    #   1. target_row (lookup cascade email > dominio — match piu specifico)
    #   2. action_map.h24_inbound_alias (override per regola)
    #   3. setting h24.default_inbound_alias (fallback globale)
    #
    # Cascade fee:
    #   1. target_row.urgent_fee_eur (priorita: match per email/dominio
    #      specifico vince sul fee della rule, perche e un override
    #      operativo per quel mittente preciso)
    #   2. action_map.urgent_fee (rule-level, applicato se target_row
    #      non ha override esplicito)
    #   3. setting h24.default_urgent_fee_eur (default globale)
    target_row = None
    if parsed.from_address and "@" in parsed.from_address:
        try:
            if hasattr(storage, "find_h24_target_by_email"):
                target_row = storage.find_h24_target_by_email(parsed.from_address)
            elif hasattr(storage, "find_h24_target_by_domain"):
                sender_domain = parsed.from_address.rsplit("@", 1)[1].lower().strip()
                target_row = storage.find_h24_target_by_domain(sender_domain)
        except Exception:  # noqa: BLE001
            target_row = None

    # Alias: target > action_map > setting
    h24_alias = (target_row or {}).get("h24_alias") or action_map.get("h24_inbound_alias")
    if not h24_alias:
        try:
            h24_alias = storage.get_setting("h24.default_inbound_alias") or None
        except Exception:  # noqa: BLE001
            pass
    ctx["h24_inbound_alias"] = h24_alias

    # Fee: target > action_map > setting
    fee = None
    if target_row:
        target_fee = target_row.get("urgent_fee_eur")
        if target_fee not in (None, ""):
            try:
                fee = int(target_fee)
            except (TypeError, ValueError):
                fee = None
    if fee is None:
        am_fee = action_map.get("urgent_fee")
        if am_fee not in (None, ""):
            try:
                fee = int(am_fee)
            except (TypeError, ValueError):
                fee = None
    if fee is None:
        try:
            fee_raw = storage.get_setting("h24.default_urgent_fee_eur")
            fee = int(fee_raw) if fee_raw else 250
        except (TypeError, ValueError):
            fee = 250
    ctx["urgent_fee"] = fee

    # H24: extra context iniettato dal chiamante (es. _send_h24_template_reply
    # per ack/reject) — sovrascrive i default di sopra (es. ticket_id post-ticket).
    h24_extra = action_map.get("_h24_extra_ctx")
    if isinstance(h24_extra, dict):
        ctx.update(h24_extra)

    # Generazione codice di autorizzazione (se richiesto dalla regola e backend disponibile)
    auth_code_extra: dict[str, Any] = {}
    if action_map.get("generate_auth_code") and backend is not None:
        try:
            ttl = int(action_map.get("auth_code_ttl_hours") or 48)
        except (TypeError, ValueError):
            ttl = 48
        rule_id = (rule or {}).get("id")
        rule_name = (rule or {}).get("name", "")
        try:
            ac = backend.issue_auth_code(
                codcli=(customer_context or {}).get("codcli"),
                rule_id=rule_id,
                ttl_hours=ttl,
                note=f"auto-reply regola #{rule_id} ({rule_name})",
                # Migration 026: traccia a chi viene spedito il codice
                sent_to_email=(parsed.from_address or "").strip().lower() or None,
            )
            if ac.ok and ac.code:
                ctx["auth_code"] = ac.code
                ctx["auth_code_valid_until"] = ac.valid_until
                ctx["auth_code_ttl_hours"] = ttl
                auth_code_extra = {"auth_code": ac.code, "auth_code_id": ac.code_id, "auth_code_valid_until": ac.valid_until}
                logger.info("Auth code generato: %s (valido fino %s, code_id=%s)", ac.code, ac.valid_until, ac.code_id)
            else:
                logger.warning("issue_auth_code fallito: %s", ac.error)
        except Exception as exc:  # noqa: BLE001
            logger.warning("issue_auth_code eccezione: %s", exc)
    elif action_map.get("generate_auth_code") and backend is None:
        logger.warning("generate_auth_code richiesto ma backend non disponibile (auto-reply senza codice)")

    # Lookup template: prima nel DB cache (sync dal manager), poi fallback file Jinja2 locali.
    # Priorità: action_map.template_id > action_map.auto_reply_template (può essere id numerico
    # o nome del template DB) > nome file fallback (out_of_hours, ecc).
    template_id_raw = action_map.get("template_id")
    template_name_raw = action_map.get("auto_reply_template")
    sender_override = action_map.get("auto_reply_from")

    tpl_row = None
    if template_id_raw is not None:
        tpl_row = storage.find_template_by_id(template_id_raw)
    if tpl_row is None and template_name_raw:
        tpl_row = storage.find_template_by_id(template_name_raw)
        if tpl_row is None:
            tpl_row = storage.find_template_by_name(template_name_raw)

    # Override no-contract: se il mittente è identificato come cliente
    # ma `contract_active=False`, sostituisci il template con
    # `always_billable_no_contract` (Migration 026 / Seed dedicato).
    cust_known = bool((customer_context or {}).get("codcli"))
    cust_contract = (customer_context or {}).get("contract_active")
    if cust_known and cust_contract is False:
        no_contract_tpl = storage.find_template_by_name(
            "always_billable_no_contract")
        if no_contract_tpl is not None:
            logger.info(
                "Cliente %s senza contratto attivo: override template %s",
                (customer_context or {}).get("codcli"),
                no_contract_tpl.get("name"))
            tpl_row = no_contract_tpl

    # Opzioni reply_* da action_map (configurabili dall'admin)
    reply_subject_prefix = action_map.get("reply_subject_prefix") or None
    reply_to_hdr = action_map.get("reply_to") or None
    reply_quote = bool(action_map.get("reply_quote_original"))
    reply_attach = bool(action_map.get("reply_attach_original"))

    template_label: str
    try:
        if tpl_row is not None:
            # Template dal DB del manager
            template_label = f"db:{tpl_row['id']}:{tpl_row['name']}"
            msg = build_auto_reply_db(
                tpl_row=tpl_row,
                sender_email_override=sender_override,
                recipient=parsed.from_address,
                in_reply_to=parsed.message_id,
                references=parsed.references,
                subject_original=parsed.subject,
                context=ctx,
                subject_prefix=reply_subject_prefix,
                reply_to=reply_to_hdr,
                quote_original=reply_quote,
                attach_original=reply_attach,
                original_mime=parsed.raw if reply_attach else None,
                original_body_text=parsed.body_text if reply_quote else None,
                original_body_html=parsed.body_html if reply_quote else None,
            )
            # Mittente effettivo per MAIL FROM SMTP (envelope)
            sender_email = (sender_override or tpl_row["reply_from_email"] or f"noreply@{cfg.listener.hostname}").strip()
        else:
            # Fallback file Jinja2 locale (out_of_hours, ecc.)
            template_label = f"file:{template_name_raw or 'out_of_hours'}"
            sender_email = (sender_override or f"noreply@{cfg.listener.hostname}").strip()
            msg = build_auto_reply(
                template_name=template_name_raw or "out_of_hours",
                sender=sender_email,
                recipient=parsed.from_address,
                in_reply_to=parsed.message_id,
                references=parsed.references,
                subject_original=parsed.subject,
                context=ctx,
            )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Render auto-reply fallito: %s", exc)
        return ActionResult(action="auto_reply", ok=False, detail=f"render error: {exc}")

    # reply_mode: chi riceve l'auto-reply
    #   to_sender_only (default) → solo il mittente originale
    #   reply_all                → mittente + tutti i destinatari originali (escluso il mittente stesso)
    #   to_alias                 → mittente + From cambiato al destinatario originale
    reply_mode = (action_map.get("reply_mode") or "to_sender_only").strip().lower()
    rcpts: list[str] = [parsed.from_address]
    if reply_mode == "reply_all":
        from_low = parsed.from_address.lower()
        for to in (parsed.to_addresses or []):
            if to and to.lower() not in (from_low, *(r.lower() for r in rcpts)):
                rcpts.append(to)
    elif reply_mode == "to_alias":
        # Risponde dall'alias intercept: From = destinatario originale
        if parsed.primary_to:
            del msg["From"]
            msg["From"] = parsed.primary_to
            sender_email = parsed.primary_to

    # Enqueue invio via outbound_queue (retry esponenziale + audit + non blocca listener)
    qid = _enqueue_outbound(
        storage,
        event_uuid=event_uuid,
        action="auto_reply",
        mime_blob=bytes(msg),
        mail_from=sender_email,
        rcpt_to=rcpts,
        smarthost=sh["smarthost"],
        smarthost_port=sh["smarthost_port"],
        smarthost_tls=sh["smarthost_tls"],
    )
    extra_dict = {"queue_id": qid, "auto_reply_sender": sender_email, "auto_reply_template": template_label}
    extra_dict.update(auth_code_extra)
    return ActionResult(
        action="auto_reply", ok=True,
        detail=f"queued id={qid} to={parsed.from_address} via {sh['smarthost']} ({sh['source']}) template={template_label}" +
               (f" auth_code={ctx['auth_code']}" if ctx.get("auth_code") else ""),
        extra=extra_dict,
    )


def do_forward(
    *,
    event_uuid: str,
    parsed: ParsedMessage,
    storage: Storage,
    cfg: RelayConfig,
    action_map: dict[str, Any],
    route_row: sqlite3.Row | None,
    rule: dict[str, Any] | None = None,
) -> ActionResult:
    # Forward ha un target esplicito (è il senso dell'azione): non si applica
    # qui la logica "scegli smarthost dal dominio destinatario", perché chi crea
    # la regola/route ha specificato un host SMTP preciso.
    target = action_map.get("forward_target")
    target_port = int(action_map.get("forward_port", cfg.outbound.default_smarthost_port))
    target_tls = action_map.get("forward_tls", cfg.outbound.default_tls)
    if not target and route_row is not None:
        target = route_row["forward_target"]
        if route_row["forward_port"]:
            target_port = int(route_row["forward_port"])
        if route_row["forward_tls"]:
            target_tls = route_row["forward_tls"]
    # Migration 027: se la regola usa forward_to_emails/forward_to_group_id ma
    # non specifica forward_target, usa il default smarthost del cfg.
    if not target and rule and (rule.get("forward_to_emails") or rule.get("forward_to_group_id")):
        target = cfg.outbound.default_smarthost
        target_port = cfg.outbound.default_smarthost_port
        target_tls = cfg.outbound.default_tls
    if not target:
        return ActionResult(action="forward", ok=False, detail="nessun forward_target definito")

    # Migration 027: rcpt override dalla regola
    # (forward_to_emails ';' o forward_to_group_id) — sovrascrive gli rcpt originali
    rcpt: list[str] = []
    if rule:
        emails_raw = (rule.get("forward_to_emails") or "").strip()
        if emails_raw:
            import re as _re
            rcpt = [e.strip().lower() for e in _re.split(r"[\s,;]+", emails_raw)
                    if e.strip() and "@" in e]
        group_id = rule.get("forward_to_group_id")
        if group_id:
            try:
                emails_from_group = storage.get_recipient_groups_emails(int(group_id))
                rcpt.extend(emails_from_group)
            except Exception as exc:  # noqa: BLE001
                logger.warning("forward_to_group_id=%s lookup fallito: %s", group_id, exc)
        rcpt = list(dict.fromkeys(rcpt))  # dedup preservando ordine
    if not rcpt:
        rcpt = list(parsed.to_addresses) or ([parsed.primary_to] if parsed.primary_to else [])
    if not rcpt:
        return ActionResult(action="forward", ok=False, detail="nessun rcpt nel MIME")

    qid = _enqueue_outbound(
        storage,
        event_uuid=event_uuid,
        action="forward",
        mime_blob=parsed.raw,
        mail_from=parsed.from_address or "",
        rcpt_to=rcpt,
        smarthost=target,
        smarthost_port=target_port,
        smarthost_tls=target_tls,
    )
    return ActionResult(action="forward", ok=True, detail=f"queued id={qid}", extra={"queue_id": qid})


def do_redirect(
    *,
    event_uuid: str,
    parsed: ParsedMessage,
    storage: Storage,
    cfg: RelayConfig,
    action_map: dict[str, Any],
    route_row: sqlite3.Row | None,
) -> ActionResult:
    redirect_to = action_map.get("redirect_to")
    if not redirect_to and route_row is not None:
        redirect_to = route_row["redirect_target"]
    if not redirect_to:
        return ActionResult(action="redirect", ok=False, detail="nessun redirect_target definito")

    # Smarthost scelto in base al dominio del nuovo destinatario (smart routing).
    # action_map.smarthost permette comunque override esplicito.
    if action_map.get("smarthost"):
        smarthost = action_map["smarthost"]
        smarthost_port = int(action_map.get("smarthost_port", cfg.outbound.default_smarthost_port))
        smarthost_tls = action_map.get("smarthost_tls", cfg.outbound.default_tls)
    else:
        rcpt_domain = redirect_to.split("@", 1)[-1].lower() if "@" in redirect_to else None
        sh = storage.pick_smarthost_for_domain(
            rcpt_domain,
            cfg.outbound.default_smarthost,
            cfg.outbound.default_smarthost_port,
            cfg.outbound.default_tls,
        )
        smarthost = sh["smarthost"]
        smarthost_port = sh["smarthost_port"]
        smarthost_tls = sh["smarthost_tls"]

    qid = _enqueue_outbound(
        storage,
        event_uuid=event_uuid,
        action="redirect",
        mime_blob=parsed.raw,
        mail_from=parsed.from_address or "",
        rcpt_to=[redirect_to],
        smarthost=smarthost,
        smarthost_port=smarthost_port,
        smarthost_tls=smarthost_tls,
    )
    return ActionResult(action="redirect", ok=True, detail=f"queued id={qid} to={redirect_to} via {smarthost}", extra={"queue_id": qid})


def do_create_authorized_ticket(
    *,
    event_uuid: str,
    parsed: ParsedMessage,
    cfg: "RelayConfig",
    storage: Storage,
    backend: Any | None,
    action_map: dict[str, Any],
    codcli: str | None,
) -> ActionResult:
    """Action H24: valida codice autorizzazione dal subject e apre ticket
    URGENZA=PAGAMENTO solo se valido. Cascade oneshot → permanente via API.

    action_map opzionale:
        ack_template_id: int      → invia auto-reply ack al cliente post-ticket
        reject_template_id: int   → invia auto-reply reject se codice non valido
        urgent_fee: int           → override settings (per audit ticket note)
        settore, urgenza          → override default (default: S, URGENTE)
    """
    # Bounce / auto-reply / list mail → skip silenzioso (no loop)
    if getattr(parsed, "is_auto_or_bulk", False):
        return ActionResult(
            action="create_authorized_ticket", ok=True,
            detail="skip: auto/bulk message",
            extra={"h24_skip_reason": "auto_or_bulk"},
        )

    if backend is None:
        logger.error("create_authorized_ticket: backend non disponibile, skip")
        return ActionResult(
            action="create_authorized_ticket", ok=False,
            detail="backend non disponibile",
        )

    inbound_alias = parsed.primary_to or None
    # Cascade validation server-side (oneshot atomico → permanente)
    val = backend.validate_auth_code(
        subject=parsed.subject,
        event_uuid=event_uuid,
        from_address=parsed.from_address,
        inbound_alias=inbound_alias,
        tenant_id=int(action_map.get("tenant_id") or 1),
    )

    if not val.valid:
        # FIX B (2026-05-05): distingue "codice noto ma rifiutato" da
        # "estrazione da regex larga = falso positivo".
        #
        # Caso A — codice fornito esplicitamente (no extraction): è un
        #          tentativo legittimo, manda reject template.
        # Caso B — estratto dal subject ma not_found in DB: il regex della
        #          regola ha fatto un falso positivo (es. "RT-FRANCESCHETTA-4833"
        #          su subject CloudTIK). NON inviare reject template (sarebbe
        #          spam al mittente che non ha mai chiesto autorizzazione)
        #          e segnala `h24_false_positive=True` così il pipeline
        #          ri-valuta le regole successive escludendo questa.
        is_false_positive = (
            val.reason == "not_found"
            and getattr(val, "extracted_from_subject", False)
        )
        logger.info(
            "H24 %s event=%s reason=%s code=%s",
            "false_positive" if is_false_positive else "reject",
            event_uuid, val.reason or val.error, val.code,
        )
        # Reject template SOLO per il caso A (codice noto ma scaduto/usato/disabled)
        reject_tid = action_map.get("reject_template_id")
        if reject_tid and not is_false_positive:
            try:
                _send_h24_template_reply(
                    template_id=int(reject_tid),
                    parsed=parsed, cfg=cfg, storage=storage,
                    backend=backend, action_map=action_map,
                    extra_ctx={
                        "h24_reject_reason": val.reason or "invalid",
                        "ticket_id": None,
                    },
                    label="h24_reject",
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("H24 reject auto-reply fallito: %s", exc)
        return ActionResult(
            action="create_authorized_ticket",
            ok=True,
            detail=(f"false_positive (regex extraction not in DB): {val.code}"
                    if is_false_positive
                    else f"reject: {val.reason or val.error or 'invalid'}"),
            extra={
                "h24_kind": None,
                "h24_reason": val.reason,
                "h24_code": val.code,
                "h24_extracted": val.extracted_from_subject,
                "h24_false_positive": is_false_positive,
            },
        )

    # Codice valido → costruisci payload ticket
    code_info = val.code_info or {}
    # === Identificazione cliente (cascade) ===
    # 1. Per codici permanenti: codice_cliente è SEMPRE valorizzato
    #    (il codice è anagraficamente legato al cliente).
    # 2. Per codici monouso: code_info.codice_cliente può essere valorizzato
    #    se al momento dell'emissione il mittente era stato identificato.
    # 3. Fallback: codcli del mittente del rientro (ctx.codcli)
    code_codcli = code_info.get("codice_cliente")
    final_codcli = code_codcli or codcli  # priorità al codice anagrafico
    # Origine identificazione (per audit chiaro nelle note ticket)
    if val.kind == "permanent":
        codcli_source = "codice permanente (anagrafica forzata)"
    elif code_codcli:
        codcli_source = "codice monouso (cliente identificato all'emissione)"
    elif codcli:
        codcli_source = "mittente del rientro (lookup customer source)"
    else:
        codcli_source = "non identificato (mittente sconosciuto in archivio)"

    # === Billable check ===
    # I codici PERMANENTI sono parte del contratto H24 attivo del cliente:
    # NESSUN addebito aggiuntivo (intervento incluso). I codici MONOUSO
    # sono attivazioni straordinarie a pagamento.
    is_billable = (val.kind == "oneshot")

    # Recupera urgent_fee (per oneshot) — per permanent resta 0 nel ticket
    urgent_fee = action_map.get("urgent_fee")
    if not urgent_fee:
        try:
            urgent_fee = int(storage.get_setting("h24.default_urgent_fee_eur") or "250")
        except (TypeError, ValueError):
            urgent_fee = 250
    effective_fee = urgent_fee if is_billable else 0

    settore = (action_map.get("settore") or "S").strip() or "S"
    urgenza = (action_map.get("urgenza") or "URGENTE").strip() or "URGENTE"

    if is_billable:
        title = "=== APERTURA TICKET URGENTE H24 (a pagamento) ==="
        billing_line = f"Importo addebitato: {urgent_fee} EUR + IVA"
    else:
        title = "=== APERTURA TICKET URGENTE H24 (incluso nel contratto) ==="
        billing_line = (
            "Addebito: NESSUNO — intervento incluso nel contratto H24 "
            "attivo del cliente (codice anagrafico permanente)."
        )

    note_lines = [
        title,
        f"",
        f"Aperto per conto di: {parsed.from_address}",
        f"Cliente: {final_codcli or 'NON IDENTIFICATO'} ({codcli_source})",
        f"Codice autorizzazione: {val.code}",
        f"Tipo codice: {val.kind} ({'riusabile, contrattuale' if val.kind == 'permanent' else 'monouso, straordinario'})",
        billing_line,
        f"Mailbox di rientro: {inbound_alias or '(non determinato)'}",
    ]
    if val.kind == "oneshot" and code_info.get("event_uuid"):
        note_lines.append(f"Evento originario (auto-reply che ha emesso il codice): {code_info['event_uuid']}")
    if val.kind == "permanent" and code_info.get("label"):
        note_lines.append(f"Etichetta codice permanente: {code_info['label']}")
    if val.kind == "permanent" and val.usage_id:
        note_lines.append(f"Usage record (audit utilizzi H24): id {val.usage_id}")

    body_prefix = "\n".join(note_lines) + "\n\n=== Body originale del rientro ===\n\n"
    original_body = (parsed.body_text or parsed.body_html or "")[:7000]

    subject_prefix = "[H24 PAGAMENTO]" if is_billable else "[H24 contrattuale]"
    payload: dict[str, Any] = {
        "channel": "email_h24_authorized",
        "external_id": f"h24-{val.kind}-{val.code}-{event_uuid[:12]}",
        "subject": f"{subject_prefix} {parsed.subject or '(senza oggetto)'}",
        "body": body_prefix + original_body,
        "from_address": parsed.from_address,
        "to_address": parsed.primary_to,
        "message_id": parsed.message_id,
        "codice_cliente": final_codcli,
        "settore": settore,
        "urgenza": urgenza,
        "metadata": {
            "h24_kind": val.kind,
            "h24_billable": is_billable,
            "h24_code": val.code,
            "h24_usage_id": val.usage_id,
            "h24_urgent_fee_eur": effective_fee,  # 0 per permanent (no addebito)
            "h24_inbound_alias": inbound_alias,
            "h24_event_uuid_origin": code_info.get("event_uuid"),
            "h24_codcli_source": codcli_source,
            "h24_from_address": parsed.from_address,  # mittente reale
            "received_count": parsed.received_count,
            "attachments": [{"filename": a.filename, "content_type": a.content_type,
                              "size": a.size_bytes} for a in parsed.attachments],
        },
    }
    qid = _enqueue_dispatch(storage, event_uuid=event_uuid, payload=payload)

    # Ack opzionale al cliente (POST async, non blocca apertura ticket)
    ack_tid = action_map.get("ack_template_id")
    if ack_tid:
        try:
            _send_h24_template_reply(
                template_id=int(ack_tid),
                parsed=parsed, cfg=cfg, storage=storage,
                backend=backend, action_map=action_map,
                extra_ctx={
                    "h24_code": val.code,
                    "h24_kind": val.kind,
                    "h24_billable": is_billable,
                    "urgent_fee": effective_fee,  # 0 per permanent
                    "ticket_id": None,  # ancora pending dispatch
                },
                label="h24_ack",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("H24 ack auto-reply fallito: %s", exc)

    logger.info(
        "H24 ticket enqueued event=%s kind=%s code=%s codcli=%s qid=%s",
        event_uuid, val.kind, val.code, final_codcli, qid,
    )
    return ActionResult(
        action="create_authorized_ticket",
        ok=True,
        detail=f"valid {val.kind}: ticket queued id={qid}",
        extra={
            "h24_kind": val.kind,
            "h24_code": val.code,
            "h24_usage_id": val.usage_id,
            "h24_extracted": val.extracted_from_subject,
            "dispatch_id": qid,
        },
    )


def _send_h24_template_reply(
    *, template_id: int, parsed: ParsedMessage, cfg: "RelayConfig",
    storage: Storage, backend: Any | None, action_map: dict[str, Any],
    extra_ctx: dict[str, Any], label: str,
) -> None:
    """Helper per inviare ack/reject H24 riusando l'infrastruttura auto_reply.

    Costruisce un action_map sintetico con template_id e chiama
    do_auto_reply, passando extra_ctx via action_map._h24_extra_ctx (se
    auto_reply.build_context lo legge — vedi Fase D, oggi è solo log).
    """
    synth_action_map = dict(action_map)
    synth_action_map["template_id"] = int(template_id)
    synth_action_map["_h24_extra_ctx"] = extra_ctx  # Fase D: build_context lo userà
    # generate_auth_code MAI in ack/reject (non vogliamo emettere nuovi codici)
    synth_action_map["generate_auth_code"] = False
    # Riusa do_auto_reply (gestisce tutto: render template, send via SMTP outbound)
    res = do_auto_reply(
        event_uuid=f"h24-{label}-{parsed.message_id or '?'}"[:64],
        parsed=parsed, cfg=cfg, storage=storage, backend=backend,
        action_map=synth_action_map,
        customer_context={"in_service": False, "codcli": None},
        rule=None,
    )
    logger.info("H24 %s template_id=%s result=%s", label, template_id, res.ok)


def do_create_ticket(
    *,
    event_uuid: str,
    parsed: ParsedMessage,
    storage: Storage,
    action_map: dict[str, Any],
    codcli: str | None,
) -> ActionResult:
    payload: dict[str, Any] = {
        "channel": "email_smtp",
        "external_id": event_uuid,
        "subject": parsed.subject or "(senza oggetto)",
        "body": parsed.body_text[:8000] if parsed.body_text else (parsed.body_html[:8000] if parsed.body_html else ""),
        "from_address": parsed.from_address,
        "to_address": parsed.primary_to,
        "message_id": parsed.message_id,
        "codice_cliente": codcli,
        # Accetta entrambi i naming: 'settore'/'urgenza' (form admin attuale)
        # e 'ticket_settore'/'ticket_urgenza' (compat legacy). Strip per evitare
        # che stringhe vuote saltino il fallback.
        "settore": ((action_map.get("settore") or "").strip()
                     or (action_map.get("ticket_settore") or "").strip() or None),
        "urgenza": ((action_map.get("urgenza") or "").strip()
                     or (action_map.get("ticket_urgenza") or "").strip() or None),
        "metadata": {
            "received_count": parsed.received_count,
            "is_auto_or_bulk": parsed.is_auto_or_bulk,
            "attachments": [{"filename": a.filename, "content_type": a.content_type, "size": a.size_bytes} for a in parsed.attachments],
        },
    }
    qid = _enqueue_dispatch(storage, event_uuid=event_uuid, payload=payload)
    return ActionResult(action="create_ticket", ok=True, detail=f"queued id={qid}", extra={"dispatch_id": qid})
