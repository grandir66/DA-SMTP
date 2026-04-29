"""API endpoints REST per il listener relay (compat con il vecchio manager).

Il listener `services/smtp_relay/relay/` parla con questo backend via X-API-Key.
Stessi endpoint del manager Stormshield (`/api/v1/relay/*`) per drop-in replacement:
basta cambiare `base_url` nel config del listener e tutto funziona.

Auth: `X-API-Key` header. La chiave è in setting `relay_api_key` del SQLite (auto-
generata al primo avvio se non presente).
"""
from __future__ import annotations

import functools
import json
import logging
import os
import secrets
import string
from datetime import datetime, timedelta, timezone

from flask import Blueprint, abort, current_app, jsonify, request

logger = logging.getLogger(__name__)


api_bp = Blueprint("api", __name__, url_prefix="/api/v1/relay")


def _storage():
    return current_app.extensions["domarc_storage"]


def _customer_source():
    return current_app.extensions["domarc_customer_source"]


def _get_or_create_api_key() -> str:
    """Recupera la chiave API dalla tabella settings; la crea al primo accesso."""
    storage = _storage()
    with storage._connect() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = 'relay_api_key'").fetchone()
        if row:
            return str(row["value"])
        new_key = secrets.token_urlsafe(48)
        conn.execute(
            """INSERT INTO settings (key, value, description)
               VALUES ('relay_api_key', ?,
                       'Chiave X-API-Key per il listener relay verso questo admin standalone.')""",
            (new_key,),
        )
        conn.commit()
        logger.warning("API key relay generata: %s — copiala nel secrets.env del listener", new_key)
        return new_key


def require_api_key(f):
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        provided = request.headers.get("X-API-Key", "").strip()
        if not provided:
            return jsonify({"error": "X-API-Key mancante"}), 401
        expected = _get_or_create_api_key()
        if not secrets.compare_digest(provided, expected):
            return jsonify({"error": "X-API-Key invalida"}), 401
        return f(*args, **kwargs)
    return wrapper


# ============================================================ ENDPOINTS ===

@api_bp.route("/health", methods=["GET"])
@require_api_key
def health():
    storage = _storage()
    return jsonify({
        "status": "ok",
        "version": current_app.extensions["domarc_version"],
        "schema_version": storage.schema_version(),
    }), 200


@api_bp.route("/customers/active", methods=["GET"])
@require_api_key
def customers_active():
    """Anagrafica clienti attivi dal customer source configurato."""
    cs = _customer_source()
    customers = cs.list_customers()
    out = []
    for c in customers:
        out.append({
            "codcli": c.codice_cliente,
            "ragione_sociale": c.ragione_sociale,
            "domains": list(c.domains or []),
            "aliases": list(c.aliases or []),
            "contract_active": bool(c.contract_active),
            "service_hours": {
                "profile": c.tipologia_servizio,
                "holidays": c.holidays or [],
                "schedule_overrides": c.schedule_overrides or [],
            },
        })
    return jsonify({
        "synced_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "customers": out,
    }), 200


