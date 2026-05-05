"""Gruppi destinatari — anagrafica + assegnazione membri.

Pattern gemello a customer_groups.py: i gruppi destinatari sono raggruppamenti
logici di indirizzi email usati come criterio di match nelle regole
(`match_to_group_id`) o come target di forward (`forward_to_group_id`).

Pagine:
- ``GET  /recipient-groups/``               lista gruppi
- ``GET  /recipient-groups/new``            form nuovo gruppo
- ``POST /recipient-groups/new``            crea gruppo
- ``GET  /recipient-groups/<id>``           dettaglio + form edit + lista membri
- ``POST /recipient-groups/<id>``           update gruppo + bulk assign membri
- ``POST /recipient-groups/<id>/delete``    elimina gruppo (e membership)

Note:
- L'aggiunta in massa avviene tipicamente da `/addresses-to` (bulk action).
  Qui solo il form per la gestione del gruppo + aggiunta libera (textarea)
  + rimozione singoli membri.
"""
from __future__ import annotations

from flask import (Blueprint, abort, current_app, flash, g, redirect,
                   render_template, request, session, url_for)

from ..auth import login_required


recipient_groups_bp = Blueprint("recipient_groups", __name__,
                                  url_prefix="/recipient-groups")


def _storage():
    return current_app.extensions["domarc_storage"]


def _tid() -> int:
    return int(getattr(g, "current_tenant_id", 1))


def _actor() -> str:
    return session.get("username") or "?"


@recipient_groups_bp.route("/")
@login_required()
def list_view():
    storage = _storage()
    groups = storage.list_recipient_groups(tenant_id=_tid())
    return render_template("admin/recipient_groups_list.html", groups=groups)


@recipient_groups_bp.route("/new", methods=["GET", "POST"])
@login_required(role="operator")
def new_view():
    if request.method == "POST":
        try:
            gid = _storage().upsert_recipient_group(
                tenant_id=_tid(),
                code=request.form.get("code", ""),
                name=request.form.get("name", ""),
                description=request.form.get("description") or None,
                color=request.form.get("color") or None,
                enabled=request.form.get("enabled") == "1",
                shadow_mode=request.form.get("shadow_mode") == "1",
                shadow_note=request.form.get("shadow_note") or None,
                actor=_actor(),
            )
            flash(f"✓ Gruppo destinatari creato (id {gid}).", "success")
            return redirect(url_for("recipient_groups.detail_view",
                                       group_id=gid))
        except ValueError as exc:
            flash(f"Errore: {exc}", "error")
    return render_template("admin/recipient_group_form.html",
                            group=None, members=[])


@recipient_groups_bp.route("/<int:group_id>", methods=["GET", "POST"])
@login_required(role="operator")
def detail_view(group_id: int):
    storage = _storage()
    group = storage.get_recipient_group(group_id)
    if not group or group["tenant_id"] != _tid():
        abort(404)

    if request.method == "POST":
        try:
            storage.upsert_recipient_group(
                group_id=group_id, tenant_id=_tid(),
                code=request.form.get("code", group["code"]),
                name=request.form.get("name", group["name"]),
                description=request.form.get("description") or None,
                color=request.form.get("color") or None,
                enabled=request.form.get("enabled") == "1",
                shadow_mode=request.form.get("shadow_mode") == "1",
                shadow_note=request.form.get("shadow_note") or None,
                actor=_actor(),
            )
            # Membri: checkbox dei membri esistenti + textarea per aggiunta libera
            kept = list(request.form.getlist("members"))
            raw = (request.form.get("members_raw") or "").strip()
            if raw:
                import re
                for tok in re.split(r"[\s,;]+", raw):
                    tok = tok.strip()
                    if tok and "@" in tok:
                        kept.append(tok)
            n = storage.replace_recipient_group_members(
                group_id, kept, tenant_id=_tid(), actor=_actor(),
            )
            flash(f"✓ Gruppo aggiornato ({n} membri).", "success")
            return redirect(url_for("recipient_groups.detail_view",
                                       group_id=group_id))
        except ValueError as exc:
            flash(f"Errore: {exc}", "error")

    members = storage.list_recipient_group_members(group_id)
    return render_template("admin/recipient_group_form.html",
                            group=group, members=members)


@recipient_groups_bp.route("/<int:group_id>/delete", methods=["POST"])
@login_required(role="admin")
def delete_view(group_id: int):
    storage = _storage()
    group = storage.get_recipient_group(group_id)
    if not group or group["tenant_id"] != _tid():
        abort(404)
    storage.delete_recipient_group(group_id)
    flash(f"✓ Gruppo «{group['name']}» eliminato.", "success")
    return redirect(url_for("recipient_groups.list_view"))
