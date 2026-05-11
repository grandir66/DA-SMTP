"""Blueprint UI per gestione UFW (firewall di rete della VM).

Accesso: solo ruolo `superadmin` (modifica firewall = potenziale lock-out).
Tutte le modifiche sono auditate via logger.warning.

Endpoint:
- GET  /firewall/                 → status + lista regole
- POST /firewall/add              → nuova regola ALLOW
- POST /firewall/delete/<n>       → elimina regola n
- POST /firewall/reload           → ufw reload
"""
from __future__ import annotations

from flask import (Blueprint, current_app, flash, make_response, redirect,
                   render_template, request, session, url_for)

from ..auth import login_required
from .. import firewall_manager as fwm

firewall_bp = Blueprint("firewall", __name__, url_prefix="/firewall")


def _no_cache(resp):
    """Disabilita caching browser/proxy per pagine firewall (dati live)."""
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, private"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


def _actor() -> str:
    return session.get("username") or "ui"


@firewall_bp.route("/")
@login_required(role="superadmin")
def index():
    available, diagnostic = fwm.check_availability()
    rules: list = []
    info: dict = {}
    error: str | None = None
    if available:
        try:
            active, rules = fwm.status_numbered()
            info = fwm.status_verbose()
            info["active"] = active
        except fwm.UfwError as exc:
            error = str(exc)
    elif diagnostic:
        current_app.logger.warning("UFW unavailable: %s", diagnostic)
    resp = make_response(render_template(
        "admin/firewall.html",
        available=available,
        diagnostic=diagnostic,
        rules=rules,
        info=info,
        error=error,
    ))
    return _no_cache(resp)


@firewall_bp.route("/add", methods=["POST"])
@login_required(role="superadmin")
def add():
    try:
        fwm.add_rule(
            port=(request.form.get("port") or "").strip(),
            proto=(request.form.get("proto") or "tcp").strip(),
            source=(request.form.get("source") or "").strip() or None,
            comment=(request.form.get("comment") or "").strip() or None,
            actor=_actor(),
        )
        flash("Regola firewall aggiunta.", "success")
    except (fwm.UfwError, ValueError) as exc:
        flash(f"Errore: {exc}", "error")
    return redirect(url_for("firewall.index"))


@firewall_bp.route("/delete/<int:rule_number>", methods=["POST"])
@login_required(role="superadmin")
def delete(rule_number: int):
    try:
        fwm.delete_rule_by_number(rule_number, actor=_actor())
        flash(f"Regola firewall #{rule_number} eliminata.", "success")
    except (fwm.UfwError, ValueError) as exc:
        flash(f"Errore: {exc}", "error")
    return redirect(url_for("firewall.index"))


@firewall_bp.route("/reload", methods=["POST"])
@login_required(role="superadmin")
def reload():
    try:
        fwm.reload_ufw(actor=_actor())
        flash("UFW reload OK.", "success")
    except fwm.UfwError as exc:
        flash(f"Errore: {exc}", "error")
    return redirect(url_for("firewall.index"))