@api_bp.route("/rules/active", methods=["GET"])
@require_api_key
def rules_active():
    """Regole attive in formato flat per il listener legacy.

    Dalla migration 010 le regole possono essere organizzate in gerarchia
    padre/figlio (1 livello). Qui chiamiamo ``flatten_rules_for_listener``
    che appiattisce gruppi+figli in record flat semanticamente equivalenti
    al modello pre-010, transparente al listener.
    Param ``?tenant_id=N`` per filtro singolo tenant.
    """
    storage = _storage()
    tid_raw = request.args.get("tenant_id")
    tid = None
    if tid_raw:
        try:
            tid = int(tid_raw)
        except ValueError:
            return jsonify({"error": "tenant_id deve essere int"}), 400
    rules = storage.flatten_rules_for_listener(tenant_id=tid)
    out = []
    for r in rules:
        am = r.get("action_map") or {}
        # Filtra meta-record gruppi (action='group') che non vanno mai serviti
        # al listener. flatten_rules_for_listener già non li include, ma
        # guard difensivo.
        if r.get("is_group") or r.get("action") == "group":
            continue
        payload = {
            "id": r["id"],
            "name": r["name"],
            "applies_to": "smtp",
            "tenant_id": r["tenant_id"],
            "scope_type": r.get("scope_type") or "global",
            "scope_ref": r.get("scope_ref"),
            "priority": int(r.get("priority", 100)),
            "enabled": bool(r["enabled"]),
            "match_from_regex": r.get("match_from_regex"),
            "match_to_regex": r.get("match_to_regex"),
            "match_subject_regex": r.get("match_subject_regex"),
            "match_body_regex": r.get("match_body_regex"),
            "match_to_domain": r.get("match_to_domain"),
            "match_from_domain": r.get("match_from_domain"),
            "match_in_service": r.get("match_in_service"),
            "match_at_hours": r.get("match_at_hours"),
            "match_contract_active": r.get("match_contract_active"),
            "match_known_customer": r.get("match_known_customer"),
            "match_has_exception_today": r.get("match_has_exception_today"),
            "action": r["action"],
            "action_map": am,
            "continue_after_match": bool(r.get("continue_after_match")),
        }
        # Metadata opzionali per audit (ignorati dal listener legacy).
        if r.get("_source_group_id"):
            payload["_source_group_id"] = r["_source_group_id"]
        if r.get("_source_child_id"):
            payload["_source_child_id"] = r["_source_child_id"]
        out.append(payload)
    return jsonify({
        "synced_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "rules": out,
    }), 200


@api_bp.route("/ai/classify", methods=["POST"])
@require_api_key
def ai_classify():
    """Inferenza inline chiamata dal listener (action `do_ai_classify`).

    Body atteso:
    ``{event: {from_address, to_address, subject, body_text, ...},
       event_uuid: "...", customer_context: {codcli, contract_active, in_service},
       tenant_id: 1}``

    Risposta: vedi `ai_assistant.decisions.classify_email`.
    """
    from ..ai_assistant.decisions import classify_email
    from ..ai_assistant.router import get_ai_router

    payload = request.get_json(silent=True) or {}
    event = payload.get("event") or {}
    event_uuid = payload.get("event_uuid")
    customer_ctx = payload.get("customer_context") or {}
    tid = int(payload.get("tenant_id") or 1)

    storage = _storage()
    router = get_ai_router(storage, tenant_id=tid)
    result = classify_email(
        storage=storage, router=router,
        event=event, event_uuid=event_uuid,
        customer_context=customer_ctx, tenant_id=tid,
    )
    return jsonify(result), 200


@api_bp.route("/ai-bindings/active", methods=["GET"])
@require_api_key
def ai_bindings_active():
    """Listener cache: bindings attivi per i job IA inline (es. classify_email)."""
    storage = _storage()
    tid_raw = request.args.get("tenant_id")
    tid = int(tid_raw) if tid_raw else None
    bindings = storage.list_ai_job_bindings(tenant_id=tid, only_enabled=True)
    out = [{
        "job_code": b["job_code"],
        "binding_id": b["id"],
        "provider_id": b["provider_id"],
        "model_id": b["model_id"],
        "timeout_ms": b["timeout_ms"] or 5000,
        "version": b["version"],
        "traffic_split": b["traffic_split"],
    } for b in bindings]
    return jsonify({
        "synced_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "bindings": out,
    }), 200


@api_bp.route("/privacy-bypass/active", methods=["GET"])
@require_api_key
def privacy_bypass_active():
    """Lista privacy-bypass attiva per il listener.

    Il listener pre-controlla ogni mail (lato pipeline.py) contro questa
    lista PRIMA del rule engine: se from o to corrispondono a un'email o un
    dominio in lista, la mail viene direttamente recapitata, senza
    elaborazione del corpo, senza rule engine, senza aggregations e con
    audit log minimale.
    """
    storage = _storage()
    tid_raw = request.args.get("tenant_id")
    tid = None
    if tid_raw:
        try:
            tid = int(tid_raw)
        except ValueError:
            return jsonify({"error": "tenant_id deve essere int"}), 400
    payload = storage.list_privacy_bypass_active(tenant_id=tid)
    return jsonify({
        "synced_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        **payload,
    }), 200


