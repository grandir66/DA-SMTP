"""Pipeline di elaborazione SMTP.

Orchestra: resolve cliente (cache locale) → calcolo orari servizio → rule engine →
dispatch dell'azione decisa. Sincrona, in-process del listener: per garantire 250 OK <1s
le azioni "lente" (forward/redirect/create_ticket) sono solo enqueue su SQLite, mai chiamate
di rete. Auto-reply è inline: il messaggio è breve, lo smarthost è solitamente locale.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from relay import actions, aggregations as agg_module
from relay.config import RelayConfig
from relay.parser import ParsedMessage
from relay.rules import RuleEngine, RuleOutcome
from relay.service_hours import is_in_service
from relay.storage import Storage

logger = logging.getLogger(__name__)


@dataclass
class CustomerContext:
    codcli: str | None
    contract_active: bool
    in_service: bool | None
    sector: str | None
    service_hours: dict[str, Any] | None
    tipologia_servizio: str | None = None  # M029: code profilo (STD/EXT/H24/NO/...)


@dataclass
class PipelineResult:
    event_uuid: str
    action_taken: str
    rule_id: int | None
    codcli: str | None
    detail: str
    chain: list[dict[str, Any]]
    extra: dict[str, Any]


def _resolve_customer(parsed: ParsedMessage, storage: Storage,
                       primary_to_override: str | None = None
                       ) -> tuple[CustomerContext, sqlite3.Row | None]:
    """Resolve cliente + route per la mail.

    `primary_to_override` (es. envelope.rcpt_tos[0] = primo destinatario SMTP
    su dominio gestito) ha priorita' sul MIME parsed.primary_to. Cosi' per
    mail con CC verso domarc.it + MIME To: esterno, il lookup cliente parte
    dal destinatario INTERNO (es. qualcuno@domarc.it) e non dall'esterno
    (che non e' un cliente Domarc).
    """
    eff_local: str | None = parsed.primary_to_local
    eff_domain: str | None = parsed.primary_to_domain
    if primary_to_override and "@" in primary_to_override:
        ov_local, _, ov_domain = primary_to_override.rpartition("@")
        if ov_local and ov_domain:
            eff_local = ov_local.lower()
            eff_domain = ov_domain.lower()

    route_row: sqlite3.Row | None = None
    if eff_local and eff_domain:
        route_row = storage.find_route(eff_local, eff_domain)

    cust_row: sqlite3.Row | None = None
    codcli: str | None = None
    if route_row is not None and route_row["codcli"]:
        codcli = str(route_row["codcli"])
        cust_row = storage.find_customer_by_alias(f"{eff_local}@{eff_domain}")
        if cust_row is None and codcli:
            with storage._connect() as conn:  # type: ignore[attr-defined]
                cust_row = conn.execute("SELECT * FROM customers_cache WHERE codcli = ?", (codcli,)).fetchone()
    # Lookup per dominio destinatario interno (eff_domain) se non risolto via route
    if cust_row is None and eff_domain:
        cust_row = storage.find_customer_by_domain(eff_domain)
        if cust_row is not None:
            codcli = str(cust_row["codcli"])
    # Fallback su dominio mittente (mail in entrata da cliente noto)
    if cust_row is None and parsed.from_domain:
        cust_row = storage.find_customer_by_domain(parsed.from_domain)
        if cust_row is not None:
            codcli = str(cust_row["codcli"])

    contract_active = bool(cust_row["contract_active"]) if cust_row is not None else False
    schedule: dict[str, Any] | None = None
    in_srv: bool | None = None
    if cust_row is not None and cust_row["service_hours_json"]:
        try:
            schedule = json.loads(cust_row["service_hours_json"])
        except (TypeError, ValueError):
            schedule = None
    if schedule:
        in_srv = is_in_service(schedule)

    # M029: estrai tipologia_servizio (code profilo orari) per attivazione rule_set
    tipologia: str | None = None
    if cust_row is not None:
        try:
            tipologia = cust_row["tipologia_servizio"]
        except (KeyError, IndexError):
            tipologia = None

    ctx = CustomerContext(
        codcli=codcli,
        contract_active=contract_active,
        in_service=in_srv,
        sector=None,
        service_hours=schedule,
        tipologia_servizio=tipologia,
    )
    return ctx, route_row


def _check_thread_continuation(parsed: ParsedMessage,
                                  storage: "Storage") -> dict[str, Any] | None:
    """M036: ritorna info sull'evento padre se la mail in arrivo e' una
    continuazione di thread tracciato (RFC 2822 In-Reply-To / References).

    Dict ritornato:
      {event_uuid, message_id, ticket_id, rule_id, action_taken, thread_root_uuid}
    None se non e' una continuazione (mail nuova / inizio thread).
    """
    if not hasattr(storage, "find_thread_root"):
        return None
    if not parsed.in_reply_to and not parsed.references:
        return None
    return storage.find_thread_root(parsed.in_reply_to, parsed.references)


def _check_shadow_recipient_group(parsed: ParsedMessage,
                                    storage: "Storage") -> dict[str, Any] | None:
    """M030: ritorna info sul gruppo shadow se uno dei destinatari della mail
    e' membro di un recipient_group con shadow_mode=1. None altrimenti.

    Controlla primary_to + tutti i to_addresses (la mail puo' avere piu'
    destinatari, basta che UNO sia in shadow per attivare il flag).
    """
    if not hasattr(storage, "find_shadow_group_for_email"):
        return None
    candidates: list[str] = []
    if parsed.primary_to_local and parsed.primary_to_domain:
        candidates.append(f"{parsed.primary_to_local}@{parsed.primary_to_domain}")
    for to in (parsed.to_addresses or []):
        if to and to not in candidates:
            candidates.append(to)
    for em in candidates:
        info = storage.find_shadow_group_for_email(em)
        if info:
            info = dict(info)
            info["matched_email"] = em
            return info
    return None


def _check_shadow_domain(parsed: ParsedMessage,
                          storage: "Storage") -> dict[str, Any] | None:
    """M031: ritorna info se il dominio del destinatario ha shadow_mode=1.
    None altrimenti. Controlla primary_to_domain + domini di tutti gli altri
    destinatari (basta che UNO sia in dominio shadow).
    """
    if not hasattr(storage, "find_shadow_domain"):
        return None
    domains: list[str] = []
    if parsed.primary_to_domain:
        domains.append(parsed.primary_to_domain.lower())
    for to in (parsed.to_addresses or []):
        if to and "@" in to:
            d = to.rsplit("@", 1)[-1].strip().lower()
            if d and d not in domains:
                domains.append(d)
    for d in domains:
        info = storage.find_shadow_domain(d)
        if info:
            info = dict(info)
            info["matched_domain"] = d
            return info
    return None


def _check_shadow_rule(rule: dict[str, Any] | None) -> dict[str, Any] | None:
    """M033: ritorna info se la regola vincente ha shadow_mode=1. None altrimenti.
    La regola arriva gia' dal rule engine (rules_cache include shadow_mode).
    """
    if not rule or not rule.get("shadow_mode"):
        return None
    return {
        "rule_id": int(rule.get("id", 0)),
        "rule_name": rule.get("name"),
        "shadow_note": rule.get("shadow_note"),
    }


def _check_shadow_cascata(parsed: ParsedMessage, storage: "Storage",
                           winning_rule: dict[str, Any] | None
                           ) -> tuple[str, dict[str, Any]] | None:
    """Cascata di check shadow nell'ordine: dominio -> recipient_group -> regola.
    Ritorna (origin, info) del PRIMO trigger trovato, None se nessuno scatta.

      origin = "domain:<domain>" | "recipient_group:<code>" | "rule:<id>"
    """
    info = _check_shadow_domain(parsed, storage)
    if info:
        return (f"domain:{info.get('matched_domain') or info.get('domain')}", info)
    info = _check_shadow_recipient_group(parsed, storage)
    if info:
        return (f"recipient_group:{info.get('code')}", info)
    info = _check_shadow_rule(winning_rule)
    if info:
        return (f"rule:{info.get('rule_id')}", info)
    return None


def _resolve_active_rule_sets(storage: "Storage",
                                profile_code: str | None) -> set[int] | None:
    """M029: ritorna l'insieme di rule_set_id attivi per la mail corrente.

    Sempre inclusi: tutti i set con is_always_active=1 (es. "globali").
    In piu', se profile_code valorizzato e c'e' un set con stesso profile_code
    e enabled=1, viene aggiunto.

    Ritorna None per backward-compat se rule_sets_cache non esiste o e' vuota
    (significa: pre-M029, no filter, comportamento legacy).
    """
    try:
        if not hasattr(storage, "fetch_active_rule_set_ids"):
            return None
        ids = storage.fetch_active_rule_set_ids(profile_code=profile_code)
        if not ids:
            return None
        return set(int(x) for x in ids)
    except Exception:  # noqa: BLE001
        return None


def _event_dict(parsed: ParsedMessage, ctx: CustomerContext,
                  storage: "Storage" | None = None) -> dict[str, Any]:
    customer_groups: list[str] = []
    if storage is not None and ctx.codcli:
        try:
            customer_groups = sorted(storage.get_groups_for_codcli(ctx.codcli))
        except Exception:  # noqa: BLE001
            pass

    # Gruppi virtuali (codici riservati `all_contract` / `no_contract`).
    # Sintetici: aggiunti automaticamente in base a contract_active,
    # senza membership esplicite. Permettono regole "tutti i clienti
    # con/senza contratto" senza popolare 1490 righe di membership.
    if ctx.codcli:
        if ctx.contract_active:
            customer_groups.append("all_contract")
        else:
            customer_groups.append("no_contract")
        customer_groups = sorted(set(customer_groups))

    # has_exception_today: True se lo schedule del cliente ha un'eccezione
    # con date == today (nella TZ del cliente, default Europe/Rome).
    # Le schedule_exceptions sono date ISO (es. "2026-04-30") generate dal manager
    # che lavora in TZ italiana — quindi il confronto deve essere fatto nella
    # stessa TZ, NON nella TZ locale del relay (potenzialmente UTC).
    has_exception_today = False
    if ctx.service_hours:
        from datetime import datetime as _dt, timezone as _tz
        try:
            from zoneinfo import ZoneInfo
            tz_name = (ctx.service_hours.get("timezone") or "Europe/Rome")
            tz = ZoneInfo(tz_name)
        except Exception:  # noqa: BLE001
            tz = _tz.utc
        today_iso = _dt.now(tz).date().isoformat()
        for exc in (ctx.service_hours.get("schedule_exceptions") or []):
            if (exc.get("date") or "") == today_iso:
                has_exception_today = True
                break

    # known_customer: True se il cliente è stato risolto (codcli presente).
    # Usato dal rule engine per match_known_customer.
    known_customer = ctx.codcli is not None

    # Recipient groups (Migration 027): per ogni destinatario noto, lookup
    # group_ids cached. Permette al rule engine di valutare match_to_group_id.
    recipient_groups: dict[str, list[int]] = {}
    try:
        all_recipients = list(parsed.to_addresses or [])
        if parsed.primary_to and parsed.primary_to not in all_recipients:
            all_recipients.append(parsed.primary_to)
        for em in all_recipients:
            ids = storage.get_recipient_group_ids_by_email(em)
            if ids:
                recipient_groups[em.lower()] = ids
    except Exception as exc:  # noqa: BLE001
        logger.debug("Lookup recipient_groups fallito: %s", exc)

    return {
        "from_address": parsed.from_address,
        "to_address": parsed.primary_to,
        "to_addresses": list(parsed.to_addresses or []),
        "to_domain": parsed.primary_to_domain,
        "subject": parsed.subject,
        "body_text": parsed.body_text,
        "body_html": parsed.body_html,
        "codice_cliente": ctx.codcli,
        "customer_groups": customer_groups,
        "contract_active": ctx.contract_active,
        "known_customer": known_customer,
        "has_exception_today": has_exception_today,
        "message_id": parsed.message_id,
        "recipient_groups": recipient_groups,
        # tag opzionale (None se non valorizzato): per match_tag.
        # Origine: parsed metadata o future estensione (es. da X-Tag header).
        "tag": getattr(parsed, "tag", None),
        # M036: thread tracking — popolato dal pipeline dopo _check_thread_continuation
        "is_thread_continuation": False,
        "in_reply_to": parsed.in_reply_to,
        "references": list(parsed.references or []),
    }


def process(
    *,
    parsed: ParsedMessage,
    cfg: RelayConfig,
    storage: Storage,
    backend: Any | None = None,
    pre_action: str | None = None,
    pre_action_reason: str | None = None,
    envelope_rcpt_to: list[str] | None = None,
) -> PipelineResult:
    import uuid as _uuid
    # Pre-estrai primo destinatario interno dall'envelope (se disponibile),
    # cosi' _resolve_customer parte dal destinatario REALE (interno) invece
    # del MIME To: che puo' essere esterno (CC pattern).
    _envelope_first_internal: str | None = None
    if envelope_rcpt_to:
        for a in envelope_rcpt_to:
            la = (a or "").strip().lower()
            if la and "@" in la:
                _envelope_first_internal = la
                break
    ctx, route_row = _resolve_customer(parsed, storage,
                                        primary_to_override=_envelope_first_internal)
    # envelope_rcpt_to = i destinatari SMTP che il listener ha accettato in
    # handle_RCPT (gia' filtrati per accepted_domains). Da usare per la delivery
    # invece di parsed.to_addresses (che viene dal MIME To: header e puo' essere
    # su domini NON gestiti, es. CC esterni). Per ora lo passiamo dentro `extra`
    # cosi' tutte le sub-functions possono accedervi.

    extra: dict[str, Any] = {}
    if envelope_rcpt_to:
        # Tieni gli rcpt envelope deduplicati + lowercase in payload_metadata.
        # Serve sia per UI (mostrare i veri destinatari) sia per la delivery.
        norm = []
        seen = set()
        for a in envelope_rcpt_to:
            la = (a or "").strip().lower()
            if la and "@" in la and la not in seen:
                seen.add(la)
                norm.append(la)
        if norm:
            extra["envelope_rcpt_to"] = norm

    # `to_address_internal` = TUTTI i destinatari SMTP su dominio gestito,
    # joined con ", ". Se la mail aveva envelope.rcpt_tos (gia' filtrati da
    # handle_RCPT contro accepted_domains), li uso TUTTI per non perdere
    # destinatari interni multipli. Fallback su MIME primary_to.
    _env_list = extra.get("envelope_rcpt_to") or []
    if _env_list:
        to_address_internal = ", ".join(_env_list)
    else:
        to_address_internal = parsed.primary_to
    chain_dump: list[dict[str, Any]] = []
    rule_id: int | None = None
    # Pre-genera l'UUID dell'evento PRIMA di invocare le action, così
    # `do_ai_classify` (e altre azioni che salvano riferimenti correlati come
    # `ai_decisions.event_uuid`) possono usare l'UUID definitivo invece del
    # placeholder. `storage.insert_event` lo riceve come parametro e non lo
    # rigenera (vedi `relay/storage.py::insert_event`).
    pre_event_uuid: str = str(_uuid.uuid4())

    # =================================================================
    # PRIVACY BYPASS (admin migration 011) — pre-check assoluto, esegue
    # PRIMA del rule engine, delle aggregations e di qualsiasi azione.
    # Se from_address o uno qualsiasi dei to_addresses (per email esatta o
    # per dominio) è in bypass, la mail viene direttamente recapitata e
    # nell'audit si salva solo from/to/subject/message_id senza chain
    # né payload_metadata complesso. Nessun body memorizzato.
    # =================================================================
    is_bypass, bypass_reason = storage.is_privacy_bypassed(
        parsed.from_address, parsed.to_addresses,
    )
    if is_bypass:
        action_taken, detail, queue_extra = _do_default_delivery(
            parsed, storage, "privacy_bypass", event_uuid=pre_event_uuid,
            envelope_rcpt_to=extra.get("envelope_rcpt_to"),
        )
        event_uuid = storage.insert_event(
            from_address=parsed.from_address,
            to_address=to_address_internal,
            subject=parsed.subject,
            message_id=parsed.message_id,
            codcli=None,
            action_taken="privacy_bypass",
            rule_id=None,
            payload_metadata={
                "size_bytes": len(parsed.raw),
                "privacy_bypass": True,
                "bypass_reason": bypass_reason,
            },
        )
        if queue_extra.get("queue_id"):
            with storage.transaction() as conn:
                conn.execute(
                    "UPDATE outbound_queue SET event_uuid = ? WHERE id = ?",
                    (event_uuid, queue_extra["queue_id"]),
                )
        return PipelineResult(
            event_uuid=event_uuid,
            action_taken=action_taken,
            rule_id=None,
            codcli=None,
            detail=detail,
            chain=[],
            extra={"privacy_bypass": True, "bypass_reason": bypass_reason, **queue_extra},
        )

    # =================================================================
    # KILL SWITCH passthrough — se setting `relay_passthrough_only` è
    # ATTIVO, salta rule engine + IA + aggregations. Si fa SOLO default
    # delivery via smarthost del dominio. Da usare in emergenza al
    # cutover (un click → ritorno comportamento "smart-host puro").
    # =================================================================
    if storage.is_passthrough_only():
        action_taken, detail, queue_extra = _do_default_delivery(
            parsed, storage, "passthrough_only", event_uuid=pre_event_uuid,
            envelope_rcpt_to=extra.get("envelope_rcpt_to"),
        )
        event_uuid = storage.insert_event(
            from_address=parsed.from_address,
            to_address=to_address_internal,
            subject=parsed.subject,
            message_id=parsed.message_id,
            codcli=None,
            action_taken="passthrough_only",
            rule_id=None,
            body_text=parsed.body_text,
            body_html=parsed.body_html,
            payload_metadata={
                "size_bytes": len(parsed.raw),
                "passthrough_only": True,
                "reason": "kill_switch_active",
            },
            event_uuid=pre_event_uuid,
        )
        if queue_extra.get("queue_id"):
            with storage.transaction() as conn:
                conn.execute(
                    "UPDATE outbound_queue SET event_uuid = ? WHERE id = ?",
                    (event_uuid, queue_extra["queue_id"]),
                )
        return PipelineResult(
            event_uuid=event_uuid,
            action_taken=action_taken,
            rule_id=None,
            codcli=None,
            detail=detail,
            chain=[],
            extra={"passthrough_only": True, **queue_extra},
        )

    if pre_action == "quarantine":
        result = actions.do_quarantine(
            event_uuid=pre_event_uuid,
            parsed=parsed,
            storage=storage,
            reason=pre_action_reason or "pre_action_quarantine",
        )
        action_taken = result.action
        detail = result.detail
        extra.update(result.extra or {})
    elif _should_skip_rules(parsed, storage, route_row):
        # Bypass rule engine: alias o dominio ha apply_rules=FALSE
        # → skip valutazione regole e va direttamente a default delivery (o received_only)
        action_taken, detail, queue_extra = _do_default_delivery(parsed, storage, "rules_disabled", event_uuid=pre_event_uuid, envelope_rcpt_to=extra.get("envelope_rcpt_to"))
        extra.update(queue_extra)
    else:
        rules_rows = storage.fetch_active_rules()
        engine = RuleEngine(rules=[dict(r) for r in rules_rows])
        # M029: calcola i rule_set attivi (sempre attivi + quello del profilo cliente)
        active_set_ids = _resolve_active_rule_sets(storage, ctx.tipologia_servizio)
        # M036: thread tracking pre-evaluate
        thread_info = _check_thread_continuation(parsed, storage)
        if thread_info:
            extra["is_thread_continuation"] = True
            extra["reply_to_event_uuid"] = thread_info.get("event_uuid")
            extra["thread_root_uuid"] = (thread_info.get("thread_root_uuid")
                                          or thread_info.get("event_uuid"))
            extra["thread_parent_ticket_id"] = thread_info.get("ticket_id")
            extra["thread_parent_action"] = thread_info.get("action_taken")
            logger.info(
                "Thread continuation: parent_event=%s parent_ticket=%s",
                thread_info.get("event_uuid"), thread_info.get("ticket_id"),
            )
        ev_dict = _event_dict(parsed, ctx, storage)
        ev_dict["is_thread_continuation"] = bool(thread_info)
        outcome: RuleOutcome = engine.evaluate(
            ev_dict,
            {"in_service": ctx.in_service, "sector": ctx.sector},
            active_rule_set_ids=active_set_ids,
        )
        chain_dump = [
            {"scope": s.scope, "rule_id": s.rule_id, "rule_name": s.rule_name,
             "priority": s.priority, "matched": s.matched, "reasons": s.reasons}
            for s in outcome.chain
        ]
        # M030+M031+M033: cascata shadow (dominio -> recipient_group -> regola).
        # Se uno qualsiasi e' in shadow, blocca il dispatch reale e forza
        # default_delivery + log in payload_metadata di cosa SAREBBE stato fatto.
        shadow_check = _check_shadow_cascata(parsed, storage, outcome.rule)

        if outcome.rule is None:
            action_taken, detail, queue_extra = _do_default_delivery(parsed, storage, "no_rule_match", event_uuid=pre_event_uuid, envelope_rcpt_to=extra.get("envelope_rcpt_to"))
            extra.update(queue_extra)
            if shadow_check:
                origin, info = shadow_check
                extra["shadow_mode"] = True
                extra["shadow_origin"] = origin
                if info.get("matched_email"):
                    extra["shadow_matched_email"] = info["matched_email"]
                if info.get("matched_domain"):
                    extra["shadow_matched_domain"] = info["matched_domain"]
                extra["shadow_note"] = info.get("shadow_note")
                extra["would_have_executed"] = {"action": "default_delivery",
                                                "reason": "no_rule_match"}
        elif shadow_check:
            origin, info = shadow_check
            rule_id = int(outcome.rule["id"])
            action_name = str(outcome.rule.get("action", ""))
            action_map = outcome.rule.get("action_map") or {}
            logger.info(
                "SHADOW MODE: rule_id=%s action=%s SOPPRESSA - origine=%s",
                rule_id, action_name, origin,
            )
            extra["shadow_mode"] = True
            extra["shadow_origin"] = origin
            if info.get("matched_email"):
                extra["shadow_matched_email"] = info["matched_email"]
            if info.get("matched_domain"):
                extra["shadow_matched_domain"] = info["matched_domain"]
            extra["shadow_note"] = info.get("shadow_note")
            extra["would_have_executed"] = {
                "rule_id": rule_id,
                "rule_name": outcome.rule.get("name"),
                "action": action_name,
                "action_map": action_map,
                "forward_to_emails": outcome.rule.get("forward_to_emails"),
                "forward_to_group_id": outcome.rule.get("forward_to_group_id"),
            }
            action_taken, detail, queue_extra = _do_default_delivery(
                parsed, storage, "shadow_mode", event_uuid=pre_event_uuid,
                envelope_rcpt_to=extra.get("envelope_rcpt_to"),
            )
            extra.update(queue_extra)
        else:
            rule_id = int(outcome.rule["id"])
            action_name = str(outcome.rule.get("action", ""))
            action_map = outcome.rule.get("action_map") or {}
            res = _dispatch_action(
                action_name=action_name,
                event_uuid=pre_event_uuid,
                parsed=parsed,
                cfg=cfg,
                storage=storage,
                backend=backend,
                action_map=action_map,
                route_row=route_row,
                ctx=ctx,
                rule=outcome.rule,
                envelope_rcpt_to=extra.get("envelope_rcpt_to"),
            )
            action_taken = res.action
            detail = res.detail
            extra.update(res.extra or {})

            # Fix B (2026-05-05): se la regola create_authorized_ticket ha
            # estratto un falso positivo (codice da regex non trovato in DB),
            # ri-valuta il rule engine ESCLUDENDO la regola corrente. Permette
            # alle regole successive di gestire normalmente la mail (es. apertura
            # ticket diretto per alert CloudTIK con nome device tipo
            # RT-FRANCESCHETTA-4833 che non è un codice H24).
            if extra.get("h24_false_positive"):
                logger.info(
                    "H24 false positive su rule_id=%s: re-evaluate escludendo questa regola",
                    rule_id,
                )
                ev_dict2 = _event_dict(parsed, ctx, storage)
                ev_dict2["is_thread_continuation"] = bool(thread_info)
                outcome2 = engine.evaluate(
                    ev_dict2,
                    {"in_service": ctx.in_service, "sector": ctx.sector},
                    exclude_rule_ids={rule_id},
                    active_rule_set_ids=active_set_ids,
                )
                # Estendi la chain con la nuova evaluation per audit
                for s in outcome2.chain:
                    chain_dump.append({
                        "scope": s.scope, "rule_id": s.rule_id,
                        "rule_name": s.rule_name, "priority": s.priority,
                        "matched": s.matched,
                        "reasons": ["[re-eval after h24_false_positive] " + r for r in s.reasons],
                    })
                if outcome2.rule is not None:
                    rule_id = int(outcome2.rule["id"])
                    action_name = str(outcome2.rule.get("action", ""))
                    action_map = outcome2.rule.get("action_map") or {}
                    res2 = _dispatch_action(
                        action_name=action_name,
                        event_uuid=pre_event_uuid,
                        parsed=parsed,
                        cfg=cfg,
                        storage=storage,
                        backend=backend,
                        action_map=action_map,
                        route_row=route_row,
                        ctx=ctx,
                        rule=outcome2.rule,
                        envelope_rcpt_to=extra.get("envelope_rcpt_to"),
                    )
                    action_taken = res2.action
                    detail = res2.detail
                    extra.update(res2.extra or {})
                    extra["h24_false_positive_recovered"] = True
                else:
                    # Nessuna regola successiva → default delivery
                    action_taken, detail, queue_extra = _do_default_delivery(
                        parsed, storage, "no_rule_match_after_h24_fp",
                        event_uuid=pre_event_uuid,
                        envelope_rcpt_to=extra.get("envelope_rcpt_to"),
                    )
                    extra.update(queue_extra)

    # Valutazione aggregazioni errori (in parallelo al rule engine, non sostituisce le azioni
    # standard ma può aprire ticket aggiuntivi al raggiungimento di una soglia)
    agg_summary = _process_aggregations(parsed=parsed, cfg=cfg, storage=storage,
                                        backend=backend, codcli=ctx.codcli,
                                        event_uuid=pre_event_uuid)
    if agg_summary:
        extra["aggregations"] = agg_summary

    # M036: ricava ticket_id ereditato dal thread parent se la regola
    # non ne ha aperto uno nuovo (default_delivery con shadow del ticket).
    inherited_ticket_id = extra.get("thread_parent_ticket_id")
    event_uuid = storage.insert_event(
        from_address=parsed.from_address,
        to_address=to_address_internal,
        subject=parsed.subject,
        message_id=parsed.message_id,
        codcli=ctx.codcli,
        action_taken=action_taken,
        rule_id=rule_id,
        ticket_id=extra.get("ticket_id") or inherited_ticket_id,
        event_uuid=pre_event_uuid,  # UUID pre-generato per correlazione con ai_decisions
        body_text=parsed.body_text,
        body_html=parsed.body_html,
        # M036: thread tracking
        in_reply_to=parsed.in_reply_to,
        references=parsed.references,
        reply_to_event_uuid=extra.get("reply_to_event_uuid"),
        thread_root_uuid=extra.get("thread_root_uuid"),
        payload_metadata={
            "size_bytes": len(parsed.raw),
            "received_count": parsed.received_count,
            "is_auto_or_bulk": parsed.is_auto_or_bulk,
            "has_loop_marker": parsed.has_loop_marker,
            "in_service": ctx.in_service,
            "contract_active": ctx.contract_active,
            "chain": chain_dump,
            **extra,
        },
    )

    if extra.get("quarantine_id"):
        with storage.transaction() as conn:
            conn.execute(
                "UPDATE quarantine SET event_uuid = ? WHERE id = ?",
                (event_uuid, extra["quarantine_id"]),
            )
    if extra.get("queue_id"):
        with storage.transaction() as conn:
            conn.execute(
                "UPDATE outbound_queue SET event_uuid = ? WHERE id = ?",
                (event_uuid, extra["queue_id"]),
            )
    if extra.get("dispatch_id"):
        with storage.transaction() as conn:
            conn.execute(
                "UPDATE dispatch_queue SET event_uuid = ? WHERE id = ?",
                (event_uuid, extra["dispatch_id"]),
            )

    return PipelineResult(
        event_uuid=event_uuid,
        action_taken=action_taken,
        rule_id=rule_id,
        codcli=ctx.codcli,
        detail=detail,
        chain=chain_dump,
        extra=extra,
    )


def _dispatch_action(
    *,
    action_name: str,
    event_uuid: str,
    parsed: ParsedMessage,
    cfg: RelayConfig,
    storage: Storage,
    backend: Any | None,
    action_map: dict[str, Any],
    route_row: sqlite3.Row | None,
    ctx: CustomerContext,
    rule: dict[str, Any] | None = None,
    envelope_rcpt_to: list[str] | None = None,
) -> "actions.ActionResult":
    if action_name == "ignore":
        result = actions.do_ignore(event_uuid=event_uuid, parsed=parsed)
    elif action_name == "flag_only":
        result = actions.do_flag_only(event_uuid=event_uuid, parsed=parsed)
    elif action_name in ("default_delivery", "passthrough"):
        # Consegna al destinatario originale via smarthost del dominio
        # (apply_rules implicito). Utile come catch-all "passa tutto".
        ko_taken, ko_detail, ko_extra = _do_default_delivery(
            parsed, storage, f"rule_action_{action_name}", event_uuid=event_uuid,
            envelope_rcpt_to=envelope_rcpt_to,
        )
        result = actions.ActionResult(
            action="default_delivery", ok=True, detail=ko_detail, extra=ko_extra,
        )
    elif action_name == "quarantine":
        result = actions.do_quarantine(
            event_uuid=event_uuid, parsed=parsed, storage=storage,
            reason=str(action_map.get("reason") or "rule_quarantine"),
        )
    elif action_name == "auto_reply":
        result = actions.do_auto_reply(
            event_uuid=event_uuid, parsed=parsed, cfg=cfg, storage=storage,
            backend=backend,
            action_map=action_map,
            customer_context={"in_service": ctx.in_service, "codcli": ctx.codcli},
            rule=rule,
        )
    elif action_name == "forward":
        result = actions.do_forward(
            event_uuid=event_uuid, parsed=parsed, storage=storage, cfg=cfg,
            action_map=action_map, route_row=route_row, rule=rule,
        )
    elif action_name == "redirect":
        result = actions.do_redirect(
            event_uuid=event_uuid, parsed=parsed, storage=storage, cfg=cfg,
            action_map=action_map, route_row=route_row,
        )
    elif action_name == "create_ticket":
        result = actions.do_create_ticket(
            event_uuid=event_uuid, parsed=parsed, storage=storage,
            action_map=action_map, codcli=ctx.codcli,
        )
    elif action_name == "create_authorized_ticket":
        # H24: valida codice da subject (cascade oneshot → permanente via API
        # admin), apre ticket URGENZA=PAGAMENTO solo se valido. Vedi piano H24
        # Fase C.
        result = actions.do_create_authorized_ticket(
            event_uuid=event_uuid, parsed=parsed, cfg=cfg, storage=storage,
            backend=backend, action_map=action_map, codcli=ctx.codcli,
        )
    elif action_name in ("ai_classify", "ai_critical_check"):
        result = actions.do_ai_classify(
            event_uuid=event_uuid, parsed=parsed, cfg=cfg, storage=storage,
            backend=backend, action_map=action_map, ctx=ctx,
        )
    else:
        logger.warning("Action sconosciuta '%s', fallback a flag_only", action_name)
        result = actions.do_flag_only(event_uuid=event_uuid, parsed=parsed)

    # keep_original_delivery: oltre all'azione principale (redirect/forward/auto_reply/ecc),
    # recapita ANCHE al destinatario originale come default_delivery aggiuntiva. Utile per
    # casi tipo: la mail va dirottata ad assistenza MA deve comunque arrivare al destinatario
    # originale per visibilità. Non si applica a quarantine/ignore/flag_only/default_delivery
    # (sarebbe ridondante o senza senso).
    keep_original = action_map.get("keep_original_delivery") if isinstance(action_map, dict) else False
    # Per ai_classify*: in shadow mode la mail deve comunque recapitarsi al
    # destinatario originale (la decisione IA è solo audit). Forziamo
    # keep_original_delivery=true di default su action_name='ai_classify'.
    if action_name in ("ai_classify", "ai_critical_check"):
        keep_original = True
    if keep_original and action_name in (
        "auto_reply", "redirect", "forward", "create_ticket",
        "create_authorized_ticket",
        "ai_classify", "ai_critical_check",
    ):
        try:
            ko_action_taken, ko_detail, ko_extra = _do_default_delivery(parsed, storage, f"keep_original_after_{action_name}", event_uuid=event_uuid, envelope_rcpt_to=envelope_rcpt_to)
            existing_extra = result.extra or {}
            if ko_extra.get("queue_id"):
                existing_extra["keep_original_queue_id"] = ko_extra["queue_id"]
            result = actions.ActionResult(
                action=result.action, ok=result.ok,
                detail=f"{result.detail} + original delivery: {ko_detail}",
                extra=existing_extra,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("keep_original_delivery fallito: %s", exc)

    # also_deliver_to: copia aggiuntiva (CC) verso uno o più destinatari di riferimento.
    # Supporta:
    #   - stringa singola: "a@x.com"
    #   - stringa CSV: "a@x.com, b@y.com c@z.com"  (split su virgola/spazio)
    #   - lista già parsata: ["a@x.com", "b@y.com"]
    # Per ogni destinatario lo smarthost viene scelto in base al dominio (Domini gestiti)
    # con fallback al default_smarthost. Destinatari con stesso smarthost vengono raggruppati
    # in un unico messaggio SMTP outbound (multi-RCPT).
    cc_raw = action_map.get("also_deliver_to") if isinstance(action_map, dict) else None
    cc_targets = _normalize_address_list(cc_raw)
    if cc_targets:
        try:
            queue_ids: list[int] = []
            # Raggruppo i destinatari per smarthost (chiave=smarthost+port+tls)
            groups: dict[tuple[str, int, str], list[str]] = {}
            for rcpt in cc_targets:
                rcpt_domain = rcpt.split("@", 1)[-1].lower() if "@" in rcpt else None
                sh = storage.pick_smarthost_for_domain(
                    rcpt_domain,
                    cfg.outbound.default_smarthost,
                    cfg.outbound.default_smarthost_port,
                    cfg.outbound.default_tls,
                )
                key = (sh["smarthost"], sh["smarthost_port"], sh["smarthost_tls"])
                groups.setdefault(key, []).append(rcpt)
            for (sm, port, tls), rcpts in groups.items():
                qid = actions._enqueue_outbound(
                    storage,
                    event_uuid=event_uuid,
                    action="also_deliver",
                    mime_blob=parsed.raw,
                    mail_from=parsed.from_address or "",
                    rcpt_to=rcpts,
                    smarthost=sm,
                    smarthost_port=port,
                    smarthost_tls=tls,
                )
                queue_ids.append(qid)
                logger.info("CC also_deliver_to=%s queued id=%s via %s", rcpts, qid, sm)
            existing_extra = result.extra or {}
            existing_extra["also_deliver_queue_ids"] = queue_ids
            existing_extra["also_deliver_to"] = cc_targets
            cc_text = ", ".join(cc_targets[:3]) + ("…" if len(cc_targets) > 3 else "")
            result = actions.ActionResult(
                action=result.action, ok=result.ok,
                detail=f"{result.detail} + cc to [{cc_text}] (qids={queue_ids})",
                extra=existing_extra,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("CC also_deliver_to fallito: %s", exc)

    return result


def _process_aggregations(
    *,
    parsed: ParsedMessage,
    cfg: RelayConfig,
    storage: Storage,
    backend: Any | None,
    codcli: str | None,
    event_uuid: str | None = None,
) -> list[dict[str, Any]]:
    """Valuta tutte le aggregazioni errori attive sulla mail corrente.

    Per ogni aggregation che matcha:
    1. Verifica reset_trigger
    2. Calcola fingerprint
    3. Verifica window expiry
    4. UPSERT counter
    5. Se soglia raggiunta → enqueue dispatch_queue per apertura ticket
    6. Replica state al manager (best-effort, audit)

    Ritorna lista riassunti per audit.
    """
    summaries: list[dict[str, Any]] = []
    aggs = storage.fetch_active_aggregations()
    for agg in aggs:
        try:
            # Reset path: una mail "recovered/online/ok" può azzerare tutte le occurrences
            # attive di questa aggregation (filtrate per stesso mittente) anche se NON matcha
            # la match condition principale. Esempio: "Backup recovered" non matcha
            # match_subject_regex='backup.*fail' ma matcha reset_subject_regex='backup.*recovered'.
            if agg_module.is_reset_match(agg, parsed):
                reset_count = storage.reset_all_occurrences_for(
                    aggregation_id=int(agg["id"]),
                    from_address=parsed.from_address,
                )
                if reset_count > 0:
                    logger.info(
                        "Aggregation '%s' RESET via trigger: azzerate %d occurrences di %s",
                        agg["name"], reset_count, parsed.from_address,
                    )
                    summaries.append({
                        "aggregation_id": int(agg["id"]),
                        "aggregation_name": agg["name"],
                        "fingerprint": "(reset)",
                        "count": 0,
                        "threshold": int(agg["threshold"]),
                        "was_reset": True,
                        "was_window_expired": False,
                        "ticket_qid": None,
                        "occurrences_reset": reset_count,
                    })
                # Se è reset, non valutiamo anche il match path (sono mutex)
                continue

            if not agg_module.aggregation_matches(agg, parsed):
                continue

            is_reset = False  # già gestito sopra; qui è solo per chiarezza

            # Computa fingerprint usando l'eventuale match della subject_regex
            subject_match = None
            if agg["match_subject_regex"]:
                try:
                    subject_match = re.search(agg["match_subject_regex"], parsed.subject or "", re.IGNORECASE)
                except re.error:
                    subject_match = None
            fingerprint = agg_module.compute_fingerprint(
                agg["fingerprint_template"], parsed, subject_match=subject_match,
            )

            # Pre-check window expiry
            existing = storage.find_occurrence(int(agg["id"]), fingerprint)
            window_expired = False
            if existing is not None and not is_reset:
                window_expired = agg_module.is_outside_window(
                    existing["first_seen"], int(agg["window_hours"])
                )

            result = storage.upsert_occurrence(
                aggregation_id=int(agg["id"]),
                fingerprint=fingerprint,
                sample_from=parsed.from_address,
                sample_subject=parsed.subject,
                sample_message_id=parsed.message_id,
                is_reset=is_reset,
                is_outside_window=window_expired,
            )

            # Timer mode (delay_minutes valorizzato): l'agg apre ticket SOLO se la
            # fingerprint non viene resettata entro N minuti dalla prima occurrence.
            # threshold/window_hours vengono ignorati. L'apertura ticket è gestita
            # dallo scheduler che ogni minuto scansiona pending_ticket_until scaduti.
            try:
                delay_minutes = agg["delay_minutes"]
            except (KeyError, IndexError):
                delay_minutes = None
            timer_mode = delay_minutes is not None and int(delay_minutes) > 0

            if timer_mode:
                # Setta pending_ticket_until alla prima occorrenza (o dopo window expiry).
                # Idempotente: se già settato, lo mantiene (timer parte dal primo Problem).
                if result["first_time"] or result["was_window_expired"]:
                    pending_until = (datetime.now(timezone.utc)
                                       + timedelta(minutes=int(delay_minutes))).isoformat(timespec="seconds")
                    storage.set_pending_ticket_until(
                        int(agg["id"]), fingerprint, pending_until,
                    )
                    logger.info(
                        "Aggregation '%s' (timer %dmin) — pending_until=%s fingerprint=%s",
                        agg["name"], int(delay_minutes), pending_until, fingerprint,
                    )
                # Mai apertura ticket sincrona in timer mode
                should_ticket = False
            else:
                should_ticket = agg_module.should_open_ticket(
                    new_count=result["current_count"],
                    threshold=int(agg["threshold"]),
                    consecutive_only=bool(agg["consecutive_only"]),
                    was_reset=result["was_reset"],
                    was_window_expired=result["was_window_expired"],
                    ticket_already_opened=result["ticket_already_opened"],
                )

            ticket_qid: int | None = None
            if should_ticket:
                # Costruisco payload ticket e lo enqueue su dispatch_queue
                # (lo scheduler lo invierà al manager via /api/v1/tickets/)
                ticket_payload = {
                    "channel": "smtp_aggregated_error",
                    "external_id": f"agg-{agg['id']}-{fingerprint[:32]}",
                    "subject": f"[{agg['name']}] {parsed.subject or '(no subject)'}",
                    "body": (
                        f"Errore ripetuto rilevato.\n\n"
                        f"Aggregation: {agg['name']}\n"
                        f"Fingerprint: {fingerprint}\n"
                        f"Occorrenze: {result['current_count']} (soglia: {agg['threshold']})\n"
                        f"Finestra: {agg['window_hours']}h\n"
                        f"Mittente: {parsed.from_address}\n"
                        f"Oggetto: {parsed.subject}\n"
                        f"Prima occorrenza: {result['first_seen']}\n"
                        f"Ultima occorrenza: {result['last_seen']}\n\n"
                        f"--- Body ultima mail (max 2000 char) ---\n"
                        f"{(parsed.body_text or '')[:2000]}"
                    ),
                    "from_address": parsed.from_address,
                    "to_address": parsed.primary_to,
                    "message_id": parsed.message_id,
                    "codice_cliente": agg["ticket_codice_cliente"] or codcli,
                    "settore": agg["ticket_settore"],
                    "urgenza": agg["ticket_urgenza"],
                    "metadata": {
                        "aggregation_id": int(agg["id"]),
                        "aggregation_name": agg["name"],
                        "fingerprint": fingerprint,
                        "occurrences_count": result["current_count"],
                        "threshold": int(agg["threshold"]),
                    },
                }
                ticket_qid = actions._enqueue_dispatch(
                    storage,
                    event_uuid=event_uuid,
                    payload=ticket_payload,
                )
                # Marca la occurrence come "ticket richiesto" usando un id placeholder;
                # il vero ticket_id arriverà dal manager dopo la POST e popolerà
                # events_log.ticket_id (NON questa tabella). Per evitare doppi tentativi
                # qui marchiamo con "pending-{qid}" che blocca should_open_ticket successivi.
                storage.mark_occurrence_ticket(
                    aggregation_id=int(agg["id"]),
                    fingerprint=fingerprint,
                    ticket_id=f"pending-dispatch-{ticket_qid}",
                )
                logger.info(
                    "Aggregation '%s' soglia raggiunta (count=%d/%d), ticket enqueued (qid=%d) fingerprint=%s",
                    agg["name"], result["current_count"], int(agg["threshold"]), ticket_qid, fingerprint,
                )
            elif result["was_reset"]:
                logger.info("Aggregation '%s' RESET (counter azzerato) fingerprint=%s",
                            agg["name"], fingerprint)
            elif result["current_count"] >= int(agg["threshold"]):
                # Soglia raggiunta ma ticket già aperto → solo log
                logger.info("Aggregation '%s' count=%d (ticket già aperto) fingerprint=%s",
                            agg["name"], result["current_count"], fingerprint)

            # Best-effort: replica state al manager (audit, multi-relay)
            if backend is not None:
                try:
                    backend.replicate_occurrence(int(agg["id"]), {
                        "fingerprint": fingerprint,
                        "current_count": result["current_count"],
                        "first_seen": result["first_seen"],
                        "last_seen": result["last_seen"],
                        "sample_from": parsed.from_address,
                        "sample_subject": parsed.subject,
                        "sample_received_at": result["last_seen"],
                        "sample_message_id": parsed.message_id,
                        "ticket_opened_at": _now_iso() if ticket_qid else None,
                        "ticket_id": f"pending-dispatch-{ticket_qid}" if ticket_qid else None,
                        "last_reset_at": result["last_seen"] if result["was_reset"] else None,
                    })
                except Exception as exc:  # noqa: BLE001
                    logger.debug("replicate_occurrence non riuscita (best-effort): %s", exc)

            summaries.append({
                "aggregation_id": int(agg["id"]),
                "aggregation_name": agg["name"],
                "fingerprint": fingerprint,
                "count": result["current_count"],
                "threshold": int(agg["threshold"]),
                "was_reset": result["was_reset"],
                "was_window_expired": result["was_window_expired"],
                "ticket_qid": ticket_qid,
            })
        except Exception as exc:  # noqa: BLE001
            logger.exception("Aggregation '%s' eccezione: %s", agg.get("name", "?"), exc)

    return summaries


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _should_skip_rules(parsed: ParsedMessage, storage: Storage, route_row: sqlite3.Row | None) -> bool:
    """Determina se saltare il rule engine in base al flag apply_rules.

    Priorità: alias-route > dominio. Se uno dei due ha apply_rules=FALSE, skip.
    """
    if route_row is not None:
        try:
            apply = route_row["apply_rules"]
            if apply is not None and not int(apply):
                return True
        except (KeyError, IndexError, ValueError):
            pass
    domain = parsed.primary_to_domain
    if domain:
        domain_row = storage.find_domain_routing(domain)
        if domain_row is not None:
            try:
                apply = domain_row["apply_rules"]
                if apply is not None and not int(apply):
                    return True
            except (KeyError, IndexError, ValueError):
                pass
    return False


def _do_default_delivery(
    parsed: ParsedMessage,
    storage: Storage,
    reason: str,
    event_uuid: str | None = None,
    envelope_rcpt_to: list[str] | None = None,
) -> tuple[str, str, dict[str, Any]]:
    """Esegue la default delivery raggruppando i destinatari per dominio gestito.

    PRIORITA' destinatari:
      1. envelope_rcpt_to (RCPT TO SMTP gia' filtrati da handle_RCPT) — corretti.
      2. parsed.to_addresses (MIME To: header) — fallback per casi legacy.

    Per ogni dominio gestito (presente in domain_routing) raggruppa i suoi
    destinatari e fa UN enqueue separato verso lo smarthost del dominio.
    Destinatari su domini NON gestiti vengono ignorati: ESVA li gestisce a monte.

    Se NESSUN destinatario e' su un dominio gestito → received_only.

    Ritorna (action_taken, detail, extra_dict).
    """
    if event_uuid is None:
        import uuid as _uuid_mod
        event_uuid = str(_uuid_mod.uuid4())

    # Sorgente prioritaria: envelope SMTP. Fallback: MIME To: header.
    candidates: list[str] = []
    if envelope_rcpt_to:
        candidates = [a for a in envelope_rcpt_to if a and "@" in a]
    if not candidates:
        candidates = list(parsed.to_addresses or [])
        if not candidates and parsed.primary_to:
            candidates = [parsed.primary_to]
    if not candidates:
        return ("received_only", f"{reason}, no rcpt disponibili",
                {"reason": reason})

    # Raggruppa per dominio destinatario
    by_domain: dict[str, list[str]] = {}
    skipped: list[str] = []
    for addr in candidates:
        norm = addr.strip().lower()
        if "@" not in norm:
            continue
        dom = norm.rsplit("@", 1)[1]
        domain_row = storage.find_domain_routing(dom)
        if domain_row is None:
            skipped.append(norm)
            continue
        by_domain.setdefault(dom, []).append(norm)

    if not by_domain:
        # Nessun destinatario su dominio gestito: ESVA dovrebbe averli gia'
        # smistati a monte; segnaliamo come received_only per audit.
        return ("received_only",
                f"{reason}, nessun rcpt su dominio gestito (skipped={','.join(skipped[:5])})",
                {"reason": reason, "skipped_external": skipped})

    # Enqueue separato per ogni dominio gestito (smarthost diverso)
    queue_ids: list[int] = []
    enqueued_rcpt: list[str] = []
    smarthosts_used: list[str] = []
    for dom, rcpts in by_domain.items():
        domain_row = storage.find_domain_routing(dom)
        if domain_row is None:
            continue
        qid = actions._enqueue_outbound(
            storage,
            event_uuid=event_uuid,
            action="default_delivery",
            mime_blob=parsed.raw,
            mail_from=parsed.from_address or "",
            rcpt_to=rcpts,
            smarthost=domain_row["smarthost"],
            smarthost_port=int(domain_row["smarthost_port"] or 25),
            smarthost_tls=domain_row["smarthost_tls"] or "opportunistic",
        )
        queue_ids.append(qid)
        enqueued_rcpt.extend(rcpts)
        smarthosts_used.append(f"{domain_row['smarthost']}:{domain_row['smarthost_port']}")

    detail = (f"queued id={','.join(str(q) for q in queue_ids)} "
              f"via {','.join(smarthosts_used)} ({reason})")
    if skipped:
        detail += f" — skipped non-gestiti: {','.join(skipped[:3])}"
    return ("default_delivery", detail,
            {"queue_id": queue_ids[0] if len(queue_ids) == 1 else queue_ids,
             "queue_ids": queue_ids,
             "reason": reason,
             "enqueued_rcpt_to": enqueued_rcpt,
             "skipped_external_rcpt": skipped or None})


_ADDR_SPLIT_RE = re.compile(r"[\s,;]+")


def _normalize_address_list(value: Any) -> list[str]:
    """Normalizza il valore di also_deliver_to in una lista di indirizzi email.

    Accetta:
    - None / "" / [] → []
    - "a@x.com"      → ["a@x.com"]
    - "a@x, b@y.com" → ["a@x.com", "b@y.com"]
    - ["a@x", " b@y "] → ["a@x", "b@y"]
    Filtra solo indirizzi che contengono '@', deduplica preservando l'ordine.
    """
    if not value:
        return []
    if isinstance(value, str):
        candidates = _ADDR_SPLIT_RE.split(value.strip())
    elif isinstance(value, (list, tuple)):
        candidates = []
        for v in value:
            if isinstance(v, str):
                candidates.extend(_ADDR_SPLIT_RE.split(v.strip()))
    else:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for c in candidates:
        c = c.strip().strip("<>").lower()
        if not c or "@" not in c:
            continue
        if c in seen:
            continue
        seen.add(c)
        out.append(c)
    return out
