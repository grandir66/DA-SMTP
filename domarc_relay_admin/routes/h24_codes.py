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
    source_email = (request.form.get("source_email") or "").strip().lower() or None
    h24_alias = (request.form.get("h24_alias") or "").strip().lower()
    fee = request.form.get("urgent_fee_eur")
    note = (request.form.get("note") or "").strip() or None
    enabled = request.form.get("enabled") == "1"

    # Validazione: serve almeno uno tra source_email e source_domain
    if not source_email and not source_domain:
        flash("Devi specificare un indirizzo email completo OPPURE un dominio.", "error")
        return redirect(url_for("h24_codes.targets_list_view"))

    try:
        fee_int = int(fee) if fee and str(fee).strip() else None
    except ValueError:
        fee_int = None
    try:
        tid = _storage().upsert_h24_target(
            tenant_id=_tid(),
            target_id=int(target_id) if target_id else None,
            source_domain=source_domain,
            source_email=source_email,
            h24_alias=h24_alias,
            urgent_fee_eur=fee_int,
            note=note,
            enabled=enabled,
        )
        match_label = source_email or source_domain
        flash(f"✓ Target salvato (id {tid}): {match_label} → {h24_alias}.",
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


# ----------------------------------------------------------- Settings H24 --

H24_SETTINGS = [
    {
        "key": "h24.default_inbound_alias",
        "label": "Mailbox H24 di rientro (fallback globale)",
        "placeholder": "h24@domarc.it",
        "help": "Indirizzo usato dai template auto-reply quando il dominio del "
                "mittente non è in tabella Mailbox H24 di rientro multi-brand.",
        "type": "email",
    },
    {
        "key": "h24.default_urgent_fee_eur",
        "label": "Importo intervento urgente (€)",
        "placeholder": "250",
        "help": "Default fattura per attivazione urgente. Override per brand "
                "nella tabella targets, override per regola via "
                "action_map.urgent_fee.",
        "type": "number",
    },
    {
        "key": "h24.code_one_shot_ttl_hours",
        "label": "TTL codici monouso (ore)",
        "placeholder": "24",
        "help": "Tempo di validità massimo dei codici emessi via auto-reply. "
                "Cap difensivo: anche se la regola chiede TTL maggiore, viene "
                "ridotto a questo valore. Max raccomandato: 24h.",
        "type": "number",
    },
    {
        "key": "h24.permanent_code_prefix",
        "label": "Prefisso codici permanenti auto-generati",
        "placeholder": "H24-",
        "help": "Prefisso per i codici permanenti generati automaticamente "
                "(quando crei un codice senza specificare il code custom). "
                "Es: 'H24-' produce codici tipo H24-XYZ12345ABCD.",
        "type": "text",
    },
    {
        "key": "h24.subject_extract_regex",
        "label": "Regex estrazione codice (override avanzato)",
        "placeholder": "(default hardcoded sicuro)",
        "help": "Override del regex usato per estrarre codici dal subject. "
                "Lascia vuoto per usare il default (consigliato). Settare solo "
                "se hai pattern custom non standard. Errori di sintassi → "
                "fallback automatico al default.",
        "type": "text",
    },
]


@h24_codes_bp.route("/h24-settings", methods=["GET", "POST"])
@login_required(role="admin")
def settings_view():
    storage = _storage()
    if request.method == "POST":
        for s in H24_SETTINGS:
            v = (request.form.get(s["key"]) or "").strip()
            storage.upsert_setting(s["key"], v, s["help"])
        flash("✓ Settings H24 aggiornate.", "success")
        return redirect(url_for("h24_codes.settings_view"))

    # GET: carica valori correnti
    values = {s["key"]: (storage.get_setting(s["key"]) or "") for s in H24_SETTINGS}
    return render_template(
        "admin/h24_settings.html",
        settings_meta=H24_SETTINGS,
        values=values,
    )


@h24_codes_bp.route("/h24-dashboard")
@login_required()
def dashboard_view():
    """Dashboard KPI H24: codici attivi, usi recenti, fatturato stimato."""
    storage = _storage()
    import sqlite3
    db_path = current_app.extensions["domarc_config"].db_path
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        # KPI attive
        kpi_perm_active = conn.execute(
            "SELECT COUNT(*) FROM customer_h24_codes "
            "WHERE enabled=1 AND revoked_at IS NULL AND tenant_id=?",
            (_tid(),),
        ).fetchone()[0]
        kpi_perm_revoked = conn.execute(
            "SELECT COUNT(*) FROM customer_h24_codes "
            "WHERE revoked_at IS NOT NULL AND tenant_id=?",
            (_tid(),),
        ).fetchone()[0]
        kpi_oneshot_active = conn.execute(
            "SELECT COUNT(*) FROM authorization_codes "
            "WHERE used_at IS NULL "
            "  AND valid_until > datetime('now') AND tenant_id=?",
            (_tid(),),
        ).fetchone()[0]
        # Usi ultimi 7 giorni
        usages_7d = conn.execute(
            "SELECT COUNT(*) FROM customer_h24_codes_usage u "
            "JOIN customer_h24_codes c ON c.id = u.h24_code_id "
            "WHERE u.used_at > datetime('now', '-7 days') AND c.tenant_id=?",
            (_tid(),),
        ).fetchone()[0]
        used_oneshot_7d = conn.execute(
            "SELECT COUNT(*) FROM authorization_codes "
            "WHERE used_at > datetime('now', '-7 days') AND tenant_id=?",
            (_tid(),),
        ).fetchone()[0]
        # Top clienti per usi
        top_customers = [dict(r) for r in conn.execute(
            "SELECT c.codice_cliente, c.code, COUNT(u.id) AS used_count "
            "FROM customer_h24_codes c "
            "LEFT JOIN customer_h24_codes_usage u ON u.h24_code_id = c.id "
            "  AND u.used_at > datetime('now', '-30 days') "
            "WHERE c.enabled=1 AND c.revoked_at IS NULL AND c.tenant_id=? "
            "GROUP BY c.id ORDER BY used_count DESC LIMIT 10",
            (_tid(),),
        ).fetchall()]
        # Targets
        targets_count = conn.execute(
            "SELECT COUNT(*) FROM smtp_relay_h24_targets "
            "WHERE enabled=1 AND tenant_id=?",
            (_tid(),),
        ).fetchone()[0]
        # Fatturato stimato 30gg (oneshot+permanent al fee default)
        try:
            fee = int(storage.get_setting("h24.default_urgent_fee_eur") or "250")
        except (TypeError, ValueError):
            fee = 250
        used_oneshot_30d = conn.execute(
            "SELECT COUNT(*) FROM authorization_codes "
            "WHERE used_at > datetime('now', '-30 days') AND tenant_id=?",
            (_tid(),),
        ).fetchone()[0]
        usages_30d = conn.execute(
            "SELECT COUNT(*) FROM customer_h24_codes_usage u "
            "JOIN customer_h24_codes c ON c.id = u.h24_code_id "
            "WHERE u.used_at > datetime('now', '-30 days') AND c.tenant_id=?",
            (_tid(),),
        ).fetchone()[0]
        # Recent usages
        recent_usages = [dict(r) for r in conn.execute(
            "SELECT u.*, c.code, c.codice_cliente, c.label "
            "FROM customer_h24_codes_usage u "
            "JOIN customer_h24_codes c ON c.id = u.h24_code_id "
            "WHERE c.tenant_id=? "
            "ORDER BY u.used_at DESC LIMIT 15",
            (_tid(),),
        ).fetchall()]

    estimated_revenue_30d = (used_oneshot_30d + usages_30d) * fee

    return render_template(
        "admin/h24_dashboard.html",
        kpi={
            "perm_active": kpi_perm_active,
            "perm_revoked": kpi_perm_revoked,
            "oneshot_active": kpi_oneshot_active,
            "usages_7d": usages_7d,
            "used_oneshot_7d": used_oneshot_7d,
            "targets_count": targets_count,
            "estimated_revenue_30d": estimated_revenue_30d,
            "usages_30d": usages_30d,
            "used_oneshot_30d": used_oneshot_30d,
            "fee_default": fee,
        },
        top_customers=top_customers,
        recent_usages=recent_usages,
    )
