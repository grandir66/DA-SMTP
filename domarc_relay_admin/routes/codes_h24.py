"""Pagina panoramica unificata Codici H24.

Mostra i KPI consolidati delle tre viste (monouso, permanenti, mailbox di
rientro) con tab per navigare. Le pagine specifiche restano accessibili
direttamente — questa è una landing per chi vuole l'overview a colpo d'occhio.

URL:
    /codes-h24/    panoramica con KPI + 3 tab (monouso, permanenti, mailbox)
"""
from __future__ import annotations

from flask import Blueprint, current_app, g, render_template

from ..auth import login_required


codes_h24_bp = Blueprint("codes_h24", __name__, url_prefix="/codes-h24")


def _storage():
    return current_app.extensions["domarc_storage"]


def _tid() -> int:
    return int(getattr(g, "current_tenant_id", 1))


@codes_h24_bp.route("/")
@login_required()
def index():
    storage = _storage()
    tid = _tid()

    # Stats codici monouso (auth_codes)
    try:
        oneshots = storage.list_auth_codes(tenant_id=tid, only_active=False)
    except Exception:  # noqa: BLE001
        oneshots = []
    by_state = {"pending": 0, "accepted": 0, "expired": 0, "canceled": 0, "other": 0}
    for c in oneshots:
        st = (c.get("state") or "").lower()
        by_state[st if st in by_state else "other"] += 1

    # Stats codici permanenti (h24_codes)
    try:
        permanents = storage.list_h24_codes(tenant_id=tid, only_active=False, limit=1000)
    except Exception:  # noqa: BLE001
        permanents = []
    perm_active = sum(1 for c in permanents
                      if c.get("enabled") and not c.get("revoked_at"))
    perm_revoked = sum(1 for c in permanents if c.get("revoked_at"))
    perm_total_uses = sum(int(c.get("used_count") or 0) for c in permanents)

    # Stats mailbox di rientro
    try:
        targets = storage.list_h24_targets(tenant_id=tid)
    except Exception:  # noqa: BLE001
        targets = []
    targets_active = sum(1 for t in targets if t.get("enabled"))
    distinct_aliases = len({t.get("h24_alias") for t in targets if t.get("h24_alias")})

    return render_template(
        "admin/codes_h24_overview.html",
        oneshots_total=len(oneshots),
        oneshots_pending=by_state["pending"],
        oneshots_accepted=by_state["accepted"],
        oneshots_expired=by_state["expired"],
        oneshots=oneshots[:10],  # ultimi 10 per preview
        perm_total=len(permanents),
        perm_active=perm_active,
        perm_revoked=perm_revoked,
        perm_total_uses=perm_total_uses,
        permanents=permanents[:10],
        targets_total=len(targets),
        targets_active=targets_active,
        distinct_aliases=distinct_aliases,
        targets=targets[:10],
    )