@api_bp.route("/templates/active", methods=["GET"])
@require_api_key
def templates_active():
    storage = _storage()
    tid_raw = request.args.get("tenant_id")
    tid = None
    if tid_raw:
        try:
            tid = int(tid_raw)
        except ValueError:
            return jsonify({"error": "tenant_id deve essere int"}), 400
    tpls = storage.list_templates(tenant_id=tid, only_enabled=True)
    out = []
    for t in tpls:
        out.append({
            "id": int(t["id"]),
            "name": t["name"],
            "description": t.get("description"),
            "subject_tmpl": t["subject_tmpl"],
            "body_html_tmpl": t["body_html_tmpl"],
            "body_text_tmpl": t.get("body_text_tmpl"),
            "reply_from_name": t.get("reply_from_name"),
            "reply_from_email": t.get("reply_from_email"),
            "attachment_paths": t.get("attachment_paths") or [],
            "tenant_id": t["tenant_id"],
        })
    return jsonify({
        "synced_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "templates": out,
    }), 200


@api_bp.route("/aggregations/active", methods=["GET"])
@require_api_key
def aggregations_active():
    storage = _storage()
    aggs = storage.list_aggregations(only_enabled=True)
    out = []
    for a in aggs:
        out.append({
            "id": int(a["id"]),
            "tenant_id": a["tenant_id"],
            "name": a["name"],
            "description": a.get("description"),
            "match_from_regex": a.get("match_from_regex"),
            "match_subject_regex": a.get("match_subject_regex"),
            "match_body_regex": a.get("match_body_regex"),
            "fingerprint_template": a.get("fingerprint_template") or "${from}|${subject_normalized}",
            "threshold": int(a.get("threshold", 2)),
            "consecutive_only": bool(a.get("consecutive_only")),
            "window_hours": int(a.get("window_hours", 24)),
            "reset_subject_regex": a.get("reset_subject_regex"),
            "reset_from_regex": a.get("reset_from_regex"),
            "ticket_settore": a.get("ticket_settore"),
            "ticket_urgenza": a.get("ticket_urgenza"),
            "ticket_codice_cliente": a.get("ticket_codice_cliente"),
            "priority": int(a.get("priority", 100)),
        })
    return jsonify({
        "synced_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "aggregations": out,
    }), 200


@api_bp.route("/events", methods=["POST"])
@require_api_key
def events_post():
    """Riceve eventi dal listener (audit + body retention)."""
    body = request.get_json(silent=True) or {}
    events = body.get("events") or []
    if not isinstance(events, list):
        return jsonify({"error": "events deve essere una lista"}), 400
    storage = _storage()

    accepted = 0
    duplicates = 0
    errors: list = []

    # Calcola TTL retention dai settings
    retention_hours = 6
    max_size_bytes = 256 * 1024
    try:
        with storage._connect() as conn:
            for k, v in conn.execute(
                "SELECT key, value FROM settings WHERE key IN ('body_retention_hours','body_max_size_kb')"
            ).fetchall():
                try:
                    if k == "body_retention_hours":
                        retention_hours = max(0, int(v))
                    elif k == "body_max_size_kb":
                        max_size_bytes = max(1024, int(v) * 1024)
                except (TypeError, ValueError):
                    pass
    except Exception as exc:  # noqa: BLE001
        logger.debug("settings retention lookup: %s", exc)

    for evt in events:
        if not isinstance(evt, dict):
            errors.append({"error": "non-dict"})
            continue
        uuid_str = evt.get("relay_event_uuid")
        if not uuid_str:
            errors.append({"error": "missing relay_event_uuid"})
            continue
        # Body con TTL truncate
        bt = evt.get("body_text") or None
        bh = evt.get("body_html") or None
        if retention_hours > 0:
            if bt and len(bt.encode("utf-8", errors="replace")) > max_size_bytes:
                bt = bt.encode("utf-8", errors="replace")[:max_size_bytes].decode("utf-8", errors="replace") + "\n[TRUNCATED]"
            if bh and len(bh.encode("utf-8", errors="replace")) > max_size_bytes:
                bh = bh.encode("utf-8", errors="replace")[:max_size_bytes].decode("utf-8", errors="replace") + "\n<!--TRUNCATED-->"
            try:
                recvd = datetime.fromisoformat(str(evt.get("received_at", "")).replace("Z", "+00:00"))
                if recvd.tzinfo is None:
                    recvd = recvd.replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                recvd = datetime.now(timezone.utc)
            body_expires_at = (recvd + timedelta(hours=retention_hours)).strftime("%Y-%m-%d %H:%M:%S")
        else:
            bt = bh = None
            body_expires_at = None

        try:
            new_id = storage.insert_event({
                "tenant_id": evt.get("tenant_id", 1),
                "relay_event_uuid": str(uuid_str),
                "received_at": evt.get("received_at") or datetime.now(timezone.utc).isoformat(),
                "from_address": evt.get("from_address"),
                "to_address": evt.get("to_address"),
                "subject": evt.get("subject"),
                "message_id": evt.get("message_id"),
                "codice_cliente": evt.get("codice_cliente"),
                "action_taken": evt.get("action_taken"),
                "rule_id": evt.get("rule_id"),
                "ticket_id": evt.get("ticket_id"),
                "payload_metadata": evt.get("payload_metadata"),
                "body_text": bt,
                "body_html": bh,
                "body_expires_at": body_expires_at,
            })
            if new_id:
                accepted += 1
                # Auto-upsert anagrafica indirizzi (mittente + destinatario)
                _upsert_address_from_event(storage, evt)
                # F2 — Error aggregator IA: hook per clustering errori
                _try_aggregate_error_cluster(storage, evt)
            else:
                duplicates += 1
        except Exception as exc:  # noqa: BLE001
            errors.append({"uuid": str(uuid_str), "error": str(exc)})

    return jsonify({"accepted": accepted, "duplicates": duplicates, "errors": errors}), 200


