"""Blueprint per: routes (smarthost), domain_routing, addresses_from/to, settings, connection."""
from __future__ import annotations

import logging
from flask import Blueprint, current_app, flash, g, redirect, render_template, request, session, url_for

from ..auth import login_required

logger = logging.getLogger(__name__)


# ----- Routes (smarthost / forward / redirect per alias) ---------------------
routes_bp = Blueprint("routes", __name__)


def _storage():
    return current_app.extensions["domarc_storage"]


def _tid() -> int:
    return int(getattr(g, "current_tenant_id", 1))


@routes_bp.route("/routes")
@login_required()
def routes_list():
    rows = _storage().list_routes(tenant_id=_tid())
    return render_template("admin/routes_list.html", routes=rows)


@routes_bp.route("/routes/new", methods=["GET", "POST"])
@routes_bp.route("/routes/<int:route_id>", methods=["GET", "POST"])
@login_required(role="operator")
def route_form(route_id: int | None = None):
    is_new = route_id is None
    record: dict = {}
    if not is_new:
        record = _storage().get_route(route_id) or {}
        if not record:
            flash("Route non trovata", "error")
            return redirect(url_for("routes.routes_list"))
    if request.method == "POST":
        data = {
            "id": route_id if not is_new else None,
            "local_part": request.form.get("local_part"),
            "domain": request.form.get("domain"),
            "codice_cliente": request.form.get("codice_cliente") or None,
            "forward_target": request.form.get("forward_target") or None,
            "forward_port": request.form.get("forward_port") or 25,
            "forward_tls": request.form.get("forward_tls") or "opportunistic",
            "redirect_target": request.form.get("redirect_target") or None,
            "enabled": request.form.get("enabled") in ("on", "true", "1"),
            "apply_rules": request.form.get("apply_rules") in ("on", "true", "1"),
            "notes": request.form.get("notes"),
        }
        try:
            new_id = _storage().upsert_route(data, tenant_id=_tid())
            flash(f"Route {'creata' if is_new else 'aggiornata'}.", "success")
            return redirect(url_for("routes.route_form", route_id=new_id))
        except ValueError as exc:
            flash(str(exc), "error")
    return render_template("admin/route_form.html", is_new=is_new, record=record)


@routes_bp.route("/routes/<int:route_id>/delete", methods=["POST"])
@login_required(role="admin")
def route_delete(route_id: int):
    _storage().delete_route(route_id)
    flash("Route eliminata.", "success")
    return redirect(url_for("routes.routes_list"))


# ----- Domain routing --------------------------------------------------------
domains_bp = Blueprint("domains", __name__)


@domains_bp.route("/domains")
@login_required()
def domains_list():
    rows = _storage().list_domain_routing(tenant_id=_tid())
    return render_template("admin/domains_list.html", domains=rows)


@domains_bp.route("/domains/new", methods=["GET", "POST"])
@domains_bp.route("/domains/<int:domain_id>", methods=["GET", "POST"])
@login_required(role="operator")
def domain_form(domain_id: int | None = None):
    is_new = domain_id is None
    record: dict = {}
    if not is_new:
        record = _storage().get_domain_routing(domain_id) or {}
        if not record:
            flash("Dominio non trovato", "error")
            return redirect(url_for("domains.domains_list"))
    if request.method == "POST":
        data = {
            "id": domain_id if not is_new else None,
            "domain": request.form.get("domain"),
            "smarthost_host": request.form.get("smarthost_host") or None,
            "smarthost_port": request.form.get("smarthost_port") or 25,
            "smarthost_tls": request.form.get("smarthost_tls") or "opportunistic",
            "apply_rules": request.form.get("apply_rules") in ("on", "true", "1"),
            "enabled": request.form.get("enabled") in ("on", "true", "1"),
            "notes": request.form.get("notes"),
        }
        try:
            new_id = _storage().upsert_domain_routing(data, tenant_id=_tid())
            flash(f"Dominio {'creato' if is_new else 'aggiornato'}.", "success")
            return redirect(url_for("domains.domain_form", domain_id=new_id))
        except ValueError as exc:
            flash(str(exc), "error")
    return render_template("admin/domain_form.html", is_new=is_new, record=record)


@domains_bp.route("/domains/<int:domain_id>/delete", methods=["POST"])
@login_required(role="admin")
def domain_delete(domain_id: int):
    _storage().delete_domain_routing(domain_id)
    flash("Dominio eliminato.", "success")
    return redirect(url_for("domains.domains_list"))


