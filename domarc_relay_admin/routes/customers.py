"""Anagrafica clienti — vista da CustomerSource configurato.

Mostra clienti + domini + alias + stato profilo orari + eccezioni attive.
Permette filtri avanzati con AND/OR/NOT su:
- testo (nome/codcli/dominio/alias)
- profilo orario (STD/EXT/H24/...)
- stato contratto (active/inactive)
- tipo contratto (STD/ADV/...)
- abilitazione (enabled — flag operativo)

Bulk action: selezione multipla → crea nuovo gruppo / aggiungi a esistente.
"""
from __future__ import annotations

from datetime import date as _date

from flask import (Blueprint, abort, current_app, flash, g, jsonify, redirect,
                   render_template, request, session, url_for)

from ..auth import login_required

customers_bp = Blueprint("customers", __name__)


def _storage():
    return current_app.extensions["domarc_storage"]


def _tid() -> int:
    return int(getattr(g, "current_tenant_id", 1))


def _norm_profile(p: str | None) -> str:
    """Normalizza profilo orario per matching coerente."""
    p = (p or "").strip().upper()
    if p == "STANDARD":
        return "STD"
    return p


def _matches_filter_set(haystack: str | None, terms: list[str], mode: str) -> bool:
    """Match testo con AND/OR/NOT.
    - mode == 'AND': tutti i terms devono essere presenti
    - mode == 'OR':  almeno uno
    - mode == 'NOT': nessuno
    """
    if not terms:
        return True
    h = (haystack or "").lower()
    if mode == "AND":
        return all(t in h for t in terms)
    if mode == "NOT":
        return all(t not in h for t in terms)
    return any(t in h for t in terms)  # OR (default)


