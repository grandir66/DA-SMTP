"""Anagrafica clienti — vista da CustomerSource configurato + integrazione orari.

Mostra clienti + domini + alias + stato profilo orari + eccezioni attive.
Permette di accedere rapidamente alla pagina di gestione eccezioni per
ciascun cliente (link diretto a /service-hours/<codcli>).
"""
from __future__ import annotations

from datetime import date as _date

from flask import (Blueprint, abort, current_app, flash, g, redirect,
                   render_template, request, session, url_for)

from ..auth import login_required

customers_bp = Blueprint("customers", __name__)


def _storage():
    return current_app.extensions["domarc_storage"]


def _tid() -> int:
    return int(getattr(g, "current_tenant_id", 1))


@customers_bp.route("/customers")
@login_required()
def list_view():
    cs = current_app.extensions["domarc_customer_source"]
    search = (request.args.get("q") or "").strip().lower()
    profile_filter = (request.args.get("profile") or "").strip().upper()
    contract_filter = (request.args.get("contract") or "").strip().lower()

    customers = cs.list_customers()

    # Filtro testo
    if search:
        customers = [
            c for c in customers
            if search in (c.codice_cliente or "").lower()
            or search in (c.ragione_sociale or "").lower()
            or any(search in d.lower() for d in (c.domains or []))
            or any(search in a.lower() for a in (c.aliases or []))
        ]
    # Filtro profilo
    if profile_filter:
        customers = [c for c in customers
                      if (c.tipologia_servizio or "").upper() == profile_filter]
    # Filtro contratto
    if contract_filter == "active":
        customers = [c for c in customers if c.contract_active]
    elif contract_filter == "inactive":
        customers = [c for c in customers if not c.contract_active]

    # Stats per filtri
    all_customers = cs.list_customers()
    by_profile: dict[str, int] = {}
    for c in all_customers:
        p = (c.tipologia_servizio or "—").upper()
        if p == "STANDARD":
            p = "STD"
        by_profile[p] = by_profile.get(p, 0) + 1

    # Eccezioni schedule attive oggi (joint per codcli)
    today = _date.today().isoformat()
    storage = _storage()
    tid = _tid()
    exceptions_today_by_codcli: dict[str, int] = {}
    sh_records_by_codcli: dict[str, dict] = {}
    try:
        sh_rows = storage.list_service_hours(tenant_id=tid)
        for r in sh_rows:
            cc = r.get("codice_cliente")
            if not cc:
                continue
            sh_records_by_codcli[cc] = r
            for exc in (r.get("schedule_exceptions") or []):
                if (exc.get("date") or "") == today:
                    exceptions_today_by_codcli[cc] = exceptions_today_by_codcli.get(cc, 0) + 1
    except Exception:  # noqa: BLE001
        pass

    # Gruppi per ogni cliente (per badge in tabella)
    groups_by_codcli: dict[str, list[dict]] = {}
    try:
        for row in storage._connect().execute(
            """SELECT m.codice_cliente, g.id, g.code, g.name, g.color
                 FROM customer_group_members m
                 JOIN customer_groups g ON g.id = m.group_id
                WHERE g.tenant_id = ? AND g.enabled = 1
                ORDER BY g.name COLLATE NOCASE""",
            (tid,),
        ).fetchall():
            groups_by_codcli.setdefault(row["codice_cliente"], []).append(dict(row))
    except Exception:  # noqa: BLE001
        pass

    health = cs.health()
    return render_template(
        "admin/customers_list.html",
        customers=customers,
        all_count=len(all_customers),
        filtered_count=len(customers),
        by_profile=by_profile,
        contract_active_count=sum(1 for c in all_customers if c.contract_active),
        contract_inactive_count=sum(1 for c in all_customers if not c.contract_active),
        sh_records=sh_records_by_codcli,
        exceptions_today=exceptions_today_by_codcli,
        groups_by_codcli=groups_by_codcli,
        health=health,
        search=request.args.get("q") or "",
        profile_filter=profile_filter,
        contract_filter=contract_filter,
        today=today,
    )


@customers_bp.route("/customers/<codcli>/groups", methods=["GET", "POST"])
@login_required(role="operator")
def groups_view(codcli: str):
    """Form assegnazione gruppi a un singolo cliente."""
    cs = current_app.extensions["domarc_customer_source"]
    storage = _storage()
    customer = cs.get_by_codcli(codcli)
    if customer is None:
        abort(404)

    if request.method == "POST":
        group_ids = [int(x) for x in request.form.getlist("group_ids") if x.isdigit()]
        n = storage.set_customer_groups(
            codcli, group_ids,
            tenant_id=_tid(),
            actor=session.get("username") or "?",
        )
        flash(f"✓ {n} gruppi assegnati a {customer.ragione_sociale or codcli}.", "success")
        return redirect(url_for("customers.list_view"))

    all_groups = storage.list_customer_groups(tenant_id=_tid())
    current_groups = storage.list_groups_for_customer(codcli, tenant_id=_tid())
    current_ids = {int(g["id"]) for g in current_groups}
    return render_template(
        "admin/customer_assign_groups.html",
        customer=customer,
        all_groups=all_groups,
        current_ids=current_ids,
    )
