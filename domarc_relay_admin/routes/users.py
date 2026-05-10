"""Gestione utenti (CRUD) con scoping per tenant.

Regole d'accesso:
- **superadmin**: vede e gestisce tutti gli utenti, può creare superadmin/admin/tech/readonly.
- **admin** (di tenant): vede e gestisce solo gli utenti del proprio tenant_id;
  può creare admin/tech/readonly del proprio tenant. NON può creare superadmin.
- **tech / readonly**: nessun accesso.
"""
from __future__ import annotations

from flask import Blueprint, abort, current_app, flash, g, redirect, render_template, request, session, url_for

from ..auth import login_required

users_bp = Blueprint("users", __name__)


def _storage():
    return current_app.extensions["domarc_storage"]


def _is_superadmin() -> bool:
    return (session.get("role") or "") == "superadmin"


def _user_tenant_id() -> int | None:
    return session.get("user_tenant_id")


def _scope_filter() -> int | None:
    """tenant_id da usare per filtrare list_users.
    superadmin → None (vede tutti); admin → suo tenant_id."""
    if _is_superadmin():
        return None
    return _user_tenant_id() or 1


def _validate_password(pwd: str) -> str | None:
    """Ritorna messaggio errore se la password non rispetta i requisiti, None se OK.

    Requisiti minimi (NIST 800-63B semplificato):
    - lunghezza >= 10
    - almeno 3 tipi tra: minuscole, maiuscole, cifre, simboli
    - no spazi a capo/inizio/fine
    """
    if pwd != pwd.strip():
        return "Password: niente spazi all'inizio/fine."
    if len(pwd) < 10:
        return "Password troppo corta: minimo 10 caratteri."
    if len(pwd) > 200:
        return "Password troppo lunga: massimo 200 caratteri."
    has_lower = any(c.islower() for c in pwd)
    has_upper = any(c.isupper() for c in pwd)
    has_digit = any(c.isdigit() for c in pwd)
    has_symbol = any(not c.isalnum() for c in pwd)
    variety = sum([has_lower, has_upper, has_digit, has_symbol])
    if variety < 3:
        return ("Password debole: usa almeno 3 tipi di carattere tra "
                "minuscole, maiuscole, cifre e simboli.")
    # Blacklist password ovvie
    blacklisted = {
        "admin123", "password", "password1", "domarc2026", "stormshield",
        "qwerty123", "letmein123",
    }
    if pwd.lower() in blacklisted:
        return "Password troppo ovvia: scegline una meno comune."
    return None


def _can_manage_user(target: dict) -> bool:
    """Un admin di tenant può gestire solo utenti del proprio tenant (no superadmin)."""
    if _is_superadmin():
        return True
    if not target:
        return False
    if target.get("role") == "superadmin":
        return False
    return target.get("tenant_id") == _user_tenant_id()


@users_bp.route("/users")
@login_required(role="admin")
def list_view():
    storage = _storage()
    users = storage.list_users(tenant_id=_scope_filter())
    tenants = storage.list_tenants() if _is_superadmin() else []
    # arricchisci con codice tenant
    by_id = {t["id"]: t for t in storage.list_tenants()}
    for u in users:
        t = by_id.get(u.get("tenant_id"))
        u["tenant_codice"] = t["codice"] if t else ("—" if not u.get("tenant_id") else f"#{u['tenant_id']}")
    return render_template(
        "admin/users_list.html",
        users=users,
        tenants=tenants,
        is_superadmin=_is_superadmin(),
    )


@users_bp.route("/users/new", methods=["GET", "POST"])
@users_bp.route("/users/<int:user_id>", methods=["GET", "POST"])
@login_required(role="admin")
def form_view(user_id: int | None = None):
    storage = _storage()
    is_new = user_id is None
    record: dict = {}
    if not is_new:
        record = storage.get_user(user_id) or {}
        if not record:
            flash("Utente non trovato", "error")
            return redirect(url_for("users.list_view"))
        if not _can_manage_user(record):
            abort(403)

    # Tenant disponibili: superadmin = tutti, admin = solo il proprio
    if _is_superadmin():
        available_tenants = storage.list_tenants()
    else:
        my_tid = _user_tenant_id() or 1
        available_tenants = [t for t in storage.list_tenants() if t["id"] == my_tid]

    if request.method == "POST":
        # Solo superadmin può creare superadmin
        new_role = (request.form.get("role") or "readonly").strip()
        if new_role == "superadmin" and not _is_superadmin():
            flash("Solo un superadmin può creare un altro superadmin.", "error")
            return redirect(url_for("users.list_view"))
        # Tenant: per admin di tenant, forziamo il proprio tenant_id
        new_tenant_id = request.form.get("tenant_id") or None
        if not _is_superadmin():
            new_tenant_id = _user_tenant_id() or 1
        data = {
            "username": request.form.get("username"),
            "role": new_role,
            "full_name": request.form.get("full_name"),
            "email": request.form.get("email"),
            "enabled": request.form.get("enabled") in ("on", "true", "1"),
            "tenant_id": new_tenant_id,
        }
        # Password: solo se nuovo o cambia, con check complessita' minima.
        pwd = (request.form.get("password") or "").strip()
        if pwd:
            pwd_error = _validate_password(pwd)
            if pwd_error:
                flash(pwd_error, "error")
                return redirect(url_for("users.form_view", user_id=user_id) if not is_new
                                 else url_for("users.form_view"))
            data["password"] = pwd
        elif is_new:
            flash("Password obbligatoria per il nuovo utente.", "error")
            return redirect(url_for("users.form_view"))
        if not is_new:
            data["id"] = user_id
        # Protezione: non rimuovere se stesso
        if not is_new and user_id == session.get("user_id") and not data["enabled"]:
            flash("Non puoi disabilitare il tuo stesso account.", "error")
            return redirect(url_for("users.form_view", user_id=user_id))
        try:
            new_id = storage.upsert_user(data)
            flash(f"Utente {'creato' if is_new else 'aggiornato'} (id={new_id}).", "success")
            return redirect(url_for("users.form_view", user_id=new_id))
        except ValueError as exc:
            flash(str(exc), "error")
            record = {**record, **data}

    return render_template(
        "admin/user_form.html",
        is_new=is_new,
        record=record,
        is_superadmin=_is_superadmin(),
        available_tenants=available_tenants,
        is_self=(not is_new and user_id == session.get("user_id")),
    )


@users_bp.route("/users/<int:user_id>/delete", methods=["POST"])
@login_required(role="admin")
def delete_view(user_id: int):
    storage = _storage()
    target = storage.get_user(user_id)
    if not target:
        flash("Utente non trovato", "error")
        return redirect(url_for("users.list_view"))
    if not _can_manage_user(target):
        abort(403)
    if user_id == session.get("user_id"):
        flash("Non puoi eliminare il tuo stesso account.", "error")
        return redirect(url_for("users.list_view"))
    storage.delete_user(user_id)
    flash("Utente eliminato.", "success")
    return redirect(url_for("users.list_view"))
