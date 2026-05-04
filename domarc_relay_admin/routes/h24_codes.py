"""H24 codici PERMANENTI cliente — CRUD + UI admin.

Pagine:
- GET  /h24-codes                       lista codici permanenti (filtri)
- POST /h24-codes/create                crea nuovo codice (modale)
- GET  /h24-codes/<id>/usages           storico utilizzi
- POST /h24-codes/<id>/revoke           revoca con motivo
"""
from __future__ import annotations

from flask import (Blueprint, abort, current_app, flash, g, redirect,
                    render_template, request, session, url_for)

from ..auth import login_required

h24_codes_bp = Blueprint("h24_codes", __name__)


def _storage():
    return current_app.extensions["domarc_storage"]


def _customer_source():
    return current_app.extensions.get("domarc_customer_source")


def _tid() -> int:
    return int(getattr(g, "current_tenant_id", 1))


def _actor() -> str:
    return session.get("username") or "?"


# ------------------------------------------------------------------ Codici --

@h24_codes_bp.route("/h24-codes")
@login_required()
def list_view():
    only_active = (request.args.get("state") or "all") == "active"
    cliente = (request.args.get("cliente") or "").strip().upper() or None
    codes = _storage().list_h24_codes(
        tenant_id=_tid(), codice_cliente=cliente, only_active=only_active,
    )
    return render_template(
        "admin/h24_codes_list.html",
        codes=codes,
        only_active=only_active,
        cliente=cliente or "",
    )


@h24_codes_bp.route("/h24-codes/create", methods=["POST"])
@login_required(role="operator")
def create_view():
    codcli = (request.form.get("codice_cliente") or "").strip().upper()
    label = (request.form.get("label") or "").strip() or None
    custom_code = (request.form.get("code") or "").strip().upper() or None
    note = (request.form.get("note") or "").strip() or None
    if not codcli:
        flash("Codice cliente obbligatorio.", "error")
        return redirect(url_for("h24_codes.list_view"))
    if custom_code:
        # Validazione minima del codice custom
        import re
        if not re.match(r"^[A-Z0-9][A-Z0-9-]{4,38}[A-Z0-9]$", custom_code):
            flash("Codice custom non valido (atteso: A-Z 0-9 -, lunghezza 6-40).",
                   "error")
            return redirect(url_for("h24_codes.list_view"))
    try:
        prefix = _storage().get_setting("h24.permanent_code_prefix") or "H24-"
        result = _storage().create_h24_code(
            tenant_id=_tid(),
            codice_cliente=codcli,
            label=label,
            code=custom_code,
            prefix=prefix,
            created_by=_actor(),
            note=note,
        )
        flash(f"✓ Codice creato: {result['code']} per cliente {codcli}.",
              "success")
    except ValueError as exc:
        flash(f"Errore: {exc}", "error")
    return redirect(url_for("h24_codes.list_view"))


@h24_codes_bp.route("/h24-codes/<int:code_id>/usages")
@login_required()
def usages_view(code_id: int):
    code = _storage().get_h24_code(code_id)
    if not code or code["tenant_id"] != _tid():
        abort(404)
    usages = _storage().get_h24_code_usages(code_id, limit=500)
    return render_template(
        "admin/h24_code_usages.html",
        code=code,
        usages=usages,
    )


@h24_codes_bp.route("/h24-codes/<int:code_id>/revoke", methods=["POST"])
@login_required(role="admin")
def revoke_view(code_id: int):
    code = _storage().get_h24_code(code_id)
    if not code or code["tenant_id"] != _tid():
        abort(404)
    reason = (request.form.get("reason") or "").strip() or None
    ok = _storage().revoke_h24_code(code_id, revoked_by=_actor(), reason=reason)
    if ok:
        flash(f"✓ Codice {code['code']} revocato.", "success")
    else:
        flash("Codice già revocato (o non trovato).", "warning")
    return redirect(url_for("h24_codes.list_view"))


# ------------------------------------------------------------------ Targets --

@h24_codes_bp.route("/h24-targets")
@login_required()
def targets_list_view():
    targets = _storage().list_h24_targets(tenant_id=_tid())
    fallback = _storage().get_setting("h24.default_inbound_alias") or ""
    return render_template(
        "admin/h24_targets_list.html",
        targets=targets,
        fallback_alias=fallback,
    )


@h24_codes_bp.route("/h24-targets/upsert", methods=["POST"])
@login_required(role="admin")
def targets_upsert_view():
    target_id = request.form.get("id")
    source_domain = (request.form.get("source_domain") or "").strip().lower()
    h24_alias = (request.form.get("h24_alias") or "").strip().lower()
    fee = request.form.get("urgent_fee_eur")
    note = (request.form.get("note") or "").strip() or None
    enabled = request.form.get("enabled") == "1"
    try:
        fee_int = int(fee) if fee and str(fee).strip() else None
    except ValueError:
        fee_int = None
    try:
        tid = _storage().upsert_h24_target(
            tenant_id=_tid(),
            target_id=int(target_id) if target_id else None,
            source_domain=source_domain,
            h24_alias=h24_alias,
            urgent_fee_eur=fee_int,
            note=note,
            enabled=enabled,
        )
        flash(f"✓ Target salvato (id {tid}): {source_domain} → {h24_alias}.",
              "success")
    except ValueError as exc:
        flash(f"Errore: {exc}", "error")
    return redirect(url_for("h24_codes.targets_list_view"))


@h24_codes_bp.route("/h24-targets/<int:target_id>/delete", methods=["POST"])
@login_required(role="admin")
def targets_delete_view(target_id: int):
    _storage().delete_h24_target(target_id)
    flash("Target eliminato.", "success")
    return redirect(url_for("h24_codes.targets_list_view"))
