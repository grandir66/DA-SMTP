"""Gestione gruppi clienti — anagrafica + assegnazione membri.

Pattern: i gruppi sono raggruppamenti logici (es. "Top Customer", "Settore
Sanità") usati come criterio di match nelle regole. Un cliente può
appartenere a più gruppi contemporaneamente.

Pagine:
- ``GET  /customer-groups``                 lista gruppi con conteggi.
- ``GET  /customer-groups/new``             form nuovo gruppo.
- ``POST /customer-groups/new``             crea gruppo.
- ``GET  /customer-groups/<id>``            dettaglio + form edit + lista membri.
- ``POST /customer-groups/<id>``            update gruppo + bulk assign membri.
- ``POST /customer-groups/<id>/delete``     elimina gruppo (e membership).
"""
from __future__ import annotations

from flask import (Blueprint, abort, current_app, flash, g, redirect,
                   render_template, request, session, url_for)

from ..auth import login_required

customer_groups_bp = Blueprint("customer_groups", __name__,
                                url_prefix="/customer-groups")


def _storage():
    return current_app.extensions["domarc_storage"]


def _customer_source():
    return current_app.extensions["domarc_customer_source"]


def _tid() -> int:
    return int(getattr(g, "current_tenant_id", 1))


def _actor() -> str:
    return session.get("username") or "?"


@customer_groups_bp.route("/")
@login_required()
def list_view():
    storage = _storage()
    groups = storage.list_customer_groups(tenant_id=_tid())
    return render_template("admin/customer_groups_list.html", groups=groups)


@customer_groups_bp.route("/new", methods=["GET", "POST"])
@login_required(role="operator")
def new_view():
    if request.method == "POST":
        try:
            gid = _storage().upsert_customer_group(
                tenant_id=_tid(),
                code=request.form.get("code", ""),
                name=request.form.get("name", ""),
                description=request.form.get("description") or None,
                color=request.form.get("color") or None,
                enabled=request.form.get("enabled") == "1",
                actor=_actor(),
            )
            flash(f"✓ Gruppo creato (id {gid}).", "success")
            return redirect(url_for("customer_groups.detail_view", group_id=gid))
        except ValueError as exc:
            flash(f"Errore: {exc}", "error")
    return render_template("admin/customer_group_form.html",
                            group=None,
                            members=[],
                            available_customers=_customer_source().list_customers())


@customer_groups_bp.route("/<int:group_id>", methods=["GET", "POST"])
@login_required(role="operator")
def detail_view(group_id: int):
    storage = _storage()
    group = storage.get_customer_group(group_id)
    if not group or group["tenant_id"] != _tid():
        abort(404)

    if request.method == "POST":
        # Update gruppo + membri in un solo POST
        try:
            storage.upsert_customer_group(
                group_id=group_id, tenant_id=_tid(),
                code=request.form.get("code", group["code"]),
                name=request.form.get("name", group["name"]),
                description=request.form.get("description") or None,
                color=request.form.get("color") or None,
                enabled=request.form.get("enabled") == "1",
                actor=_actor(),
            )
            # Assegnazione membri (lista codcli da multi-select)
            new_members = request.form.getlist("members")
            with storage.transaction() as conn:
                conn.execute(
                    "DELETE FROM customer_group_members WHERE group_id = ?",
                    (group_id,),
                )
                for codcli in new_members:
                    codcli = (codcli or "").strip()
                    if not codcli:
                        continue
                    conn.execute(
                        """INSERT OR IGNORE INTO customer_group_members
                               (tenant_id, group_id, codice_cliente, added_by)
                           VALUES (?, ?, ?, ?)""",
                        (_tid(), group_id, codcli, _actor()),
                    )
            flash(f"✓ Gruppo aggiornato ({len(new_members)} membri).", "success")
            return redirect(url_for("customer_groups.detail_view", group_id=group_id))
        except ValueError as exc:
            flash(f"Errore: {exc}", "error")

    members = storage.list_group_members(group_id)
    member_codcli = {m["codice_cliente"] for m in members}
    customers = _customer_source().list_customers()
    return render_template("admin/customer_group_form.html",
                            group=group,
                            members=members,
                            member_codcli=member_codcli,
                            available_customers=customers)


@customer_groups_bp.route("/<int:group_id>/delete", methods=["POST"])
@login_required(role="admin")
def delete_view(group_id: int):
    storage = _storage()
    group = storage.get_customer_group(group_id)
    if not group or group["tenant_id"] != _tid():
        abort(404)
    storage.delete_customer_group(group_id)
    flash(f"✓ Gruppo «{group['name']}» eliminato.", "success")
    return redirect(url_for("customer_groups.list_view"))
