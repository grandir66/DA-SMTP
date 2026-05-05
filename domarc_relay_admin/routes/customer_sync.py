"""UI customer-sync: gestione sorgenti esterne agnostiche per la tabella
clienti autoritativa (M028).

Pagine:
- GET  /customer-sync/                   lista sorgenti + stato + next run
- GET  /customer-sync/new                wizard nuovo (con kind selector)
- POST /customer-sync/new                crea sorgente
- GET  /customer-sync/<id>               edit form
- POST /customer-sync/<id>               update sorgente
- POST /customer-sync/<id>/delete        elimina sorgente
- POST /customer-sync/<id>/toggle        enable/disable
- POST /customer-sync/<id>/test          test connessione + describe schema
- POST /customer-sync/<id>/run           run sync (con flag dry_run)
- GET  /customer-sync/<id>/runs          storico run
- GET  /customer-sync/runs/<run_id>      dettaglio run (con report dry-run)
"""
from __future__ import annotations

import json
import logging
import threading
from typing import Any

from flask import (Blueprint, abort, current_app, flash, g, jsonify,
                   redirect, render_template, request, session, url_for)

from ..auth import login_required
from ..customer_sync import PROVIDER_KINDS, get_provider
from ..customer_sync.engine import SyncEngine
from ..customer_sync import mapper as _mapper

logger = logging.getLogger(__name__)

customer_sync_bp = Blueprint("customer_sync", __name__,
                              url_prefix="/customer-sync")


def _storage():
    return current_app.extensions["domarc_storage"]


def _tid() -> int:
    return int(getattr(g, "current_tenant_id", 1))


def _actor() -> str:
    return session.get("username") or "?"


# ============================================================ Lista =====

@customer_sync_bp.route("/")
@login_required()
def list_view():
    storage = _storage()
    sources = storage.list_customer_sync_sources(tenant_id=_tid())
    # Per ogni sorgente, conta quanti clienti vengono da li'
    counts: dict[int, int] = {}
    last_runs: dict[int, dict[str, Any]] = {}
    for s in sources:
        runs = storage.list_customer_sync_runs(source_id=s["id"], limit=1)
        if runs:
            last_runs[s["id"]] = runs[0]
        codclis = storage.list_customer_codclis_for_source(s["id"])
        counts[s["id"]] = len(codclis)
    return render_template(
        "admin/customer_sync_list.html",
        sources=sources, counts=counts, last_runs=last_runs,
        kinds=PROVIDER_KINDS, canonical_targets=_mapper.CANONICAL_TARGETS,
    )


# ============================================================ Nuovo =====

@customer_sync_bp.route("/new", methods=["GET", "POST"])
@login_required(role="operator")
def new_view():
    if request.method == "POST":
        return _save_source(source_id=None)
    return render_template(
        "admin/customer_sync_form.html",
        source=None, kinds=PROVIDER_KINDS,
        canonical_targets=_mapper.CANONICAL_TARGETS,
        canonical_targets_info=_mapper.CANONICAL_TARGETS_INFO,
        on_missing_options=("flag", "delete", "keep"),
    )


# ============================================================ Edit ======

@customer_sync_bp.route("/<int:source_id>", methods=["GET", "POST"])
@login_required(role="operator")
def edit_view(source_id: int):
    storage = _storage()
    source = storage.get_customer_sync_source(source_id)
    if not source:
        abort(404)
    if request.method == "POST":
        return _save_source(source_id=source_id)
    return render_template(
        "admin/customer_sync_form.html",
        source=source, kinds=PROVIDER_KINDS,
        canonical_targets=_mapper.CANONICAL_TARGETS,
        canonical_targets_info=_mapper.CANONICAL_TARGETS_INFO,
        on_missing_options=("flag", "delete", "keep"),
    )


def _save_source(*, source_id: int | None):
    """Comune a new e edit: parsa il form e salva."""
    form = request.form
    name = (form.get("name") or "").strip()
    kind = (form.get("kind") or "").strip()
    if not name:
        flash("Il nome e' obbligatorio.", "error")
        return redirect(request.referrer or url_for("customer_sync.list_view"))
    if kind not in PROVIDER_KINDS:
        flash(f"Tipo provider non supportato: {kind}", "error")
        return redirect(request.referrer or url_for("customer_sync.list_view"))

    config_json = form.get("config_json") or "{}"
    try:
        config_dict = json.loads(config_json)
    except (TypeError, ValueError) as exc:
        flash(f"Config JSON non valido: {exc}", "error")
        return redirect(request.referrer or url_for("customer_sync.list_view"))

    mapping_json = form.get("mapping_json") or "{}"
    try:
        mapping_dict = json.loads(mapping_json)
    except (TypeError, ValueError) as exc:
        flash(f"Mapping JSON non valido: {exc}", "error")
        return redirect(request.referrer or url_for("customer_sync.list_view"))

    data = {
        "id": source_id,
        "name": name,
        "kind": kind,
        "enabled": form.get("enabled") == "1",
        "config_json": config_dict,
        "query_or_path": (form.get("query_or_path") or None),
        "mapping_json": mapping_dict,
        "schedule_hours": int(form.get("schedule_hours") or 24),
        "on_missing": form.get("on_missing") or "flag",
        "created_by": _actor(),
    }
    sid = _storage().upsert_customer_sync_source(data, tenant_id=_tid())
    flash(f"Sorgente {'aggiornata' if source_id else 'creata'} (id {sid}).", "success")
    return redirect(url_for("customer_sync.edit_view", source_id=sid))


