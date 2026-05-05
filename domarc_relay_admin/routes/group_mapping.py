"""UI /group-mapping (M034): mapping campi gestionale -> gruppi cliente built-in.

Pagine:
- GET  /group-mapping/                   Lista delle rules con conteggio match
- GET  /group-mapping/new                Form nuova rule
- POST /group-mapping/new                Crea rule
- GET  /group-mapping/<id>               Edit rule
- POST /group-mapping/<id>               Update rule
- POST /group-mapping/<id>/delete        Elimina rule
- POST /group-mapping/<id>/toggle        Enable/disable
"""
from __future__ import annotations

from flask import (Blueprint, abort, current_app, flash, g, redirect,
                   render_template, request, session, url_for)

from ..auth import login_required

group_mapping_bp = Blueprint("group_mapping", __name__, url_prefix="/group-mapping")


def _storage():
    return current_app.extensions["domarc_storage"]


def _tid() -> int:
    return int(getattr(g, "current_tenant_id", 1))


def _actor() -> str:
    return session.get("username") or "?"


# ============================================================ Lista =====

@group_mapping_bp.route("/")
@login_required()
def list_view():
    storage = _storage()
    rules = storage.list_group_membership_rules(tenant_id=_tid())
    groups = storage.list_customer_groups(tenant_id=_tid())
    sources = storage.list_customer_sync_sources(tenant_id=_tid())
    auto_counts = storage.count_auto_memberships_per_group(tenant_id=_tid())
    return render_template(
        "admin/group_mapping_list.html",
        rules=rules, groups=groups, sources=sources,
        auto_counts=auto_counts,
    )


# ============================================================ New ========

@group_mapping_bp.route("/new", methods=["GET", "POST"])
@login_required(role="operator")
def new_view():
    if request.method == "POST":
        return _save(rule_id=None)
    storage = _storage()
    return render_template(
        "admin/group_mapping_form.html",
        rule=None,
        groups=storage.list_customer_groups(tenant_id=_tid()),
        sources=storage.list_customer_sync_sources(tenant_id=_tid()),
        match_types=("equals", "contains", "in_list", "regex",
                     "truthy", "falsy", "not_empty"),
    )


# ============================================================ Edit =======

@group_mapping_bp.route("/<int:rule_id>", methods=["GET", "POST"])
@login_required(role="operator")
def edit_view(rule_id: int):
    storage = _storage()
    rule = storage.get_group_membership_rule(rule_id)
    if not rule:
        abort(404)
    if request.method == "POST":
        return _save(rule_id=rule_id)
    return render_template(
        "admin/group_mapping_form.html",
        rule=rule,
        groups=storage.list_customer_groups(tenant_id=_tid()),
        sources=storage.list_customer_sync_sources(tenant_id=_tid()),
        match_types=("equals", "contains", "in_list", "regex",
                     "truthy", "falsy", "not_empty"),
    )


def _save(*, rule_id: int | None):
    form = request.form
    target_group_id = form.get("target_group_id")
    source_field = (form.get("source_field") or "").strip()
    match_type = (form.get("match_type") or "equals").strip()
    if not target_group_id or not source_field:
        flash("Campi obbligatori: gruppo target + nome campo sorgente.", "error")
        return redirect(request.referrer or url_for("group_mapping.list_view"))

    data = {
        "id": rule_id,
        "target_group_id": int(target_group_id),
        "source_field": source_field,
        "match_type": match_type,
        "match_value": form.get("match_value") or None,
        "source_id": int(form.get("source_id")) if form.get("source_id") else None,
        "priority": int(form.get("priority") or 100),
        "description": form.get("description") or None,
        "enabled": form.get("enabled") == "1",
    }
    try:
        rid = _storage().upsert_group_membership_rule(
            data, tenant_id=_tid(), actor=_actor(),
        )
        flash(f"Rule {'aggiornata' if rule_id else 'creata'} (id {rid}). "
              f"L'auto-assignment scattera' al prossimo sync della sorgente.",
              "success")
        return redirect(url_for("group_mapping.edit_view", rule_id=rid))
    except Exception as exc:  # noqa: BLE001
        flash(f"Errore: {exc}", "error")
        return redirect(request.referrer or url_for("group_mapping.list_view"))


@group_mapping_bp.route("/<int:rule_id>/delete", methods=["POST"])
@login_required(role="operator")
def delete_view(rule_id: int):
    storage = _storage()
    rule = storage.get_group_membership_rule(rule_id)
    if not rule:
        abort(404)
    storage.delete_group_membership_rule(rule_id)
    flash(f"Rule eliminata. Le membership auto-assegnate da questa rule "
          f"verranno ripulite al prossimo sync.", "success")
    return redirect(url_for("group_mapping.list_view"))


@group_mapping_bp.route("/<int:rule_id>/toggle", methods=["POST"])
@login_required(role="operator")
def toggle_view(rule_id: int):
    storage = _storage()
    rule = storage.get_group_membership_rule(rule_id)
    if not rule:
        abort(404)
    new_enabled = not rule.get("enabled")
    storage.upsert_group_membership_rule(
        {**rule, "enabled": new_enabled,
         "target_group_id": rule["target_group_id"]},
        tenant_id=_tid(),
    )
    flash(f"Rule {'abilitata' if new_enabled else 'disabilitata'}.", "success")
    return redirect(url_for("group_mapping.list_view"))
