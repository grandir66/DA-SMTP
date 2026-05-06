"""Domain resolve strategy UI (M038).

Pagina `/domains/` per configurare la strategia di risoluzione cliente per
domini condivisi. 3 strategy:
  - 'auto'    (default implicito): primo cliente con contract_active=1
  - 'primary' : forza il codcli specificato
  - 'bypass'  : non risolve cliente per quel dominio

Pre-popolata dalla migration con suggerimenti automatici per i domini
condivisi (>1 cliente nel dominio).
"""
from __future__ import annotations

from flask import Blueprint, current_app, flash, redirect, render_template, request, session, url_for

from ..auth import login_required
from flask import g as _g

domain_strategy_bp = Blueprint("domain_strategy", __name__, url_prefix="/domain-strategy")


def current_tenant_id() -> int:
    return int(getattr(_g, "current_tenant_id", 1))


def _storage():
    return current_app.extensions["domarc_storage"]


@domain_strategy_bp.route("/")
@login_required(role="operator")
def list_view():
    storage = _storage()
    items = storage.list_domain_strategies(tenant_id=current_tenant_id())

    # Per ogni dominio, prepara breakdown clienti (per UI)
    customers_by_codcli = {}
    for c in storage.list_customers_local(tenant_id=current_tenant_id()):
        customers_by_codcli[str(c.get("codcli") or c.get("codice_cliente") or "")] = c

    # Recupera dominio → lista clienti dal customer_source
    import json as _json
    domain_to_customers: dict[str, list] = {}
    for c in storage.list_customers_local(tenant_id=current_tenant_id()):
        codcli = str(c.get("codcli") or "").strip()
        if not codcli:
            continue
        try:
            domains = _json.loads(c.get("domains_json") or "[]")
        except (TypeError, ValueError):
            domains = []
        for d in domains:
            d = (d or "").lower().strip()
            if not d:
                continue
            domain_to_customers.setdefault(d, []).append(c)

    enriched = []
    for it in items:
        cs = domain_to_customers.get(it["domain"], [])
        enriched.append({
            **it,
            "customers": cs,
        })

    # Aggregati per UI
    summary = {
        "total": len(items),
        "auto": sum(1 for r in items if r["strategy"] == "auto"),
        "primary": sum(1 for r in items if r["strategy"] == "primary"),
        "bypass": sum(1 for r in items if r["strategy"] == "bypass"),
    }
    return render_template(
        "admin/domain_strategy_list.html",
        items=enriched,
        summary=summary,
    )


@domain_strategy_bp.route("/<path:domain>/update", methods=["POST"])
@login_required(role="operator")
def update_view(domain: str):
    strategy = (request.form.get("strategy") or "auto").strip()
    primary_codcli = (request.form.get("primary_codcli") or "").strip() or None
    note = (request.form.get("note") or "").strip() or None

    if strategy == "primary" and not primary_codcli:
        flash("Per strategy='primary' serve il codcli del proprietario.", "error")
        return redirect(url_for("domain_strategy.list_view"))

    try:
        _storage().upsert_domain_strategy(
            domain, strategy,
            primary_codcli=primary_codcli, note=note,
            tenant_id=current_tenant_id(),
            set_by=session.get("username") or "ui",
        )
        flash(f"✓ Strategia per {domain}: {strategy}"
              + (f" → codcli {primary_codcli}" if primary_codcli else ""),
              "success")
    except ValueError as exc:
        flash(f"Errore: {exc}", "error")
    return redirect(url_for("domain_strategy.list_view"))
