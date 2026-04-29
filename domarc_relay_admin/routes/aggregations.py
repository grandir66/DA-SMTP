"""Error aggregations + occurrences viewer."""
from __future__ import annotations

import re

from flask import Blueprint, abort, current_app, flash, g, jsonify, redirect, render_template, request, session, url_for

from ..auth import login_required

aggregations_bp = Blueprint("aggregations", __name__)


def _storage():
    return current_app.extensions["domarc_storage"]


def _tid() -> int:
    return int(getattr(g, "current_tenant_id", 1))


@aggregations_bp.route("/aggregations")
@login_required()
def list_view():
    state = (request.args.get("state") or "all").lower()
    only_enabled = True if state == "enabled" else (False if state == "disabled" else None)
    aggs = _storage().list_aggregations(tenant_id=_tid(), only_enabled=only_enabled)
    return render_template("admin/aggregations_list.html",
                           aggregations=aggs, filter_state=state)


@aggregations_bp.route("/aggregations/new", methods=["GET", "POST"])
@aggregations_bp.route("/aggregations/<int:agg_id>", methods=["GET", "POST"])
@login_required(role="operator")
def form_view(agg_id: int | None = None):
    is_new = agg_id is None
    record: dict = {}
    if not is_new:
        record = _storage().get_aggregation(agg_id) or {}
        if not record:
            flash("Aggregation non trovata", "error")
            return redirect(url_for("aggregations.list_view"))

    if request.method == "POST":
        data = {
            "id": agg_id if not is_new else None,
            "name": request.form.get("name"),
            "description": request.form.get("description"),
            "match_from_regex": request.form.get("match_from_regex"),
            "match_subject_regex": request.form.get("match_subject_regex"),
            "match_body_regex": request.form.get("match_body_regex"),
            "fingerprint_template": request.form.get("fingerprint_template"),
            "threshold": request.form.get("threshold") or 2,
            "consecutive_only": request.form.get("consecutive_only") in ("on", "true", "1"),
            "window_hours": request.form.get("window_hours") or 24,
            "reset_subject_regex": request.form.get("reset_subject_regex"),
            "reset_from_regex": request.form.get("reset_from_regex"),
            "ticket_settore": request.form.get("ticket_settore"),
            "ticket_urgenza": request.form.get("ticket_urgenza"),
            "ticket_codice_cliente": request.form.get("ticket_codice_cliente"),
            "enabled": request.form.get("enabled") in ("on", "true", "1"),
            "priority": request.form.get("priority") or 100,
            "created_by": session.get("username") or "ui",
        }
        try:
            new_id = _storage().upsert_aggregation(data, tenant_id=_tid())
            flash(f"Aggregation {'creata' if is_new else 'aggiornata'}.", "success")
            return redirect(url_for("aggregations.form_view", agg_id=new_id))
        except ValueError as exc:
            flash(str(exc), "error")
            record = {**record, **data}
    return render_template("admin/aggregation_form.html",
                           is_new=is_new, record=record)


@aggregations_bp.route("/aggregations/test-regex", methods=["POST"])
@login_required(role="operator")
def test_regex():
    payload = request.get_json(silent=True) or {}
    pattern = (payload.get("pattern") or "").strip()
    sample = payload.get("sample") or ""
    if not pattern:
        return jsonify({"ok": False, "error": "Pattern vuoto"})
    try:
        m = re.search(pattern, sample[:16384], re.IGNORECASE)
    except re.error as exc:
        return jsonify({"ok": False, "error": f"Regex invalida: {exc}"})
    if not m:
        return jsonify({"ok": True, "matched": False})
    return jsonify({"ok": True, "matched": True, "match": m.group(0),
                    "groups": list(m.groups())})


@aggregations_bp.route("/aggregations/test-fingerprint", methods=["POST"])
@login_required(role="operator")
def test_fingerprint():
    """Calcola fingerprint da un sample (subject/from) usando il template fornito."""
    payload = request.get_json(silent=True) or {}
    template = (payload.get("template") or "").strip() or "${from}|${subject_normalized}"
    sample = payload.get("sample") or {}
    fr = (sample.get("from") or "").strip().lower()
    subj = (sample.get("subject") or "").strip()
    subj_norm = re.sub(r"\s+", " ", re.sub(r"[\d\W_]+", " ", subj)).strip().lower()
    fp = template.replace("${from}", fr).replace("${subject_normalized}", subj_norm).replace("${subject}", subj)
    return jsonify({"ok": True, "fingerprint": fp})


@aggregations_bp.route("/aggregations/<int:agg_id>/delete", methods=["POST"])
@login_required(role="admin")
def delete_view(agg_id: int):
    _storage().delete_aggregation(agg_id)
    flash("Aggregation eliminata.", "success")
    return redirect(url_for("aggregations.list_view"))


@aggregations_bp.route("/aggregations/<int:agg_id>/toggle", methods=["POST"])
@login_required(role="operator")
def toggle_view(agg_id: int):
    _storage().toggle_aggregation(agg_id)
    return redirect(request.referrer or url_for("aggregations.list_view"))


@aggregations_bp.route("/aggregations/<int:agg_id>/duplicate", methods=["POST"])
@login_required(role="operator")
def duplicate_view(agg_id: int):
    src = _storage().get_aggregation(agg_id)
    if not src:
        flash("Aggregation non trovata", "error")
        return redirect(url_for("aggregations.list_view"))
    data = {k: v for k, v in src.items() if k not in ("id", "created_at", "created_by")}
    data["name"] = (src.get("name") or "agg") + " (copia)"
    data["created_by"] = session.get("username") or "ui"
    try:
        new_id = _storage().upsert_aggregation(data, tenant_id=src.get("tenant_id") or _tid())
        flash(f"Aggregation duplicata (id={new_id}).", "success")
        return redirect(url_for("aggregations.form_view", agg_id=new_id))
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("aggregations.list_view"))


@aggregations_bp.route("/occurrences/<int:occ_id>/reset", methods=["POST"])
@login_required(role="operator")
def occurrence_reset(occ_id: int):
    _storage().reset_occurrence(occ_id)
    flash("Occurrence resettata.", "success")
    return redirect(request.referrer or url_for("aggregations.occurrences_view"))


@aggregations_bp.route("/occurrences/<int:occ_id>/delete", methods=["POST"])
@login_required(role="admin")
def occurrence_delete(occ_id: int):
    _storage().delete_occurrence(occ_id)
    flash("Occurrence eliminata.", "success")
    return redirect(request.referrer or url_for("aggregations.occurrences_view"))


@aggregations_bp.route("/occurrences/reset-all", methods=["POST"])
@login_required(role="admin")
def occurrences_reset_all():
    aggregation_id = request.form.get("aggregation_id")
    n = _storage().reset_all_occurrences(
        tenant_id=_tid(),
        aggregation_id=int(aggregation_id) if aggregation_id else None,
    )
    flash(f"Resettate {n} occurrences.", "success")
    return redirect(request.referrer or url_for("aggregations.occurrences_view"))


@aggregations_bp.route("/aggregations/occurrences")
@login_required()
def occurrences_view():
    agg_id = request.args.get("aggregation_id")
    state = (request.args.get("state") or "active").lower()
    occurrences = _storage().list_occurrences(
        tenant_id=_tid(),
        aggregation_id=int(agg_id) if agg_id else None,
        filter_state=state,
    )
    aggregations = _storage().list_aggregations(tenant_id=_tid())
    return render_template("admin/occurrences_list.html",
                           occurrences=occurrences,
                           aggregations=aggregations,
                           filter_state=state,
                           filter_aggregation_id=int(agg_id) if agg_id else None)
