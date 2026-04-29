"""Multi-tenant context middleware + CRUD blueprint.

g.current_tenant_id viene risolto da:
  1. ?tenant_id=N (query string, persistito in session)
  2. session['relay_tenant_id']
  3. Default = 1 (DOMARC)

Il context_processor inietta tenant_ctx in tutti i template:
  - tenants: lista per dropdown
  - current_tenant: dict del tenant attivo
  - tenants_count: int
  - is_single_tenant: bool (count <= 1)
"""
from __future__ import annotations

import logging
from typing import Any

from flask import Blueprint, abort, current_app, flash, g, redirect, render_template, request, session, url_for

from ..auth import login_required

logger = logging.getLogger(__name__)


tenants_bp = Blueprint("tenants", __name__)


def _storage():
    return current_app.extensions["domarc_storage"]


def _is_superadmin() -> bool:
    return (session.get("role") or "") == "superadmin"


def current_tenant_id() -> int:
    """Determina il tenant attivo per la request corrente.

    Per **superadmin**: legge query string → session → default 1 (può switchare liberamente).
    Per altri ruoli: imposto al `users.tenant_id` dell'utente (immutabile),
                     ignorando query string e session per sicurezza.
    """
    # Non-superadmin sono bloccati al proprio tenant
    if not _is_superadmin():
        user_tid = session.get("user_tenant_id")
        if user_tid:
            try:
                return int(user_tid)
            except (TypeError, ValueError):
                pass
        return 1  # fallback safe

    # Superadmin: switch via query string o session
    qs = request.args.get("tenant_id") if request else None
    if qs:
        try:
            tid = int(qs)
            if _storage().get_tenant(tid):
                session["relay_tenant_id"] = tid
                return tid
        except (TypeError, ValueError):
            pass
    sess_tid = session.get("relay_tenant_id")
    if sess_tid:
        try:
            tid = int(sess_tid)
            if _storage().get_tenant(tid):
                return tid
        except (TypeError, ValueError):
            pass
    return 1  # default DOMARC


def tenant_context() -> dict[str, Any]:
    storage = _storage()
    tenants = storage.list_tenants(only_enabled=True)
    cur_id = current_tenant_id()
    cur = next((t for t in tenants if t["id"] == cur_id), None)
    return {
        "current_tenant_id": cur_id,
        "current_tenant": cur,
        "tenants": tenants,
        "tenants_count": len(tenants),
        "is_single_tenant": len(tenants) <= 1,
        "can_switch_tenant": _is_superadmin(),
    }


def register_tenant_middleware(app) -> None:
    """Registra middleware before_request + context_processor sulla Flask app."""

    @app.before_request
    def _set_tenant_id() -> None:
        if not session.get("user_id"):
            return
        try:
            g.current_tenant_id = current_tenant_id()
        except Exception:  # noqa: BLE001
            g.current_tenant_id = 1

    @app.context_processor
    def _inject_tenant_ctx() -> dict[str, Any]:
        if session.get("user_id"):
            try:
                return {"tenant_ctx": tenant_context()}
            except Exception:  # noqa: BLE001
                return {"tenant_ctx": None}
        return {"tenant_ctx": None}


# ============================================================ ROUTES ===

@tenants_bp.route("/tenants")
@login_required(role="tech")
def list_view():
    state = (request.args.get("state") or "all").lower()
    only_enabled = True if state == "enabled" else (False if state == "disabled" else None)
    tenants = _storage().list_tenants(only_enabled=only_enabled,
                                       search=(request.args.get("q") or None))
    # Scoping non-superadmin: filtra al solo tenant dell'utente
    if (session.get("role") or "") != "superadmin":
        my_tid = session.get("user_tenant_id") or 1
        tenants = [t for t in tenants if t["id"] == my_tid]
    return render_template(
        "admin/tenants_list.html",
        tenants=tenants,
        filter_state=state,
        search=request.args.get("q") or "",
    )


@tenants_bp.route("/tenants/new", methods=["GET", "POST"])
@tenants_bp.route("/tenants/<int:tenant_id>", methods=["GET", "POST"])
@login_required(role="admin")
def form_view(tenant_id: int | None = None):
    is_new = tenant_id is None
    record: dict = {}
    if not is_new:
        record = _storage().get_tenant(tenant_id) or {}
        if not record:
            flash("Tenant non trovato", "error")
            return redirect(url_for("tenants.list_view"))

    if request.method == "POST":
        data = {
            "id": tenant_id if not is_new else None,
            "codice": request.form.get("codice"),
            "ragione_sociale": request.form.get("ragione_sociale"),
            "description": request.form.get("description"),
            "contract_active": request.form.get("contract_active") in ("on", "true", "1"),
            "enabled": request.form.get("enabled") in ("on", "true", "1"),
            "created_by": session.get("username") or "ui",
        }
        try:
            new_id = _storage().upsert_tenant(data)
            flash(f"Tenant {'creato' if is_new else 'aggiornato'} (id={new_id}).", "success")
            return redirect(url_for("tenants.form_view", tenant_id=new_id))
        except ValueError as exc:
            flash(str(exc), "error")
            record = {**record, **data}

    return render_template(
        "admin/tenant_form.html",
        is_new=is_new,
        record=record,
        is_default=(tenant_id == 1 if tenant_id else False),
    )


@tenants_bp.route("/tenants/<int:tenant_id>/delete", methods=["POST"])
@login_required(role="admin")
def delete_view(tenant_id: int):
    try:
        _storage().delete_tenant(tenant_id)
        flash("Tenant eliminato.", "success")
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(url_for("tenants.list_view"))


@tenants_bp.route("/tenants/switch/<int:tenant_id>", methods=["GET", "POST"])
@login_required()
def switch_view(tenant_id: int):
    if _storage().get_tenant(tenant_id):
        session["relay_tenant_id"] = int(tenant_id)
    return redirect(request.referrer or url_for("dashboard.index"))


@tenants_bp.route("/tenants/overview")
@login_required(role="admin")
def overview_view():
    """Overview multi-tenant: per ogni tenant, KPI di rules/templates/events/quote."""
    storage = _storage()
    tenants = storage.list_tenants()
    overview = []
    for t in tenants:
        tid = t["id"]
        rules = storage.list_rules(tenant_id=tid, only_enabled=True)
        all_rules = storage.list_rules(tenant_id=tid)
        templates_list = storage.list_templates(tenant_id=tid, only_enabled=True)
        _, total_events = storage.list_events(tenant_id=tid, hours=24, page=1, page_size=1)
        codes = storage.list_auth_codes(tenant_id=tid, only_active=True, limit=1000)
        overview.append({
            **t,
            "rules_count": len(all_rules),
            "rules_enabled": len(rules),
            "templates_count": len(templates_list),
            "events_24h": total_events,
            "auth_codes_active": len(codes),
        })
    return render_template("admin/tenants_overview.html", overview=overview)