def _try_aggregate_error_cluster(storage, evt: dict) -> None:
    """Tenta clustering dell'evento (F2). Best-effort, errori loggati ma non bloccanti."""
    try:
        from ..ai_assistant.error_aggregator import process_event_for_clustering
        result = process_event_for_clustering(
            storage=storage,
            tenant_id=int(evt.get("tenant_id") or 1),
            event_uuid=evt.get("relay_event_uuid"),
            subject=evt.get("subject"),
            body_excerpt=(evt.get("body_text") or "")[:500],
        )
        if result:
            logger.info("Error cluster: %s", result)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Error aggregator skip: %s", exc)


def _upsert_address_from_event(storage, evt: dict) -> None:
    """Aggiorna addresses_from + addresses_to dai dati dell'evento.

    - Per il mittente: insert/update con seen_count++ e last_seen_at; se
      l'evento ha già `codice_cliente`, lo eredita con `codcli_source='auto'`
      (a meno che esista già un override 'manual').
    - Per il destinatario: insert/update con seen_count++ e last_seen_at.
    """
    from_addr = (evt.get("from_address") or "").strip().lower()
    to_addr = (evt.get("to_address") or "").strip().lower()
    tenant_id = int(evt.get("tenant_id") or 1)
    codcli = evt.get("codice_cliente")
    try:
        with storage.transaction() as conn:
            if from_addr and "@" in from_addr:
                local, dom = from_addr.rsplit("@", 1)
                conn.execute(
                    """INSERT INTO addresses_from
                          (tenant_id, email_address, local_part, domain,
                           codice_cliente, codcli_source,
                           seen_count, first_seen_at, last_seen_at, created_by)
                       VALUES (?, ?, ?, ?, ?, ?, 1, datetime('now'), datetime('now'), 'auto')
                       ON CONFLICT(tenant_id, email_address) DO UPDATE SET
                          seen_count = seen_count + 1,
                          last_seen_at = datetime('now'),
                          codice_cliente = CASE
                              WHEN codcli_source = 'manual' THEN codice_cliente
                              ELSE COALESCE(excluded.codice_cliente, codice_cliente)
                          END,
                          codcli_source = CASE
                              WHEN codcli_source = 'manual' THEN 'manual'
                              ELSE COALESCE(excluded.codcli_source, codcli_source)
                          END""",
                    (tenant_id, from_addr, local, dom,
                     codcli, ('auto' if codcli else None)),
                )
            if to_addr and "@" in to_addr:
                local, dom = to_addr.rsplit("@", 1)
                conn.execute(
                    """INSERT INTO addresses_to
                          (tenant_id, email_address, local_part, domain,
                           seen_count, first_seen_at, last_seen_at)
                       VALUES (?, ?, ?, ?, 1, datetime('now'), datetime('now'))
                       ON CONFLICT(tenant_id, email_address) DO UPDATE SET
                          seen_count = seen_count + 1,
                          last_seen_at = datetime('now')""",
                    (tenant_id, to_addr, local, dom),
                )
    except Exception as exc:  # noqa: BLE001
        logger.debug("upsert addresses skip: %s", exc)


