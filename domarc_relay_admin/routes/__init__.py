"""Route blueprints dell'admin web. Per v1.0 skeleton: dashboard + health.

Le 6 macroaree UI (rules / templates / service_hours / auth_codes / aggregations /
events / tenants) saranno portate dal manager nelle settimane 5-6.
"""
from __future__ import annotations

from flask import Blueprint, current_app, jsonify, render_template, session

from ..auth import login_required


dashboard_bp = Blueprint("dashboard", __name__)
health_bp = Blueprint("health", __name__)


@dashboard_bp.route("/")
@dashboard_bp.route("/dashboard")
@login_required()
def index():
    from flask import g
    from collections import Counter
    from datetime import datetime, timedelta, timezone
    storage = current_app.extensions["domarc_storage"]
    customer_source = current_app.extensions["domarc_customer_source"]
    tid = int(getattr(g, "current_tenant_id", 1))
    # KPI scopati al tenant attivo
    rules = storage.list_rules(tenant_id=tid, only_enabled=True)
    templates = storage.list_templates(tenant_id=tid, only_enabled=True)
    events_recent, total_events = storage.list_events(tenant_id=tid, hours=24, page=1, page_size=10000)
    auth_codes = storage.list_auth_codes(tenant_id=tid, only_active=True, limit=1000)
    occurrences = storage.list_occurrences(tenant_id=tid, filter_state="active")
    occ_with_ticket = sum(1 for o in occurrences if o.get("ticket_id"))

    # Hourly series 24h
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    buckets = [(now - timedelta(hours=h)) for h in range(23, -1, -1)]
    hourly_series = []
    by_hour = {}
    by_hour_tickets = {}
    for e in events_recent:
        ra = e.get("received_at")
        if not ra: continue
        try:
            if isinstance(ra, str):
                dt = datetime.fromisoformat(ra.replace("Z", "+00:00"))
            else:
                dt = ra
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            key = dt.replace(minute=0, second=0, microsecond=0).isoformat()
        except Exception:
            continue
        by_hour[key] = by_hour.get(key, 0) + 1
        if e.get("ticket_id"):
            by_hour_tickets[key] = by_hour_tickets.get(key, 0) + 1
    for b in buckets:
        k = b.isoformat()
        hourly_series.append({"hour": k, "total": by_hour.get(k, 0), "tickets": by_hour_tickets.get(k, 0)})

    # Actions breakdown
    actions_counter = Counter((e.get("action_taken") or "default_delivery") for e in events_recent)
    actions_breakdown = sorted(actions_counter.items(), key=lambda x: -x[1])

    # Top senders/recipients
    top_senders = Counter((e.get("from_address") or "—").lower() for e in events_recent if e.get("from_address")).most_common(8)
    top_recipients = Counter((e.get("to_address") or "—").lower() for e in events_recent if e.get("to_address")).most_common(8)

    last_event_seen = events_recent[0]["received_at"] if events_recent else None

    return render_template(
        "admin/dashboard.html",
        storage_health=storage.health(),
        customer_source_health=customer_source.health(),
        version=current_app.extensions["domarc_version"],
        kpi={
            "rules": len(rules),
            "templates": len(templates),
            "events_24h": len(events_recent),
            "events_total": total_events,
            "auth_codes_active": len(auth_codes),
            "occurrences_active": len(occurrences),
            "occurrences_with_ticket": occ_with_ticket,
        },
        stats={
            "hourly_series": hourly_series,
            "actions_breakdown": actions_breakdown,
            "top_senders": top_senders,
            "top_recipients": top_recipients,
            "last_event_seen": last_event_seen,
            "aggregations_active": len(storage.list_aggregations(tenant_id=tid, only_enabled=True)) if hasattr(storage, "list_aggregations") else 0,
        },
    )


@health_bp.route("/health")
def health():
    """Endpoint pubblico no-auth — utile per loadbalancer healthcheck (D3 del piano)."""
    storage = current_app.extensions["domarc_storage"]
    h = storage.health()
    code = 200 if h.get("ok") else 503
    return jsonify({
        "status": "ok" if h.get("ok") else "error",
        "version": current_app.extensions["domarc_version"],
        "schema_version": h.get("schema_version"),
    }), code


@health_bp.route("/diagnostic")
@login_required(role="admin")
def diagnostic():
    """Endpoint admin-only — bundle completo per troubleshooting (D3)."""
    storage = current_app.extensions["domarc_storage"]
    customer_source = current_app.extensions["domarc_customer_source"]
    return jsonify({
        "version": current_app.extensions["domarc_version"],
        "storage": storage.health(),
        "customer_source": customer_source.health(),
        "session_username": session.get("username"),
    })
