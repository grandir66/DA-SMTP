"""Events list + detail (con body iframe se retention attiva)."""
from __future__ import annotations

import csv
import io
import json
from datetime import datetime
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
    }
    events, total = _storage().list_events(
        tenant_id=_tid(), hours=hours,
        page=page, page_size=page_size, filters=filters,
    )
    pages = max(1, (total + page_size - 1) // page_size)
    available_actions = ["create_ticket", "auto_reply", "forward", "redirect",
                         "quarantine", "flag_only", "ignore", "default_delivery"]
    return render_template(
        "admin/events_list.html",
        events=events, total=total, page=page, page_size=page_size, pages=pages,
        available_actions=available_actions,
        filters={**filters, "hours": hours, "q": request.args.get("q") or ""},
    )


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
    fname_ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
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
