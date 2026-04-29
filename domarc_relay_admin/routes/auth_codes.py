"""Authorization codes CRUD."""
from __future__ import annotations

from flask import Blueprint, current_app, flash, g, redirect, render_template, request, url_for

from ..auth import login_required

auth_codes_bp = Blueprint("auth_codes", __name__)


def _storage():
    return current_app.extensions["domarc_storage"]


def _tid() -> int:
    return int(getattr(g, "current_tenant_id", 1))


@auth_codes_bp.route("/auth-codes")
@login_required()
def list_view():
    only_active = (request.args.get("state") or "all") == "active"
    cliente = (request.args.get("cliente") or "").strip() or None
    codes = _storage().list_auth_codes(tenant_id=_tid(), only_active=only_active,
                                        codice_cliente=cliente)
    return render_template("admin/auth_codes_list.html", codes=codes,
                           only_active=only_active, cliente=cliente or "")


@auth_codes_bp.route("/auth-codes/issue", methods=["POST"])
@login_required(role="operator")
def issue_view():
    codcli = (request.form.get("codice_cliente") or "").strip().upper() or None
    note = (request.form.get("note") or "").strip() or None
    try:
        ttl_hours = int(request.form.get("ttl_hours") or 48)
    except ValueError:
        ttl_hours = 48
    try:
        result = _storage().issue_auth_code(tenant_id=_tid(), codice_cliente=codcli,
                                             rule_id=None, ttl_hours=ttl_hours, note=note)
        flash(f"Codice generato: {result['code']} (valido fino a {result['valid_until']})",
              "success")
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(url_for("auth_codes.list_view"))


@auth_codes_bp.route("/auth-codes/<int:code_id>/delete", methods=["POST"])
@login_required(role="admin")
def delete_view(code_id: int):
    _storage().delete_auth_code(code_id)
    flash("Codice eliminato.", "success")
    return redirect(url_for("auth_codes.list_view"))