# ----- Addresses (mittenti + destinatari) -----------------------------------
addresses_bp = Blueprint("addresses", __name__)


def _addresses_stats(rows: list[dict]) -> dict:
    """Calcola stats card su una lista di indirizzi (mittenti o destinatari)."""
    total = len(rows)
    with_codcli = sum(1 for r in rows if r.get("codice_cliente"))
    domains = {(r.get("domain") or "").lower() for r in rows if r.get("domain")}
    return {
        "total": total,
        "with_codcli": with_codcli,
        "without_codcli": total - with_codcli,
        "unique_domains": len(domains),
    }


@addresses_bp.route("/addresses-from/auto-match-codcli", methods=["POST"])
@login_required(role="operator")
def auto_match_codcli():
    """Per ogni mittente senza codcli (o con `codcli_source != 'manual'`),
    cerca il dominio nei customers del customer_source attivo.
    Se trova un match unico, popola `codice_cliente` con `codcli_source='auto'`.
    """
    storage = _storage()
    cs = current_app.extensions.get("domarc_customer_source")
    if not cs:
        flash("Customer source non disponibile.", "error")
        return redirect(url_for("addresses.from_list"))

    # Costruisci index domain → [codcli, ...]
    domain_to_codcli: dict[str, list[str]] = {}
    for c in cs.list_customers():
        for d in (c.domains or []):
            domain_to_codcli.setdefault(d.strip().lower(), []).append(c.codice_cliente)

    rows = storage.list_addresses("from", tenant_id=_tid())
    matched = 0
    ambiguous = 0
    for r in rows:
        # Skip override manuali e indirizzi che hanno già codcli auto
        if r.get("codcli_source") == "manual" and r.get("codice_cliente"):
            continue
        if r.get("codice_cliente"):
            continue
        domain = (r.get("domain") or "").lower()
        if not domain:
            continue
        cands = domain_to_codcli.get(domain) or []
        if len(cands) == 1:
            storage.upsert_address_codcli("from", r["id"], cands[0])
            # Marca origine auto via UPDATE diretta
            with storage.transaction() as conn:
                conn.execute(
                    "UPDATE addresses_from SET codcli_source='auto' WHERE id = ?",
                    (r["id"],),
                )
            matched += 1
        elif len(cands) > 1:
            ambiguous += 1
    flash(
        f"Auto-match completato: {matched} mittenti aggiornati, "
        f"{ambiguous} ambigui (dominio condiviso da più clienti, intervento manuale).",
        "success" if matched else "info",
    )
    return redirect(url_for("addresses.from_list"))


@addresses_bp.route("/addresses-from")
@login_required()
def from_list():
    q = (request.args.get("q") or "").strip()
    domain_filter = (request.args.get("domain") or "").strip().lower()
    no_codcli = request.args.get("no_codcli") in ("on", "true", "1")
    rows = _storage().list_addresses("from", tenant_id=_tid(), search=q or None)
    if domain_filter:
        rows = [r for r in rows if (r.get("domain") or "").lower() == domain_filter]
    if no_codcli:
        rows = [r for r in rows if not r.get("codice_cliente")]
    all_rows = _storage().list_addresses("from", tenant_id=_tid())
    return render_template("admin/addresses_list.html",
                           rows=rows, kind="from",
                           title="Mittenti noti",
                           search=q,
                           domain_filter=domain_filter,
                           no_codcli=no_codcli,
                           stats=_addresses_stats(all_rows))


@addresses_bp.route("/addresses-to")
@login_required()
def to_list():
    q = (request.args.get("q") or "").strip()
    domain_filter = (request.args.get("domain") or "").strip().lower()
    rows = _storage().list_addresses("to", tenant_id=_tid(), search=q or None)
    if domain_filter:
        rows = [r for r in rows if (r.get("domain") or "").lower() == domain_filter]
    all_rows = _storage().list_addresses("to", tenant_id=_tid())
    return render_template("admin/addresses_list.html",
                           rows=rows, kind="to",
                           title="Destinatari noti",
                           search=q,
                           domain_filter=domain_filter,
                           no_codcli=False,
                           stats=_addresses_stats(all_rows))


