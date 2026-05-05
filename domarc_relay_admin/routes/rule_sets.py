"""UI rule_sets (M029): set di regole organizzati per profilo orario.

Pagine:
- GET  /rule-sets/                  Lista set con conteggio regole + stato
- GET  /rule-sets/new               Form nuovo set custom
- POST /rule-sets/new               Crea set
- GET  /rule-sets/<id>              Form edit set
- POST /rule-sets/<id>              Update set
- POST /rule-sets/<id>/delete       Elimina set custom (regole spostate in 'globali')
- POST /rule-sets/<id>/toggle       Enable/disable
"""
from __future__ import annotations

from flask import (Blueprint, abort, current_app, flash, g, redirect,
                   render_template, request, session, url_for)

from ..auth import login_required

rule_sets_bp = Blueprint("rule_sets", __name__, url_prefix="/rule-sets")


def _storage():
    return current_app.extensions["domarc_storage"]


def _tid() -> int:
    return int(getattr(g, "current_tenant_id", 1))


# ============================================================ Lista =====

@rule_sets_bp.route("/")
@login_required()
def list_view():
    storage = _storage()
    sets = storage.list_rule_sets(tenant_id=_tid())
    counts = storage.count_rules_per_set(tenant_id=_tid())
    return render_template(
        "admin/rule_sets_list.html",
        sets=sets, counts=counts,
    )


# ============================================================ New ========

@rule_sets_bp.route("/new", methods=["GET", "POST"])
@login_required(role="operator")
def new_view():
    if request.method == "POST":
        return _save(set_id=None)
    return render_template("admin/rule_sets_form.html",
                           rs=None, profile_codes=("STD", "EXT", "H24", "NO"))


# ============================================================ Edit =======

@rule_sets_bp.route("/<int:set_id>", methods=["GET", "POST"])
@login_required(role="operator")
def edit_view(set_id: int):
    storage = _storage()
    rs = storage.get_rule_set(set_id)
    if not rs:
        abort(404)
    if request.method == "POST":
        return _save(set_id=set_id)
    return render_template("admin/rule_sets_form.html",
                           rs=rs, profile_codes=("STD", "EXT", "H24", "NO"))


def _save(*, set_id: int | None):
    form = request.form
    name = (form.get("name") or "").strip()
    if not name:
        flash("Nome obbligatorio.", "error")
        return redirect(request.referrer or url_for("rule_sets.list_view"))

    data = {
        "id": set_id,
        "name": name,
        "code": (form.get("code") or "").strip().lower() or None,
        "description": form.get("description") or None,
        "is_always_active": form.get("is_always_active") == "1",
        "profile_code": (form.get("profile_code") or "").strip().upper() or None,
        "evaluation_order": int(form.get("evaluation_order") or 100),
        "color": form.get("color") or None,
        "enabled": form.get("enabled") == "1",
    }
    try:
        sid = _storage().upsert_rule_set(data, tenant_id=_tid())
        flash(f"Set {'aggiornato' if set_id else 'creato'} (id {sid}).", "success")
        return redirect(url_for("rule_sets.edit_view", set_id=sid))
    except Exception as exc:  # noqa: BLE001
        flash(f"Errore: {exc}", "error")
        return redirect(request.referrer or url_for("rule_sets.list_view"))


# ============================================================ Delete =====

@rule_sets_bp.route("/<int:set_id>/delete", methods=["POST"])
@login_required(role="operator")
def delete_view(set_id: int):
    storage = _storage()
    rs = storage.get_rule_set(set_id)
    if not rs:
        abort(404)
    if rs.get("is_builtin"):
        flash("I set built-in non possono essere eliminati.", "error")
        return redirect(url_for("rule_sets.edit_view", set_id=set_id))
    try:
        storage.delete_rule_set(set_id)
        flash(f"Set '{rs['name']}' eliminato. Le regole sono state spostate "
              f"nel set 'globali'.", "success")
    except Exception as exc:  # noqa: BLE001
        flash(f"Errore eliminazione: {exc}", "error")
    return redirect(url_for("rule_sets.list_view"))


@rule_sets_bp.route("/<int:set_id>/toggle", methods=["POST"])
@login_required(role="operator")
def toggle_view(set_id: int):
    storage = _storage()
    rs = storage.get_rule_set(set_id)
    if not rs:
        abort(404)
    new_enabled = not rs.get("enabled")
    storage.upsert_rule_set({"id": set_id, **rs, "enabled": new_enabled},
                             tenant_id=_tid())
    flash(f"Set '{rs['name']}' {'abilitato' if new_enabled else 'disabilitato'}.",
          "success")
    return redirect(url_for("rule_sets.list_view"))
