"""Dashboard /shadow (M030): vista aggregata della modalita' shadow.

Fase 1: solo recipient_groups con shadow_mode=1 (in osservazione).
Fasi successive: dominio (M031), rule_set (M032), regola singola (M033).
"""
from __future__ import annotations

from datetime import datetime, timedelta
import json

from flask import Blueprint, current_app, g, render_template

from ..auth import login_required

shadow_bp = Blueprint("shadow", __name__, url_prefix="/shadow")


def _storage():
    return current_app.extensions["domarc_storage"]


def _tid() -> int:
    return int(getattr(g, "current_tenant_id", 1))


@shadow_bp.route("/")
@login_required()
def dashboard():
    storage = _storage()
    shadow_groups = storage.list_shadow_recipient_groups(tenant_id=_tid())
    shadow_domains = storage.list_shadow_domains(tenant_id=_tid())
    shadow_rules = storage.list_shadow_rules(tenant_id=_tid())

    # Conta eventi shadow ultime 24h e 7gg
    n_24h = 0
    n_7d = 0
    actions_24h: dict[str, int] = {}
    recent_events = []
    try:
        evts_7d, _ = storage.list_events(
            tenant_id=_tid(), hours=7 * 24,
            page=1, page_size=500,
            filters={"only_shadow": True},
        )
        cutoff_24h = datetime.utcnow() - timedelta(hours=24)
        for e in evts_7d:
            n_7d += 1
            try:
                rcv = e.get("received_at") or ""
                rcv_dt = datetime.fromisoformat(rcv.replace("Z", "+00:00").split("+")[0])
                if rcv_dt >= cutoff_24h:
                    n_24h += 1
                    pm = e.get("payload_metadata") or {}
                    if isinstance(pm, str):
                        try:
                            pm = json.loads(pm)
                        except (TypeError, ValueError):
                            pm = {}
                    whe = (pm or {}).get("would_have_executed") or {}
                    act = whe.get("action") or "default_delivery"
                    actions_24h[act] = actions_24h.get(act, 0) + 1
            except (ValueError, TypeError, KeyError):
                pass
        recent_events = evts_7d[:20]
    except Exception:  # noqa: BLE001
        pass

    actions_24h_sorted = sorted(actions_24h.items(), key=lambda x: -x[1])

    return render_template(
        "admin/shadow_dashboard.html",
        shadow_groups=shadow_groups,
        shadow_domains=shadow_domains,
        shadow_rules=shadow_rules,
        n_24h=n_24h,
        n_7d=n_7d,
        actions_24h=actions_24h_sorted,
        recent_events=recent_events,
    )
