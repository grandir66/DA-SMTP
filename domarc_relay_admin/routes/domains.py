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
    # Filtri da query string
    filter_active = (request.args.get("filter") or "all").lower()  # all | with_active | only_active | strategy_*
    strategy_filter = request.args.get("strategy")  # auto | primary | bypass | None

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
        n_act = sum(1 for c in cs if c.get("contract_active"))
        # In modalità only_active filtriamo i clienti del breakdown a soli attivi
        if filter_active == "only_active":
            cs_view = [c for c in cs if c.get("contract_active")]
        else:
            cs_view = cs
        enriched.append({
            **it,
            "customers": cs_view,
            "n_active_real": n_act,  # ricalcolato live (snapshot al sync)
        })

    # Aggregati per UI sul totale (NON filtrato), per i counter in cima
    summary = {
        "total": len(items),
        "with_active": sum(1 for r in enriched if r["n_active_real"] > 0),
        "only_inactive": sum(1 for r in enriched if r["n_active_real"] == 0),
        "auto": sum(1 for r in items if r["strategy"] == "auto"),
        "primary": sum(1 for r in items if r["strategy"] == "primary"),
        "bypass": sum(1 for r in items if r["strategy"] == "bypass"),
    }

    # Applica filtri
    filtered = enriched
    if filter_active == "with_active":
        filtered = [r for r in filtered if r["n_active_real"] > 0]
    elif filter_active == "only_active":
        # Nasconde i domini SENZA alcun cliente attivo (i breakdown già limitati sopra)
        filtered = [r for r in filtered if r["n_active_real"] > 0]
    elif filter_active == "only_inactive":
        filtered = [r for r in filtered if r["n_active_real"] == 0]
    if strategy_filter in ("auto", "primary", "bypass"):
        filtered = [r for r in filtered if r["strategy"] == strategy_filter]

    return render_template(
        "admin/domain_strategy_list.html",
        items=filtered,
        summary=summary,
        filter_active=filter_active,
        strategy_filter=strategy_filter,
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
