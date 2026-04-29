"""Anagrafica clienti — vista read-only dal CustomerSource configurato.

Mostra clienti + domini + alias. La sorgente dipende dal config:
- yaml: file YAML
- sqlite: tabella customers
- rest: API CRM esterno
- stormshield: API manager Domarc

UI read-only per visibilità: l'editing vero dipende dalla sorgente (es. modificare
il file YAML, aprire l'admin del CRM, ecc.). Per il backend `sqlite` un futuro
v0.2 può aggiungere CRUD inline.
"""
from __future__ import annotations

from flask import Blueprint, current_app, render_template, request

from ..auth import login_required

customers_bp = Blueprint("customers", __name__)


@customers_bp.route("/customers")
@login_required()
def list_view():
    cs = current_app.extensions["domarc_customer_source"]
    search = (request.args.get("q") or "").strip().lower()
    customers = cs.list_customers()
    if search:
        customers = [
            c for c in customers
            if search in (c.codice_cliente or "").lower()
            or search in (c.ragione_sociale or "").lower()
            or any(search in d.lower() for d in (c.domains or []))
            or any(search in a.lower() for a in (c.aliases or []))
        ]
    health = cs.health()
    return render_template(
        "admin/customers_list.html",
        customers=customers,
        health=health,
        search=request.args.get("q") or "",
    )