@customers_bp.route("/customers")
@login_required()
def list_view():
    cs = current_app.extensions["domarc_customer_source"]
    storage = _storage()

    # === Parsing filtri ===
    # Testo libero (multi-token, separato da spazi/virgola, case-insensitive)
    q_raw = (request.args.get("q") or "").strip()
    q_mode = (request.args.get("q_mode") or "OR").upper()
    if q_mode not in ("AND", "OR", "NOT"):
        q_mode = "OR"
    q_terms = [t.lower() for t in q_raw.replace(",", " ").split() if t.strip()]

    # Profilo orario (multiple, separati da virgola)
    profile_raw = (request.args.get("profile") or "").strip()
    profile_mode = (request.args.get("profile_mode") or "OR").upper()
    if profile_mode not in ("OR", "NOT"):
        profile_mode = "OR"
    profile_terms = [_norm_profile(p) for p in profile_raw.replace(",", " ").split() if p.strip()]

    # Stato contratto (active/inactive/any)
    contract_filter = (request.args.get("contract") or "").strip().lower()

    # Tipo contratto (STD/ADV/...)
    ctype_raw = (request.args.get("ctype") or "").strip()
    ctype_mode = (request.args.get("ctype_mode") or "OR").upper()
    if ctype_mode not in ("OR", "NOT"):
        ctype_mode = "OR"
    ctype_terms = [t.upper() for t in ctype_raw.replace(",", " ").split() if t.strip()]

    # Filtro per gruppo già assegnato
    group_filter = (request.args.get("group") or "").strip()
    group_mode = (request.args.get("group_mode") or "IN").upper()  # IN / NOT_IN

    customers = cs.list_customers()
    all_customers = list(customers)

    # === Lookup ausiliari per filtri ===
    # Mappa codcli -> contract_type (dalla cache postgres se disponibile)
    contract_type_by_codcli: dict[str, str] = {}
    try:
        with storage._connect() as conn:
            rows = conn.execute(
                "SELECT codcli, contract_type FROM customers_pg_cache "
                "WHERE contract_type IS NOT NULL"
            ).fetchall()
            for r in rows:
                contract_type_by_codcli[r["codcli"]] = (r["contract_type"] or "").upper()
    except Exception:  # noqa: BLE001
        pass

    # Mappa codcli -> set di group_id (per filtro gruppo)
    groups_by_codcli: dict[str, list[dict]] = {}
    group_ids_by_codcli: dict[str, set[int]] = {}
    try:
        with storage._connect() as conn:
            for row in conn.execute(
                """SELECT m.codice_cliente, g.id, g.code, g.name, g.color
                     FROM customer_group_members m
                     JOIN customer_groups g ON g.id = m.group_id
                    WHERE g.tenant_id = ? AND g.enabled = 1
                    ORDER BY g.name COLLATE NOCASE""",
                (_tid(),),
            ).fetchall():
                groups_by_codcli.setdefault(row["codice_cliente"], []).append(dict(row))
                group_ids_by_codcli.setdefault(row["codice_cliente"], set()).add(int(row["id"]))
    except Exception:  # noqa: BLE001
        pass

    # === Applica filtri in cascade ===
    def _passes(c) -> bool:
        # Testo: cerca in codcli, ragione_sociale, domini, aliases
        if q_terms:
            haystack = " ".join([
                (c.codice_cliente or "").lower(),
                (c.ragione_sociale or "").lower(),
                " ".join([d.lower() for d in (c.domains or [])]),
                " ".join([a.lower() for a in (c.aliases or [])]),
            ])
            if not _matches_filter_set(haystack, q_terms, q_mode):
                return False

        # Profilo
        if profile_terms:
            cust_profile = _norm_profile(c.tipologia_servizio)
            if profile_mode == "NOT":
                if cust_profile in profile_terms:
                    return False
            else:  # OR
                if cust_profile not in profile_terms:
                    return False

        # Contract active/inactive
        if contract_filter == "active":
            if not c.contract_active:
                return False
        elif contract_filter == "inactive":
            if c.contract_active:
                return False

        # Tipo contratto
        if ctype_terms:
            cust_ctype = contract_type_by_codcli.get(c.codice_cliente, "")
            if ctype_mode == "NOT":
                if cust_ctype in ctype_terms:
                    return False
            else:
                if cust_ctype not in ctype_terms:
                    return False

        # Gruppo
        if group_filter:
            try:
                gid = int(group_filter)
                in_group = gid in group_ids_by_codcli.get(c.codice_cliente, set())
                if group_mode == "NOT_IN" and in_group:
                    return False
                if group_mode == "IN" and not in_group:
                    return False
            except (TypeError, ValueError):
                pass

        return True

    customers = [c for c in customers if _passes(c)]

    # === Stats per UI ===
    by_profile: dict[str, int] = {}
    by_ctype: dict[str, int] = {}
    for c in all_customers:
        p = _norm_profile(c.tipologia_servizio) or "—"
        by_profile[p] = by_profile.get(p, 0) + 1
        ct = contract_type_by_codcli.get(c.codice_cliente, "") or "—"
        by_ctype[ct] = by_ctype.get(ct, 0) + 1

    # Tutti i ctype distinti per dropdown
    all_ctypes = sorted({v for v in contract_type_by_codcli.values() if v})

    # Eccezioni schedule attive oggi
    today = _date.today().isoformat()
    exceptions_today_by_codcli: dict[str, int] = {}
    sh_records_by_codcli: dict[str, dict] = {}
    try:
        sh_rows = storage.list_service_hours(tenant_id=_tid())
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

    # Lista gruppi per dropdown filtro + bulk modal
    all_groups = []
    try:
        all_groups = storage.list_customer_groups(tenant_id=_tid(), only_enabled=True)
    except Exception:  # noqa: BLE001
        pass

    health = cs.health()
    return render_template(
        "admin/customers_list.html",
        customers=customers,
        all_count=len(all_customers),
        filtered_count=len(customers),
        by_profile=by_profile,
        by_ctype=by_ctype,
        all_ctypes=all_ctypes,
        contract_type_by_codcli=contract_type_by_codcli,
        contract_active_count=sum(1 for c in all_customers if c.contract_active),
        contract_inactive_count=sum(1 for c in all_customers if not c.contract_active),
        sh_records=sh_records_by_codcli,
        exceptions_today=exceptions_today_by_codcli,
        groups_by_codcli=groups_by_codcli,
        all_groups=all_groups,
        health=health,
        # echo dei filtri per persistenza tra request
        search=q_raw, q_mode=q_mode,
        profile_filter=profile_raw, profile_mode=profile_mode,
        contract_filter=contract_filter,
        ctype_filter=ctype_raw, ctype_mode=ctype_mode,
        group_filter=group_filter, group_mode=group_mode,
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


# === Bulk action: gestione gruppi su selezione multipla ==================

@customers_bp.route("/customers/bulk/add-to-group", methods=["POST"])
@login_required(role="operator")
def bulk_add_to_group():
    """Aggiunge i codcli selezionati a un gruppo esistente."""
    storage = _storage()
    codcli_list = request.form.getlist("codcli")
    group_id = request.form.get("group_id")
    if not codcli_list:
        flash("Nessun cliente selezionato.", "warning")
        return redirect(url_for("customers.list_view"))
    if not group_id:
        flash("Seleziona un gruppo destinazione.", "error")
        return redirect(url_for("customers.list_view"))

    actor = session.get("username") or "?"
    added = 0
    for codcli in codcli_list:
        codcli = (codcli or "").strip().upper()
        if not codcli:
            continue
        try:
            with storage.transaction() as conn:
                conn.execute(
                    """INSERT OR IGNORE INTO customer_group_members
                           (tenant_id, group_id, codice_cliente, added_by)
                       VALUES (?, ?, ?, ?)""",
                    (_tid(), int(group_id), codcli, actor),
                )
            added += 1
        except Exception:  # noqa: BLE001
            pass

    group = storage.get_customer_group(int(group_id))
    flash(f"✓ {added} clienti aggiunti al gruppo «{(group or {}).get('name') or group_id}».",
          "success")
    return redirect(url_for("customers.list_view"))


@customers_bp.route("/customers/bulk/create-group", methods=["POST"])
@login_required(role="operator")
def bulk_create_group():
    """Crea un nuovo gruppo dalla selezione di codcli."""
    storage = _storage()
    codcli_list = request.form.getlist("codcli")
    code = (request.form.get("group_code") or "").strip()
    name = (request.form.get("group_name") or "").strip()
    description = (request.form.get("group_description") or "").strip() or None
    color = (request.form.get("group_color") or "").strip() or None

    if not codcli_list:
        flash("Nessun cliente selezionato.", "warning")
        return redirect(url_for("customers.list_view"))
    if not code or not name:
        flash("Codice e nome del gruppo obbligatori.", "error")
        return redirect(url_for("customers.list_view"))

    actor = session.get("username") or "?"
    try:
        gid = storage.upsert_customer_group(
            tenant_id=_tid(),
            code=code, name=name,
            description=description, color=color,
            enabled=True, actor=actor,
        )
    except ValueError as exc:
        flash(f"Errore creazione gruppo: {exc}", "error")
        return redirect(url_for("customers.list_view"))

    # Aggiungi tutti i codcli al nuovo gruppo
    added = 0
    for codcli in codcli_list:
        codcli = (codcli or "").strip().upper()
        if not codcli:
            continue
        try:
            with storage.transaction() as conn:
                conn.execute(
                    """INSERT OR IGNORE INTO customer_group_members
                           (tenant_id, group_id, codice_cliente, added_by)
                       VALUES (?, ?, ?, ?)""",
                    (_tid(), int(gid), codcli, actor),
                )
            added += 1
        except Exception:  # noqa: BLE001
            pass

    flash(f"✓ Gruppo «{name}» creato con {added} membri.", "success")
    return redirect(url_for("customer_groups.detail_view", group_id=gid))
