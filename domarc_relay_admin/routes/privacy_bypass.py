"""Blueprint UI per gestire la privacy bypass list.

Tre tab principali (`/privacy-bypass`):

- **Mittenti**: indirizzi from in privacy bypass + autocomplete da
  `addresses_from` rilevati.
- **Destinatari**: idem per to.
- **Domini**: lista di domini interi con scope (from/to/both).
- **Audit log**: storico attivazioni/disattivazioni (chi, quando, perché).

Operazioni:
- Toggle privacy bypass su un singolo indirizzo già censito.
- Aggiunta/edit/disabilita di un dominio.
- Aggiunta rapida per email non ancora censita (crea record in
  `addresses_from`/`addresses_to` con privacy_bypass=1).

Tutte le operazioni richiedono ruolo ``admin`` (creare/modificare) o
``superadmin`` (eliminare domini).
"""
from __future__ import annotations

from flask import (Blueprint, current_app, flash, g, redirect, render_template,
                   request, session, url_for)

from ..auth import login_required

privacy_bp = Blueprint("privacy_bypass", __name__, url_prefix="/privacy-bypass")


def _storage():
    return current_app.extensions["domarc_storage"]


def _tid() -> int:
    return int(getattr(g, "current_tenant_id", 1))


def _actor() -> str:
    return session.get("username") or "ui"


@privacy_bp.route("/")
@login_required(role="admin")
def index():
    storage = _storage()
    tid = _tid()
    from_list = storage.list_addresses_privacy_bypass("from", tenant_id=tid)
    to_list = storage.list_addresses_privacy_bypass("to", tenant_id=tid)
    domains = storage.list_privacy_bypass_domains(tenant_id=tid)
    audit = storage.list_privacy_bypass_audit(tenant_id=tid, limit=50)
    # Suggestions: indirizzi già rilevati ma NON ancora in bypass
    candidate_from = [
        a for a in storage.list_addresses("from", tenant_id=tid, limit=100)
        if not a.get("privacy_bypass")
    ][:20]
    candidate_to = [
        a for a in storage.list_addresses("to", tenant_id=tid, limit=100)
        if not a.get("privacy_bypass")
    ][:20]
    return render_template(
        "admin/privacy_bypass.html",
        from_list=from_list,
        to_list=to_list,
        domains=domains,
        audit=audit,
        candidate_from=candidate_from,
        candidate_to=candidate_to,
    )


@privacy_bp.route("/address/<kind>/<int:addr_id>/toggle", methods=["POST"])
@login_required(role="admin")
def toggle_address(kind: str, addr_id: int):
    if kind not in ("from", "to"):
        flash("kind non valido", "error")
        return redirect(url_for("privacy_bypass.index"))
    on = (request.form.get("on") or "").lower() in ("1", "true", "on")
    reason = (request.form.get("reason") or "").strip() or None
    if on and not reason:
        flash("Motivo obbligatorio per attivare la privacy bypass.", "error")
        return redirect(url_for("privacy_bypass.index"))
    try:
        _storage().set_address_privacy_bypass(
            kind, addr_id, on=on, reason=reason, actor=_actor(),
        )
        flash(f"Privacy bypass {'attivata' if on else 'rimossa'} sull'indirizzo.", "success")
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(request.referrer or url_for("privacy_bypass.index"))


@privacy_bp.route("/address/<kind>/quick-add", methods=["POST"])
@login_required(role="admin")
def quick_add_address(kind: str):
    """Inserimento rapido di un indirizzo non ancora censito + privacy_bypass=1."""
    if kind not in ("from", "to"):
        flash("kind non valido", "error")
        return redirect(url_for("privacy_bypass.index"))
    email = (request.form.get("email") or "").strip().lower()
    reason = (request.form.get("reason") or "").strip() or None
    if not email or "@" not in email:
        flash("Email non valida.", "error")
        return redirect(url_for("privacy_bypass.index"))
    if not reason:
        flash("Motivo obbligatorio.", "error")
        return redirect(url_for("privacy_bypass.index"))
    storage = _storage()
    tid = _tid()
    local, _, domain = email.partition("@")
    tbl = f"addresses_{kind}"
    with storage.transaction() as conn:  # type: ignore[attr-defined]
        existing = conn.execute(
            f"SELECT id FROM {tbl} WHERE tenant_id = ? AND LOWER(email_address) = ?",
            (tid, email),
        ).fetchone()
        if existing:
            addr_id = int(existing[0])
        else:
            cur = conn.execute(
                f"""INSERT INTO {tbl}
                       (tenant_id, email_address, local_part, domain,
                        first_seen_at, last_seen_at, seen_count, created_by,
                        privacy_bypass, privacy_bypass_reason, privacy_bypass_at, privacy_bypass_by)
                   VALUES (?, ?, ?, ?, datetime('now'), datetime('now'), 0, ?,
                           1, ?, datetime('now'), ?)""",
                (tid, email, local, domain, _actor(), reason, _actor()),
            )
            addr_id = int(cur.lastrowid or 0)
            conn.execute(
                """INSERT INTO privacy_bypass_audit
                       (tenant_id, target_kind, target_value, action, reason, actor)
                   VALUES (?, ?, ?, 'create', ?, ?)""",
                (tid, f"address_{kind}", email, reason, _actor()),
            )
    if existing:
        # già esisteva → attiva privacy bypass
        storage.set_address_privacy_bypass(
            kind, addr_id, on=True, reason=reason, actor=_actor(),
        )
    flash(f"Indirizzo {email} aggiunto in privacy bypass.", "success")
    return redirect(url_for("privacy_bypass.index"))


@privacy_bp.route("/domain/new", methods=["POST"])
@privacy_bp.route("/domain/<int:domain_id>", methods=["POST"])
@login_required(role="admin")
def upsert_domain(domain_id: int | None = None):
    domain = (request.form.get("domain") or "").strip().lower()
    scope = (request.form.get("scope") or "both").strip()
    reason = (request.form.get("reason") or "").strip() or None
    enabled = (request.form.get("enabled") or "on").lower() in ("on", "true", "1")
    if not reason:
        flash("Motivo obbligatorio per la privacy bypass.", "error")
        return redirect(url_for("privacy_bypass.index"))
    try:
        _storage().upsert_privacy_bypass_domain(
            tenant_id=_tid(), domain=domain, scope=scope, reason=reason,
            enabled=enabled, actor=_actor(), domain_id=domain_id,
        )
        flash(f"Dominio {'aggiornato' if domain_id else 'aggiunto'}: {domain} (scope={scope}).", "success")
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(url_for("privacy_bypass.index"))


@privacy_bp.route("/domain/<int:domain_id>/delete", methods=["POST"])
@login_required(role="superadmin")
def delete_domain(domain_id: int):
    _storage().delete_privacy_bypass_domain(domain_id, actor=_actor())
    flash("Dominio rimosso dalla privacy bypass list.", "success")
    return redirect(url_for("privacy_bypass.index"))
