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


@dataclass
class PipelineResult:
    event_uuid: str
    action_taken: str
    rule_id: int | None
    codcli: str | None
    detail: str
    chain: list[dict[str, Any]]
    extra: dict[str, Any]


def _resolve_customer(parsed: ParsedMessage, storage: Storage) -> tuple[CustomerContext, sqlite3.Row | None]:
    route_row: sqlite3.Row | None = None
    if parsed.primary_to_local and parsed.primary_to_domain:
        route_row = storage.find_route(parsed.primary_to_local, parsed.primary_to_domain)

    cust_row: sqlite3.Row | None = None
    codcli: str | None = None
    if route_row is not None and route_row["codcli"]:
        codcli = str(route_row["codcli"])
        cust_row = storage.find_customer_by_alias(f"{parsed.primary_to_local}@{parsed.primary_to_domain}")
        if cust_row is None and codcli:
            with storage._connect() as conn:  # type: ignore[attr-defined]
                cust_row = conn.execute("SELECT * FROM customers_cache WHERE codcli = ?", (codcli,)).fetchone()
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

    ctx = CustomerContext(
        codcli=codcli,
        contract_active=contract_active,
        in_service=in_srv,
        sector=None,
        service_hours=schedule,
    )
    return ctx, route_row


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

    return {
        "from_address": parsed.from_address,
        "to_address": parsed.primary_to,
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
        # tag opzionale (None se non valorizzato): per match_tag.
        # Origine: parsed metadata o future estensione (es. da X-Tag header).
        "tag": getattr(parsed, "tag", None),
    }


def process(
    *,
    parsed: ParsedMessage,
    cfg: RelayConfig,
    storage: Storage,
    backend: Any | None = None,
    pre_action: str | None = None,
    pre_action_reason: str | None = None,
) -> PipelineResult:
    import uuid as _uuid
    ctx, route_row = _resolve_customer(parsed, storage)

    extra: dict[str, Any] = {}
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
        )
        event_uuid = storage.insert_event(
            from_address=parsed.from_address,
            to_address=parsed.primary_to,
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
        )
        event_uuid = storage.insert_event(
            from_address=parsed.from_address,
            to_address=parsed.primary_to,
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
        action_taken, detail, queue_extra = _do_default_delivery(parsed, storage, "rules_disabled", event_uuid=pre_event_uuid)
        extra.update(queue_extra)
    else:
        rules_rows = storage.fetch_active_rules()
        engine = RuleEngine(rules=[dict(r) for r in rules_rows])
        outcome: RuleOutcome = engine.evaluate(_event_dict(parsed, ctx, storage), {"in_service": ctx.in_service, "sector": ctx.sector})
        chain_dump = [
            {"scope": s.scope, "rule_id": s.rule_id, "rule_name": s.rule_name,
             "priority": s.priority, "matched": s.matched, "reasons": s.reasons}
            for s in outcome.chain
        ]
        if outcome.rule is None:
            action_taken, detail, queue_extra = _do_default_delivery(parsed, storage, "no_rule_match", event_uuid=pre_event_uuid)
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
            )
            action_taken = res.action
            detail = res.detail
            extra.update(res.extra or {})

    # Valutazione aggregazioni errori (in parallelo al rule engine, non sostituisce le azioni
    # standard ma può aprire ticket aggiuntivi al raggiungimento di una soglia)
    agg_summary = _process_aggregations(parsed=parsed, cfg=cfg, storage=storage,
                                        backend=backend, codcli=ctx.codcli)
    if agg_summary:
        extra["aggregations"] = agg_summary

    event_uuid = storage.insert_event(
        from_address=parsed.from_address,
        to_address=parsed.primary_to,
        subject=parsed.subject,
        message_id=parsed.message_id,
        codcli=ctx.codcli,
        action_taken=action_taken,
        rule_id=rule_id,
        event_uuid=pre_event_uuid,  # UUID pre-generato per correlazione con ai_decisions
        body_text=parsed.body_text,
        body_html=parsed.body_html,
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
            action_map=action_map, route_row=route_row,
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
            ko_action_taken, ko_detail, ko_extra = _do_default_delivery(parsed, storage, f"keep_original_after_{action_name}", event_uuid=event_uuid)
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
                    event_uuid=pre_event_uuid,
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
) -> tuple[str, str, dict[str, Any]]:
    """Esegue la default delivery verso lo smarthost del dominio destinatario.

    Se ``event_uuid`` non è fornito, ne viene generato uno fresh: la mail
    viene comunque consegnata e tracciata in outbound_queue.

    Ritorna (action_taken, detail, extra_dict_per_event_metadata).
    """
    if event_uuid is None:
        import uuid as _uuid_mod
        event_uuid = str(_uuid_mod.uuid4())
    domain_row = None
    if parsed.primary_to_domain:
        domain_row = storage.find_domain_routing(parsed.primary_to_domain)
    if domain_row is None:
        return ("received_only",
                f"{reason}, dominio {parsed.primary_to_domain or '(?)'} non gestito",
                {"reason": reason})
    rcpt = list(parsed.to_addresses) or ([parsed.primary_to] if parsed.primary_to else [])
    if not rcpt:
        return ("received_only", f"{reason}, no rcpt nel MIME", {"reason": reason})
    qid = actions._enqueue_outbound(
        storage,
        event_uuid=event_uuid,
        action="default_delivery",
        mime_blob=parsed.raw,
        mail_from=parsed.from_address or "",
        rcpt_to=rcpt,
        smarthost=domain_row["smarthost"],
        smarthost_port=int(domain_row["smarthost_port"] or 25),
        smarthost_tls=domain_row["smarthost_tls"] or "opportunistic",
    )
    return ("default_delivery",
            f"queued id={qid} via {domain_row['smarthost']}:{domain_row['smarthost_port']} ({reason})",
            {"queue_id": qid, "reason": reason})


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