@addresses_bp.route("/addresses-<kind>/<int:addr_id>/codcli", methods=["POST"])
@login_required(role="operator")
def address_set_codcli(kind: str, addr_id: int):
    if kind not in ("from", "to"):
        flash("Tipo non valido", "error")
        return redirect(url_for("dashboard.index"))
    codcli = (request.form.get("codice_cliente") or "").strip().upper() or None
    _storage().upsert_address_codcli(kind, addr_id, codcli)
    flash("Codcli aggiornato.", "success")
    return redirect(url_for(f"addresses.{kind}_list"))


@addresses_bp.route("/addresses-<kind>/<int:addr_id>/delete", methods=["POST"])
@login_required(role="admin")
def address_delete(kind: str, addr_id: int):
    if kind not in ("from", "to"):
        flash("Tipo non valido", "error")
        return redirect(url_for("dashboard.index"))
    _storage().delete_address(kind, addr_id)
    flash("Indirizzo eliminato.", "success")
    return redirect(url_for(f"addresses.{kind}_list"))


# ----- Settings (chiave-valore globali) -------------------------------------
settings_bp = Blueprint("settings", __name__)


@settings_bp.route("/settings/passthrough/toggle", methods=["POST"])
@login_required(role="admin")
def toggle_passthrough():
    """Kill-switch globale: attiva/disattiva relay_passthrough_only.

    Quando ATTIVO il listener bypassa rule engine + IA e fa solo default
    delivery via smarthost del dominio. Da usare in caso di problemi al
    cutover. La modifica si propaga al listener al prossimo sync (≤5min)
    o subito dopo restart del scheduler.
    """
    storage = _storage()
    cur = (storage.get_setting("relay_passthrough_only") or "false").strip().lower()
    new_value = "false" if cur in ("true", "1", "yes", "on") else "true"
    storage.upsert_setting(
        "relay_passthrough_only", new_value,
        description="KILL SWITCH: bypass rule engine + IA, solo default delivery via smarthost",
    )
    actor = session.get("username") or "?"
    if new_value == "true":
        flash(
            f"⚠ KILL SWITCH ATTIVATO: il relay ora consegna SOLO via smarthost "
            f"(bypass rule engine + IA). Attivato da {actor}. Si propaga al "
            f"listener entro 5 min o subito con `systemctl restart "
            f"stormshield-smtp-relay-scheduler`.",
            "warning",
        )
    else:
        flash(
            f"✓ Kill switch disattivato. Il relay torna al normale flusso "
            f"con regole + IA. Disattivato da {actor}.",
            "success",
        )
    return redirect(request.referrer or url_for("dashboard.index"))


@settings_bp.route("/settings", methods=["GET", "POST"])
@login_required(role="admin")
def settings_view():
    if request.method == "POST":
        # Salva tutte le chiavi del form (eccetto quelle protected come API key)
        protected = {"relay_api_key", "schema_version"}
        n = 0
        for key, value in request.form.items():
            if key in protected:
                continue
            if not key.replace("_", "").replace("-", "").isalnum():
                continue  # sanity
            _storage().upsert_setting(key, value.strip())
            n += 1
        flash(f"Salvate {n} impostazioni.", "success")
        return redirect(url_for("settings.settings_view"))
    settings = _storage().list_settings()
    # Maschera la API key per UI (mostra solo prefix)
    for s in settings:
        if s["key"] == "relay_api_key" and s.get("value"):
            v = s["value"]
            s["value"] = v[:8] + "…" + v[-4:] if len(v) > 16 else "***"
            s["_masked"] = True
    return render_template("admin/settings_view.html", settings=settings)


# ----- Connection (API key + test) ------------------------------------------
connection_bp = Blueprint("connection", __name__)


@connection_bp.route("/connection")
@login_required(role="admin")
def connection_view():
    storage = _storage()
    # Forza generazione chiave se non c'è
    from .api import _get_or_create_api_key
    api_key = _get_or_create_api_key()
    return render_template("admin/connection_view.html",
                           api_key=api_key,
                           settings=storage.list_settings())


@connection_bp.route("/connection/regen-key", methods=["POST"])
@login_required(role="admin")
def regen_key():
    import secrets as _s
    new_key = _s.token_urlsafe(48)
    _storage().upsert_setting("relay_api_key", new_key,
                               "Chiave X-API-Key per il listener relay verso questo admin standalone.")
    flash("API key rigenerata. Aggiorna il listener.", "success")
    return redirect(url_for("connection.connection_view"))
