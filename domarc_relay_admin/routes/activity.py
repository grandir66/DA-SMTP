"""Activity log realtime — flusso mail + analisi regole + decisioni IA in tempo reale.

Architettura:

- ``GET /activity`` — pagina HTML con UI di tail live (auto-refresh JS polling).
- ``GET /activity/stream?since_id=N`` — JSON con eventi nuovi (id > N).
- ``GET /activity/stream?since_id=N&format=sse`` — Server-Sent Events.

L'UI fa polling ogni 2s sull'endpoint stream incrementale (only-new), così
non ricarica la pagina e non transferisce dati duplicati. Per i sistemi con
volumi alti si può passare a SSE estendibile con ``Last-Event-ID``.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from flask import Blueprint, Response, current_app, g, jsonify, render_template, request

from ..auth import login_required

activity_bp = Blueprint("activity", __name__, url_prefix="/activity")


def _storage():
    return current_app.extensions["domarc_storage"]


def _tid() -> int:
    return int(getattr(g, "current_tenant_id", 1))


def _safe_int(value, default: int, *, min_val: int = 0,
              max_val: int = 10**12) -> int:
    try:
        v = int(value) if value not in (None, "") else default
    except (TypeError, ValueError):
        v = default
    if v < min_val:
        v = min_val
    if v > max_val:
        v = max_val
    return v


@activity_bp.route("/")
@login_required()
def view():
    """Pagina UI activity log."""
    return render_template("admin/activity_live.html")


@activity_bp.route("/stream")
@login_required()
def stream():
    """Stream incrementale eventi (mail + decisioni IA + cluster updates).

    Parametri query:
    - ``since_event_id``: ritorna solo eventi con id > N (default 0 = tutti recenti).
    - ``since_decision_id``: idem per ai_decisions.
    - ``since_cluster_id``: idem per ai_error_clusters.
    - ``limit``: max righe per stream (default 50, max 500).

    Risposta JSON:
    ```
    {
        "events": [...],
        "decisions": [...],
        "clusters": [...],
        "max_event_id": N, "max_decision_id": N, "max_cluster_id": N,
        "ts": "2026-..."
    }
    ```

    Il client UI tiene traccia degli ultimi id ricevuti e li manda nel
    polling successivo per ottenere SOLO il delta.
    """
    storage = _storage()
    tid = _tid()
    since_event = _safe_int(request.args.get("since_event_id"), 0)
    since_decision = _safe_int(request.args.get("since_decision_id"), 0)
    since_cluster = _safe_int(request.args.get("since_cluster_id"), 0)
    limit = _safe_int(request.args.get("limit"), 50, min_val=1, max_val=500)

    # === Events nuovi (mail processate) ===
    new_events: list[dict[str, Any]] = []
    try:
        events_recent, _ = storage.list_events(
            tenant_id=tid, hours=24, page=1, page_size=limit * 4,
        )
        # Filtra per id > since
        for e in events_recent:
            eid = int(e.get("id") or 0)
            if eid <= since_event:
                continue
            pm = e.get("payload_metadata") or {}
            if not isinstance(pm, dict):
                pm = {}
            new_events.append({
                "id": eid,
                "received_at": e.get("received_at"),
                "from_address": e.get("from_address"),
                "to_address": e.get("to_address"),
                "subject": (e.get("subject") or "")[:120],
                "action_taken": e.get("action_taken"),
                "rule_id": e.get("rule_id"),
                "ai_decision_id": pm.get("ai_decision_id"),
                "ai_classification": pm.get("ai_classification"),
                "ai_urgenza": pm.get("ai_urgenza"),
                "ai_unavailable": pm.get("ai_unavailable"),
                "privacy_bypass": pm.get("privacy_bypass"),
            })
            if len(new_events) >= limit:
                break
        # Ordino DESC per timestamp
        new_events.sort(key=lambda x: x["id"], reverse=True)
    except Exception as exc:  # noqa: BLE001
        current_app.logger.warning("activity stream events: %s", exc)

    # === Decisioni IA nuove ===
    new_decisions: list[dict[str, Any]] = []
    try:
        decisions = storage.list_ai_decisions(tenant_id=tid, hours=24, limit=limit * 2)
        for d in decisions:
            did = int(d.get("id") or 0)
            if did <= since_decision:
                continue
            new_decisions.append({
                "id": did,
                "created_at": d.get("created_at"),
                "job_code": d.get("job_code"),
                "intent": d.get("intent"),
                "urgenza": d.get("urgenza_proposta"),
                "summary": (d.get("summary") or "")[:80],
                "model": d.get("model"),
                "latency_ms": d.get("latency_ms"),
                "cost_usd": d.get("cost_usd"),
                "shadow_mode": bool(d.get("shadow_mode")),
                "applied": bool(d.get("applied")),
                "error": d.get("error"),
                "event_uuid": d.get("event_uuid"),
            })
            if len(new_decisions) >= limit:
                break
    except Exception as exc:  # noqa: BLE001
        current_app.logger.warning("activity stream decisions: %s", exc)

    # === Cluster errori — solo cambi di stato/count recenti ===
    cluster_updates: list[dict[str, Any]] = []
    try:
        clusters = storage.list_ai_error_clusters(tenant_id=tid, limit=limit)
        for c in clusters:
            cid = int(c.get("id") or 0)
            if cid <= since_cluster:
                continue
            cluster_updates.append({
                "id": cid,
                "subject": (c.get("representative_subject") or "")[:80],
                "count": c.get("count"),
                "state": c.get("state"),
                "last_seen": c.get("last_seen"),
                "threshold": c.get("manual_threshold"),
            })
            if len(cluster_updates) >= limit:
                break
    except Exception as exc:  # noqa: BLE001
        current_app.logger.warning("activity stream clusters: %s", exc)

    return jsonify({
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "events": new_events,
        "decisions": new_decisions,
        "clusters": cluster_updates,
        "max_event_id": max([e["id"] for e in new_events] + [since_event]),
        "max_decision_id": max([d["id"] for d in new_decisions] + [since_decision]),
        "max_cluster_id": max([c["id"] for c in cluster_updates] + [since_cluster]),
        "counts": {
            "events": len(new_events),
            "decisions": len(new_decisions),
            "clusters": len(cluster_updates),
        },
    })
