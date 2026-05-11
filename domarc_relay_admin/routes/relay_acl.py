"""Blueprint UI per gestire la Relay Client ACL.

Lista di IP / CIDR autorizzati a consegnare mail al listener :25.
- Quando la lista ha almeno una entry abilitata: il listener fa enforcement
  (rifiuta connessioni da IP non whitelistati con "550 5.7.1").
- Quando vuota: nessun enforcement, comportamento legacy.

Tutte le operazioni richiedono ruolo `admin`. Sync verso listener avviene
nei normali cicli scheduler (5 min) oppure forza-restart scheduler per
applicare immediatamente.
"""
from __future__ import annotations

from flask import (Blueprint, current_app, flash, g, redirect, render_template,
                   request, session, url_for)

from ..auth import login_required

relay_acl_bp = Blueprint("relay_acl", __name__, url_prefix="/relay-acl")


def _storage():
    return current_app.extensions["domarc_storage"]


def _tid() -> int:
    return int(getattr(g, "current_tenant_id", 1))


def _actor() -> str:
    return session.get("username") or "ui"


@relay_acl_bp.route("/")
@login_required(role="admin")
def index():
    storage = _storage()
    entries = storage.list_relay_client_acl(tenant_id=_tid())
    n_enabled = sum(1 for e in entries if e.get("enabled"))
    return render_template(
        "admin/relay_acl.html",
        entries=entries,
        n_enabled=n_enabled,
        enforcing=(n_enabled > 0),
    )


@relay_acl_bp.route("/new", methods=["GET"])
@login_required(role="admin")
def new_view():
    return render_template("admin/relay_acl_form.html", entry=None)


@relay_acl_bp.route("/<int:entry_id>/edit", methods=["GET"])
@login_required(role="admin")
def edit_view(entry_id: int):
    storage = _storage()
    entries = storage.list_relay_client_acl(tenant_id=_tid())
    entry = next((e for e in entries if int(e["id"]) == entry_id), None)
    if not entry:
        flash("Entry non trovata.", "error")
        return redirect(url_for("relay_acl.index"))
    return render_template("admin/relay_acl_form.html", entry=entry)


@relay_acl_bp.route("/upsert", methods=["POST"])
@login_required(role="admin")
def upsert():
    storage = _storage()
    data = {
        "id": request.form.get("id") or None,
        "ip_or_cidr": (request.form.get("ip_or_cidr") or "").strip(),
        "label": request.form.get("label"),
        "description": request.form.get("description"),
        "enabled": request.form.get("enabled") in ("on", "true", "1"),
        "set_by": _actor(),
    }
    try:
        new_id = storage.upsert_relay_client_acl(data, tenant_id=_tid())
        action = "aggiornata" if data["id"] else "aggiunta"
        flash(f"Entry ACL {action} (id={new_id}, ip={data['ip_or_cidr']}).", "success")
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(url_for("relay_acl.index"))


@relay_acl_bp.route("/<int:entry_id>/toggle", methods=["POST"])
@login_required(role="admin")
def toggle(entry_id: int):
    storage = _storage()
    entries = storage.list_relay_client_acl(tenant_id=_tid())
    target = next((e for e in entries if int(e["id"]) == entry_id), None)
    if not target:
        flash("Entry non trovata.", "error")
        return redirect(url_for("relay_acl.index"))
    storage.upsert_relay_client_acl(
        {
            "id": entry_id,
            "ip_or_cidr": target["ip_or_cidr"],
            "label": target.get("label"),
            "description": target.get("description"),
            "enabled": not target.get("enabled"),
            "set_by": _actor(),
        },
        tenant_id=_tid(),
    )
    new_state = "disabilitata" if target.get("enabled") else "abilitata"
    flash(f"Entry ACL {new_state} ({target['ip_or_cidr']}).", "success")
    return redirect(url_for("relay_acl.index"))


@relay_acl_bp.route("/<int:entry_id>/delete", methods=["POST"])
@login_required(role="admin")
def delete(entry_id: int):
    storage = _storage()
    if storage.delete_relay_client_acl(entry_id, tenant_id=_tid()):
        flash("Entry ACL eliminata.", "success")
    else:
        flash("Entry non trovata.", "error")
    return redirect(url_for("relay_acl.index"))