# ============================================================ Delete ====

@customer_sync_bp.route("/<int:source_id>/delete", methods=["POST"])
@login_required(role="operator")
def delete_view(source_id: int):
    storage = _storage()
    source = storage.get_customer_sync_source(source_id)
    if not source:
        abort(404)
    storage.delete_customer_sync_source(source_id)
    flash(f"Sorgente '{source['name']}' eliminata. I clienti gia' sincronizzati "
          f"restano in tabella.", "success")
    return redirect(url_for("customer_sync.list_view"))


@customer_sync_bp.route("/<int:source_id>/toggle", methods=["POST"])
@login_required(role="operator")
def toggle_view(source_id: int):
    storage = _storage()
    source = storage.get_customer_sync_source(source_id)
    if not source:
        abort(404)
    new_state = storage.toggle_customer_sync_source(source_id)
    flash(f"Sorgente '{source['name']}' {'abilitata' if new_state else 'disabilitata'}.",
          "success")
    return redirect(url_for("customer_sync.list_view"))


# ============================================================ Test ======

@customer_sync_bp.route("/<int:source_id>/test", methods=["POST"])
@login_required(role="operator")
def test_view(source_id: int):
    storage = _storage()
    source = storage.get_customer_sync_source(source_id)
    if not source:
        abort(404)
    try:
        provider = get_provider(
            source["kind"],
            config=source.get("config_json") or {},
            query_or_path=source.get("query_or_path"),
            storage=storage,
        )
        result = provider.test_connection()
        schema = provider.describe_schema()
        result["schema"] = schema
        return jsonify(result)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Test connessione fallito: %s", exc)
        return jsonify({"ok": False, "message": str(exc)[:500]}), 200


# ============================================================ Run =======

@customer_sync_bp.route("/<int:source_id>/run", methods=["POST"])
@login_required(role="operator")
def run_view(source_id: int):
    storage = _storage()
    source = storage.get_customer_sync_source(source_id)
    if not source:
        abort(404)
    dry_run = request.form.get("dry_run") == "1"
    background = request.form.get("background") == "1"

    if dry_run or not background:
        # Esegui sincrono, blocca la richiesta finche' completa
        engine = SyncEngine(storage)
        report = engine.run(source, triggered_by=f"manual:{_actor()}",
                            dry_run=dry_run)
        run_id = report.get("run_id")
        if dry_run:
            return redirect(url_for("customer_sync.run_detail", run_id=run_id))
        flash(f"Run completato: status={report.get('status')}, "
              f"insert={report.get('n_inserted')}, "
              f"update={report.get('n_updated')}, "
              f"errori={report.get('n_errored')}", "success")
        return redirect(url_for("customer_sync.runs_view", source_id=source_id))

    # Background: lancia thread, ritorna subito
    def _bg():
        engine = SyncEngine(storage)
        try:
            engine.run(source, triggered_by=f"manual:{_actor()}", dry_run=False)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Run background fallito: %s", exc)

    threading.Thread(target=_bg, name=f"sync-manual-{source_id}",
                     daemon=True).start()
    flash("Run avviato in background. Controlla lo storico tra qualche secondo.",
          "info")
    return redirect(url_for("customer_sync.runs_view", source_id=source_id))


# ============================================================ Runs ======

@customer_sync_bp.route("/<int:source_id>/runs")
@login_required()
def runs_view(source_id: int):
    storage = _storage()
    source = storage.get_customer_sync_source(source_id)
    if not source:
        abort(404)
    runs = storage.list_customer_sync_runs(source_id=source_id, limit=100)
    return render_template(
        "admin/customer_sync_runs.html",
        source=source, runs=runs,
    )


@customer_sync_bp.route("/runs/<int:run_id>")
@login_required()
def run_detail(run_id: int):
    storage = _storage()
    run = storage.get_customer_sync_run(run_id)
    if not run:
        abort(404)
    source = storage.get_customer_sync_source(run["source_id"])
    return render_template(
        "admin/customer_sync_run_detail.html",
        run=run, source=source,
    )