@api_bp.route("/auth-codes", methods=["POST"])
@require_api_key
def auth_codes_post():
    """Genera un codice autorizzazione (chiamato dal listener fuori orario)."""
    body = request.get_json(silent=True) or {}
    codcli = (body.get("codice_cliente") or "").strip().upper() or None
    rule_id = body.get("rule_id")
    note = body.get("note") or None
    try:
        ttl_hours = int(body.get("ttl_hours") or 48)
    except (TypeError, ValueError):
        ttl_hours = 48
    tenant_id = int(body.get("tenant_id") or 1)
    try:
        result = _storage().issue_auth_code(
            tenant_id=tenant_id, codice_cliente=codcli,
            rule_id=int(rule_id) if rule_id else None,
            ttl_hours=ttl_hours, note=note,
        )
        return jsonify(result), 200
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)}), 500


# Domain routing / settings / routes: stub minimi per compat (il listener attuale li chiama)

@api_bp.route("/settings/active", methods=["GET"])
@require_api_key
def settings_active():
    storage = _storage()
    out = {}
    with storage._connect() as conn:
        for k, v in conn.execute("SELECT key, value FROM settings").fetchall():
            out[k] = v
    return jsonify({
        "synced_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "settings": out,
    }), 200


@api_bp.route("/routes/active", methods=["GET"])
@require_api_key
def routes_active():
    """Routes (alias intercept) attive — letti dal DB."""
    storage = _storage()
    rows = storage.list_routes(only_enabled=True)
    return jsonify({
        "synced_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "routes": [
            {
                "id": r["id"],
                "tenant_id": r["tenant_id"],
                "alias": f"{r['local_part']}@{r['domain']}",
                "local_part": r["local_part"],
                "domain": r["domain"],
                "codice_cliente": r.get("codice_cliente"),
                "forward_target": r.get("forward_target"),
                "forward_port": int(r.get("forward_port") or 25),
                "forward_tls": r.get("forward_tls") or "opportunistic",
                "redirect_target": r.get("redirect_target"),
                "apply_rules": bool(r.get("apply_rules", True)),
                "enabled": bool(r["enabled"]),
            } for r in rows
        ],
    }), 200


@api_bp.route("/domain-routing/active", methods=["GET"])
@require_api_key
def domain_routing_active():
    """Domain routing (smarthost per dominio).

    Schema payload allineato al listener: campo `smarthost` (non `smarthost_host`)
    + `notes`. Manteniamo `smarthost_host` come alias per consumer alternativi.
    """
    storage = _storage()
    rows = storage.list_domain_routing()
    return jsonify({
        "synced_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "domains": [
            {
                "id": d["id"],
                "tenant_id": d["tenant_id"],
                "domain": d["domain"],
                "smarthost": d.get("smarthost_host"),         # nome atteso dal listener
                "smarthost_host": d.get("smarthost_host"),    # alias retrocompat
                "smarthost_port": int(d.get("smarthost_port") or 25),
                "smarthost_tls": d.get("smarthost_tls") or "opportunistic",
                "apply_rules": bool(d.get("apply_rules", True)),
                "enabled": bool(d["enabled"]),
                "notes": d.get("notes"),
            } for d in rows if d.get("enabled")
        ],
    }), 200


@api_bp.route("/aggregations/<int:agg_id>/occurrence", methods=["POST"])
@require_api_key
def aggregations_occurrence(agg_id: int):
    """Stub per replicazione stato occurrences dal relay locale."""
    # Accetta e ignora silenziosamente per ora — il listener mantiene la sua state in SQLite locale
    return jsonify({"ok": True}), 200
