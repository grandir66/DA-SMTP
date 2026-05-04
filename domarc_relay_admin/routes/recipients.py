"""Gestione destinatari + gruppi destinatari.

Pattern gemello a customer_groups, ma per indirizzi mail.
Use case: raggruppare indirizzi tecnici per regole di routing/forward
(es. "Tecnici no fuori orario" → catchall h24).

Pagine:
- ``GET  /recipients``                          lista indirizzi visti (autodiscovery)
- ``POST /recipients/bulk/add-to-group``        bulk add a gruppo esistente
- ``POST /recipients/bulk/create-group``        crea gruppo da selezione

- ``GET  /recipient-groups/``                   lista gruppi
- ``GET  /recipient-groups/new``                form nuovo gruppo
- ``POST /recipient-groups/new``                crea gruppo
- ``GET  /recipient-groups/<id>``               dettaglio + form edit + membri
- ``POST /recipient-groups/<id>``               update gruppo + bulk assign membri
- ``POST /recipient-groups/<id>/delete``        elimina gruppo (e membership)
"""
from __future__ import annotations

from flask import (Blueprint, abort, current_app, flash, g, redirect,
                   render_template, request, session, url_for)

from ..auth import login_required


recipients_bp = Blueprint("recipients", __name__, url_prefix="/recipients")
recipient_groups_bp = Blueprint("recipient_groups", __name__,
                                  url_prefix="/recipient-groups")


def _storage():
    return current_app.extensions["domarc_storage"]


def _tid() -> int:
    return int(getattr(g, "current_tenant_id", 1))


def _actor() -> str:
    return session.get("username") or "?"


# ============================================================ /recipients ===

@recipients_bp.route("/")
@login_required()
def list_view():
    storage = _storage()
    q = (request.args.get("q") or "").strip()
    domain = (request.args.get("domain") or "").strip()
    recipients = storage.list_recipients(tenant_id=_tid(),
                                            q=q or None,
                                            domain=domain or None,
                                            limit=2000)
    # Group membership lookup per badge
    membership: dict[str, list[dict]] = {}
    for r in recipients:
        gs = storage.get_recipient_groups_by_email(r["email"], tenant_id=_tid())
        if gs:
            membership[r["email"]] = gs
    all_groups = storage.list_recipient_groups(tenant_id=_tid(),
                                                  only_enabled=True)
    # Tutti i domini per filtro dropdown
    all_domains = sorted({r.get("domain") or "" for r in recipients
                            if r.get("domain")})
    return render_template("admin/recipients_list.html",
                            recipients=recipients,
                            membership=membership,
                            all_groups=all_groups,
                            all_domains=all_domains,
                            q=q, domain_filter=domain,
                            total=len(recipients))


@recipients_bp.route("/bulk/add-to-group", methods=["POST"])
@login_required(role="operator")
def bulk_add_to_group():
    storage = _storage()
    emails = request.form.getlist("email")
    group_id = (request.form.get("group_id") or "").strip()
    if not emails:
        flash("Nessun destinatario selezionato.", "error")
        return redirect(url_for("recipients.list_view"))
    if not group_id:
        flash("Selezionare un gruppo.", "error")
        return redirect(url_for("recipients.list_view"))
    try:
        gid = int(group_id)
    except ValueError:
        flash("group_id non valido.", "error")
        return redirect(url_for("recipients.list_view"))
    g_obj = storage.get_recipient_group(gid)
    if not g_obj or g_obj["tenant_id"] != _tid():
        abort(404)
    added = storage.add_recipients_to_group(gid, emails,
                                              tenant_id=_tid(),
                                              actor=_actor())
    flash(f"✓ {added} destinatari aggiunti al gruppo «{g_obj['name']}».", "success")
    return redirect(url_for("recipient_groups.detail_view", group_id=gid))


@recipients_bp.route("/bulk/create-group", methods=["POST"])
@login_required(role="operator")
def bulk_create_group():
    storage = _storage()
    emails = request.form.getlist("email")
    code = (request.form.get("code") or "").strip()
    name = (request.form.get("name") or "").strip()
    description = (request.form.get("description") or "").strip() or None
    color = (request.form.get("color") or "").strip() or None
    if not emails:
        flash("Nessun destinatario selezionato.", "error")
        return redirect(url_for("recipients.list_view"))
    if not code or not name:
        flash("Code e nome sono obbligatori.", "error")
        return redirect(url_for("recipients.list_view"))
    try:
        gid = storage.upsert_recipient_group(tenant_id=_tid(),
                                                code=code, name=name,
                                                description=description,
                                                color=color, enabled=True,
                                                actor=_actor())
        added = storage.add_recipients_to_group(gid, emails,
                                                  tenant_id=_tid(),
                                                  actor=_actor())
        flash(f"✓ Gruppo «{name}» creato con {added} membri.", "success")
        return redirect(url_for("recipient_groups.detail_view", group_id=gid))
    except ValueError as exc:
        flash(f"Errore: {exc}", "error")
        return redirect(url_for("recipients.list_view"))


# ===================================================== /recipient-groups ===

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
                actor=_actor(),
            )
            flash(f"✓ Gruppo destinatari creato (id {gid}).", "success")
            return redirect(url_for("recipient_groups.detail_view",
                                       group_id=gid))
        except ValueError as exc:
            flash(f"Errore: {exc}", "error")
    return render_template("admin/recipient_group_form.html",
                            group=None, members=[], available_recipients=[])


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
                actor=_actor(),
            )
            # Membri: lista email + lista raw da textarea (separata da virgola/newline)
            new_emails = list(request.form.getlist("members"))
            raw = (request.form.get("members_raw") or "").strip()
            if raw:
                import re
                for tok in re.split(r"[\s,;]+", raw):
                    tok = tok.strip()
                    if tok and "@" in tok:
                        new_emails.append(tok)
            n = storage.replace_recipient_group_members(group_id, new_emails,
                                                          tenant_id=_tid(),
                                                          actor=_actor())
            flash(f"✓ Gruppo aggiornato ({n} membri).", "success")
            return redirect(url_for("recipient_groups.detail_view",
                                       group_id=group_id))
        except ValueError as exc:
            flash(f"Errore: {exc}", "error")

    members = storage.list_recipient_group_members(group_id)
    member_emails = {m["email"] for m in members}
    # Suggerimenti dall'autodiscovery (top 500 più recenti)
    suggestions = storage.list_recipients(tenant_id=_tid(), limit=500)
    return render_template("admin/recipient_group_form.html",
                            group=group, members=members,
                            member_emails=member_emails,
                            available_recipients=suggestions)


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
