"""Blueprint UI per gestione chiavi API (cifrate) e moduli installabili.

- ``/settings/api-keys`` — CRUD chiavi API (Anthropic, future). Cifratura
  Fernet via :mod:`secrets_manager`. Le chiavi vengono iniettate in
  ``os.environ`` al boot dell'app via :func:`load_secrets_into_env`.
- ``/settings/modules`` — catalogo moduli Python installabili (whitelist
  hard-coded). Bottone install/uninstall con audit log.

Tutte le operazioni richiedono ``admin``. Solo ``superadmin`` può eliminare
chiavi e disinstallare moduli.
"""
from __future__ import annotations

from flask import (Blueprint, current_app, flash, g, redirect, render_template,
                   request, session, url_for)

from ..auth import login_required

secrets_modules_bp = Blueprint("secrets_modules", __name__, url_prefix="/settings")


def _storage():
    return current_app.extensions["domarc_storage"]


def _tid() -> int:
    return int(getattr(g, "current_tenant_id", 1))


def _actor() -> str:
    return session.get("username") or "ui"


# =================================================== API KEYS =====

@secrets_modules_bp.route("/api-keys")
@login_required(role="admin")
def api_keys_list():
    storage = _storage()
    keys = storage.list_api_keys(tenant_id=_tid())
    return render_template("admin/api_keys.html", keys=keys)


@secrets_modules_bp.route("/api-keys/new", methods=["GET", "POST"])
@secrets_modules_bp.route("/api-keys/<int:key_id>", methods=["GET", "POST"])
@login_required(role="admin")
def api_key_form(key_id: int | None = None):
    from ..secrets_manager import get_secrets_manager

    storage = _storage()
    record: dict = {}
    if key_id:
        record = storage.get_api_key(key_id) or {}
        if not record:
            flash("Chiave non trovata.", "error")
            return redirect(url_for("secrets_modules.api_keys_list"))

    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        env_var_name = (request.form.get("env_var_name") or "").strip()
        new_value = (request.form.get("value") or "").strip()
        description = (request.form.get("description") or "").strip() or None
        enabled = (request.form.get("enabled") or "").lower() in ("on", "true", "1")
        if not name or not env_var_name:
            flash("Nome ed env var sono obbligatori.", "error")
            return render_template("admin/api_key_form.html",
                                    record=record, is_new=key_id is None)
        if not key_id and not new_value:
            flash("Valore della chiave obbligatorio in fase di creazione.", "error")
            return render_template("admin/api_key_form.html",
                                    record=record, is_new=True)
        try:
            sm = get_secrets_manager()
            if new_value:
                encrypted = sm.encrypt(new_value)
                masked = sm.mask(new_value)
            else:
                # Edit senza cambiare valore: riusa l'encrypted esistente
                encrypted = record.get("value_encrypted")
                masked = record.get("masked_preview")
            new_id = storage.upsert_api_key(
                tenant_id=_tid(), name=name, env_var_name=env_var_name,
                value_encrypted=encrypted, masked_preview=masked,
                description=description, enabled=enabled,
                actor=_actor(), key_id=key_id,
            )
            # Inietta subito in env (no restart)
            if new_value and enabled:
                import os as _os
                _os.environ[env_var_name] = new_value
            flash(f"Chiave salvata (id={new_id}).", "success")
            return redirect(url_for("secrets_modules.api_keys_list"))
        except Exception as exc:  # noqa: BLE001
            flash(f"Errore: {exc}", "error")

    return render_template("admin/api_key_form.html",
                            record=record, is_new=key_id is None)


@secrets_modules_bp.route("/api-keys/<int:key_id>/toggle", methods=["POST"])
@login_required(role="admin")
def api_key_toggle(key_id: int):
    storage = _storage()
    new_state = storage.toggle_api_key(key_id)
    # Update env: load se attivo, unset se disattivo
    record = storage.get_api_key(key_id)
    if record:
        import os as _os
        env_var = record.get("env_var_name")
        if env_var:
            if new_state:
                from ..secrets_manager import get_secrets_manager
                try:
                    _os.environ[env_var] = get_secrets_manager().decrypt(record["value_encrypted"])
                except ValueError:
                    pass
            else:
                _os.environ.pop(env_var, None)
    flash(f"Chiave {'attivata' if new_state else 'disattivata'}.", "success")
    return redirect(url_for("secrets_modules.api_keys_list"))


@secrets_modules_bp.route("/api-keys/<int:key_id>/delete", methods=["POST"])
@login_required(role="superadmin")
def api_key_delete(key_id: int):
    storage = _storage()
    record = storage.get_api_key(key_id)
    storage.delete_api_key(key_id)
    if record:
        import os as _os
        env_var = record.get("env_var_name")
        if env_var:
            _os.environ.pop(env_var, None)
    flash("Chiave eliminata.", "success")
    return redirect(url_for("secrets_modules.api_keys_list"))


# =================================================== MODULES =====

@secrets_modules_bp.route("/modules")
@login_required(role="admin")
def modules_list():
    from ..module_manager import list_modules_status
    storage = _storage()
    modules = list_modules_status()
    log = storage.list_module_install_log(limit=20)
    return render_template("admin/modules.html", modules=modules, log=log)


@secrets_modules_bp.route("/modules/<code>/install", methods=["POST"])
@login_required(role="superadmin")
def module_install(code: str):
    from ..module_manager import install_module
    storage = _storage()
    result = install_module(code, storage=storage, actor=_actor())
    if result.get("ok"):
        flash(f"Modulo '{code}' installato in {result.get('duration_ms')}ms.", "success")
    else:
        flash(f"Installazione '{code}' fallita: {result.get('error') or 'rc=' + str(result.get('return_code'))}", "error")
    return redirect(url_for("secrets_modules.modules_list"))


@secrets_modules_bp.route("/modules/<code>/uninstall", methods=["POST"])
@login_required(role="superadmin")
def module_uninstall(code: str):
    from ..module_manager import uninstall_module
    storage = _storage()
    result = uninstall_module(code, storage=storage, actor=_actor())
    if result.get("ok"):
        flash(f"Modulo '{code}' disinstallato.", "success")
    else:
        flash(f"Disinstallazione fallita: {result.get('error')}", "error")
    return redirect(url_for("secrets_modules.modules_list"))


@secrets_modules_bp.route("/modules/log/<int:log_id>")
@login_required(role="admin")
def module_log_detail(log_id: int):
    storage = _storage()
    rows = [r for r in storage.list_module_install_log(limit=200)
            if r["id"] == log_id]
    if not rows:
        flash("Log non trovato.", "error")
        return redirect(url_for("secrets_modules.modules_list"))
    return render_template("admin/module_log_detail.html", entry=rows[0])
