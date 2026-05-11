"""Events list + detail (con body iframe se retention attiva)."""
from __future__ import annotations

import csv
import io
import json
from datetime import datetime, timezone
from typing import Any

from flask import Blueprint, Response, abort, current_app, flash, g, jsonify, redirect, render_template, request, url_for
from markupsafe import escape as _escape

from ..auth import login_required

events_bp = Blueprint("events", __name__)


def _storage():
    return current_app.extensions["domarc_storage"]


def _tid() -> int:
    return int(getattr(g, "current_tenant_id", 1))


@events_bp.route("/events")
@login_required()
def list_view():
    try:
        hours = max(1, min(int(request.args.get("hours") or 24), 720))
    except (TypeError, ValueError):
        hours = 24
    try:
        page = max(1, int(request.args.get("page") or 1))
    except ValueError:
        page = 1
    try:
        page_size = max(25, min(int(request.args.get("page_size") or 50), 100))
    except ValueError:
        page_size = 50

    filters = {
        "action": (request.args.get("action") or "").strip() or None,
        "q": (request.args.get("q") or "").strip() or None,
        "no_client": request.args.get("no_client") in ("on", "true", "1"),
        "no_rule": request.args.get("no_rule") in ("on", "true", "1"),
        "only_ticket": request.args.get("only_ticket") in ("on", "true", "1"),
        "only_shadow": request.args.get("only_shadow") in ("on", "true", "1"),
    }
    events, total = _storage().list_events(
        tenant_id=_tid(), hours=hours,
        page=page, page_size=page_size, filters=filters,
    )
    # Formattazione compatta data per ogni evento (HH:MM oggi / DD/MM HH:MM altrimenti)
    _now = datetime.now(timezone.utc)
    for e in events:
        e["received_short"] = _format_short_ts(e.get("received_at"), _now)
    pages = max(1, (total + page_size - 1) // page_size)
    available_actions = ["create_ticket", "auto_reply", "forward", "redirect",
                         "quarantine", "flag_only", "ignore", "default_delivery",
                         "create_authorized_ticket", "received_only", "passthrough_only",
                         "ai_classify", "ai_taxonomy", "ai_classify_failsafe"]

    # KPI/stats aggregati su dataset ALLARGATO (non solo pagina corrente, ma
    # con cap a 2000 eventi per evitare costo eccessivo). Le statistiche
    # rispettano i filtri attivi (hours, action, q, ecc.).
    stats = _compute_event_stats(
        _storage(), tenant_id=_tid(), hours=hours, filters=filters,
    )

    return render_template(
        "admin/events_list.html",
        events=events, total=total, page=page, page_size=page_size, pages=pages,
        available_actions=available_actions,
        filters={**filters, "hours": hours, "q": request.args.get("q") or ""},
        stats=stats,
    )


def _compute_event_stats(storage, *, tenant_id: int, hours: int,
                          filters: dict[str, Any]) -> dict[str, Any]:
    """Aggrega KPI sugli eventi nel filtro attivo (cap 2000 record).

    Ritorna dict con:
      - total_sample: numero eventi nel sample
      - top_from_domains: [(domain, n), ...]
      - top_to_recipients: [(email, n), ...]  (split anche su to_address CSV)
      - top_to_domains: [(domain, n), ...]
      - top_codcli: [(codcli, n), ...]
      - top_rules: [(rule_id, rule_name, n), ...]
      - by_action: [(action_taken, n), ...]
    """
    from collections import Counter
    sample, _ = storage.list_events(
        tenant_id=tenant_id, hours=hours,
        page=1, page_size=2000, filters=filters,
    )
    from_doms = Counter()
    to_recs = Counter()
    to_doms = Counter()
    codclis = Counter()
    rules = Counter()
    actions = Counter()
    for e in sample:
        if e.get("from_address") and "@" in e["from_address"]:
            from_doms[e["from_address"].rsplit("@", 1)[1].lower()] += 1
        ta = (e.get("to_address") or "").strip()
        if ta:
            # to_address puo' contenere CSV (vedi pipeline.to_address_internal)
            for piece in ta.split(","):
                addr = piece.strip().lower()
                if addr and "@" in addr:
                    to_recs[addr] += 1
                    to_doms[addr.rsplit("@", 1)[1]] += 1
        if e.get("codice_cliente"):
            codclis[str(e["codice_cliente"])] += 1
        if e.get("rule_id"):
            rules[int(e["rule_id"])] += 1
        if e.get("action_taken"):
            actions[str(e["action_taken"])] += 1
    # Risolvi rule_name per le top rules
    top_rules_raw = rules.most_common(8)
    rule_names: dict[int, str] = {}
    for rid, _ in top_rules_raw:
        try:
            r = storage.get_rule(rid)
            if r:
                rule_names[rid] = r.get("name") or f"rule {rid}"
        except Exception:  # noqa: BLE001
            pass
    top_rules = [(rid, rule_names.get(rid, f"rule #{rid}"), n)
                  for rid, n in top_rules_raw]
    return {
        "total_sample": len(sample),
        "sample_capped": len(sample) >= 2000,
        "top_from_domains": from_doms.most_common(8),
        "top_to_recipients": to_recs.most_common(8),
        "top_to_domains": to_doms.most_common(8),
        "top_codcli": codclis.most_common(8),
        "top_rules": top_rules,
        "by_action": actions.most_common(),
    }


def _format_short_ts(s, now: datetime | None = None) -> str:
    """Data compatta: 'HH:MM' oggi, 'DD/MM HH:MM' anno corrente, 'DD/MM/YY' altrimenti."""
    if not s:
        return "—"
    try:
        if isinstance(s, str):
            iso = s.replace("Z", "+00:00") if s.endswith("Z") else s
            dt = datetime.fromisoformat(iso.replace(" ", "T"))
        else:
            dt = s
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return str(s)[:16]
    cur = now or datetime.now(timezone.utc)
    if dt.date() == cur.date():
        return dt.strftime("%H:%M")
    if dt.year == cur.year:
        return dt.strftime("%d/%m %H:%M")
    return dt.strftime("%d/%m/%y")


@events_bp.route("/events/<int:event_id>")
@login_required()
def detail_view(event_id: int):
    evt = _storage().get_event(event_id)
    if not evt:
        abort(404)
    matched_rule = None
    if evt.get("rule_id"):
        matched_rule = _storage().get_rule(evt["rule_id"])
    # Domain diagnostic: cerca matches in addresses_from per il dominio mittente
    diag: dict[str, Any] = {"domain": None, "is_mapped": False, "matches": []}
    from_addr = (evt.get("from_address") or "").strip().lower()
    if "@" in from_addr:
        domain = from_addr.rsplit("@", 1)[-1]
        diag["domain"] = domain
        try:
            cs = current_app.extensions.get("domarc_customer_source")
            # Match via customer source (clienti con questo dominio)
            if cs:
                for c in cs.list_customers():
                    if domain in [d.lower() for d in (c.domains or [])]:
                        diag["matches"].append({"codice_cliente": c.codice_cliente, "sorgente": "customer_source"})
            # Match via addresses_from registrate
            rows = _storage().list_addresses("from", tenant_id=_tid())
            for r in rows:
                if (r.get("domain") or "").lower() == domain and r.get("codice_cliente"):
                    if not any(m["codice_cliente"] == r["codice_cliente"] for m in diag["matches"]):
                        diag["matches"].append({
                            "codice_cliente": r["codice_cliente"],
                            "sorgente": r.get("codcli_source") or "manual",
                        })
            diag["is_mapped"] = len(diag["matches"]) > 0
        except Exception:
            pass
    return render_template("admin/event_detail.html",
                           event=evt, matched_rule=matched_rule,
                           domain_diagnostic=diag)


@events_bp.route("/events/<int:event_id>/body-html")
@login_required()
def body_html_view(event_id: int):
    """Iframe sandbox con CSP restrittiva per il body HTML."""
    evt = _storage().get_event(event_id)
    if not evt:
        abort(404)
    html = evt.get("body_html")
    if not html:
        bt = evt.get("body_text") or "(body non disponibile o scaduto)"
        html = ("<!DOCTYPE html><html><body>"
                "<pre style='font-family:ui-monospace,Menlo,monospace; "
                "white-space:pre-wrap; padding:1rem; background:#fafbfd;'>"
                + str(_escape(bt)) + "</pre></body></html>")
    elif not html.lstrip().lower().startswith(("<!doctype", "<html")):
        html = ("<!DOCTYPE html><html><head><meta charset='utf-8'>"
                "<style>body{font-family:Arial; padding:1rem;} img{max-width:100%;}</style></head>"
                "<body>" + html + "</body></html>")
    resp = Response(html, mimetype="text/html; charset=utf-8")
    resp.headers["Content-Security-Policy"] = (
        "default-src 'none'; img-src data: https: http:; "
        "style-src 'unsafe-inline'; font-src data: https:; frame-ancestors 'self';"
    )
    resp.headers["X-Frame-Options"] = "SAMEORIGIN"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


@events_bp.route("/events/<int:event_id>/re-evaluate", methods=["POST"])
@login_required(role="operator")
def re_evaluate(event_id: int):
    """Ri-valuta in dry-run le regole attive contro un evento esistente.

    Ritorna catena di valutazione (regole testate, match/no-match, action), senza
    modificare nulla. Utile per debug regole e capire perché un evento ha o non
    ha matchato.
    """
    import re as _re
    evt = _storage().get_event(event_id)
    if not evt:
        return jsonify({"ok": False, "error": "evento non trovato"}), 404
    rules = _storage().list_rules(tenant_id=evt.get("tenant_id") or _tid(),
                                   only_enabled=True)
    chain: list[dict] = []
    matched_chain = []
    final_action = None
    for r in rules:
        m: dict[str, Any] = {"rule_id": r["id"], "name": r["name"], "priority": r["priority"], "action": r["action"], "matches": []}
        all_match = True
        # Subject
        for fld, src in (
            ("match_subject_regex", evt.get("subject") or ""),
            ("match_from_regex",    evt.get("from_address") or ""),
            ("match_to_regex",      evt.get("to_address") or ""),
            ("match_body_regex",    evt.get("body_text") or ""),
        ):
            patt = r.get(fld)
            if not patt:
                continue
            try:
                hit = _re.search(patt, src[:16384], _re.IGNORECASE)
            except _re.error as exc:
                m["matches"].append({"field": fld, "ok": False, "error": str(exc)})
                all_match = False
                continue
            m["matches"].append({"field": fld, "ok": bool(hit),
                                 "match": hit.group(0) if hit else None})
            if not hit: all_match = False
        # Domain
        if r.get("match_to_domain"):
            to = (evt.get("to_address") or "").lower()
            ok = to.endswith("@" + r["match_to_domain"].lower())
            m["matches"].append({"field": "match_to_domain", "ok": ok})
            if not ok: all_match = False
        m["matched"] = all_match
        chain.append(m)
        if all_match:
            matched_chain.append(r["id"])
            if not final_action:
                final_action = r["action"]
            if not r.get("continue_after_match"):
                break
    return jsonify({
        "ok": True,
        "event_id": event_id,
        "rules_evaluated": len(chain),
        "matched_rules": matched_chain,
        "final_action": final_action,
        "chain": chain,
    })


@events_bp.route("/events/export.<fmt>")
@login_required()
def export_view(fmt: str):
    if fmt not in ("csv", "json"):
        abort(404)
    try:
        hours = max(1, min(int(request.args.get("hours") or 24), 720))
    except (TypeError, ValueError):
        hours = 24
    filters = {
        "action": (request.args.get("action") or "").strip() or None,
        "q": (request.args.get("q") or "").strip() or None,
        "no_client": request.args.get("no_client") in ("on", "true", "1"),
        "no_rule": request.args.get("no_rule") in ("on", "true", "1"),
        "only_ticket": request.args.get("only_ticket") in ("on", "true", "1"),
    }
    rows, _ = _storage().list_events(tenant_id=_tid(), hours=hours,
                                      page=1, page_size=50000, filters=filters)
    fname_ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    if fmt == "json":
        return jsonify({"tenant_id": _tid(), "count": len(rows), "events": rows})
    buf = io.StringIO()
    if rows:
        writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        for r in rows:
            d = {k: ("" if v is None else
                     (json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else str(v)))
                 for k, v in r.items()}
            writer.writerow(d)
    return Response(
        buf.getvalue(), mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition":
                 f"attachment; filename=domarc-events-tenant{_tid()}-{fname_ts}.csv"},
    )
