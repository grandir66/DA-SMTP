"""Service hours CRUD."""
from __future__ import annotations

import json

from flask import Blueprint, current_app, flash, g, redirect, render_template, request, session, url_for

from ..auth import login_required

service_hours_bp = Blueprint("service_hours", __name__)


def _storage():
    return current_app.extensions["domarc_storage"]


def _tid() -> int:
    return int(getattr(g, "current_tenant_id", 1))


@service_hours_bp.route("/service-hours")
@login_required()
def list_view():
    rows = _storage().list_service_hours(tenant_id=_tid(),
                                          search=(request.args.get("q") or None))
    return render_template("admin/service_hours_list.html", rows=rows,
                           search=request.args.get("q") or "")


@service_hours_bp.route("/service-hours/new", methods=["GET", "POST"])
@service_hours_bp.route("/service-hours/<codcli>", methods=["GET", "POST"])
@login_required(role="operator")
def form_view(codcli: str | None = None):
    is_new = codcli is None
    record: dict = {}
    if not is_new:
        record = _storage().get_service_hours(codcli, tenant_id=_tid()) or {}
        if not record:
            flash("Cliente non trovato", "error")
            return redirect(url_for("service_hours.list_view"))
    profiles = _storage().list_profiles(tenant_id=_tid())

    if request.method == "POST":
        try:
            schedule = json.loads(request.form.get("schedule_json") or "{}")
            holidays = json.loads(request.form.get("holidays_json") or "[]")
            exceptions = json.loads(request.form.get("schedule_exceptions_json") or "[]")
        except json.JSONDecodeError as exc:
            flash(f"JSON invalido: {exc}", "error")
            return render_template("admin/service_hours_form.html",
                                   is_new=is_new, record=record, profiles=profiles)

        data = {
            "codice_cliente": request.form.get("codice_cliente") or codcli,
            "profile": request.form.get("profile") or "custom",
            "profile_id": request.form.get("profile_id"),
            "timezone": request.form.get("timezone") or "Europe/Rome",
            "schedule": schedule,
            "holidays": holidays,
            "schedule_exceptions": exceptions,
            "ah_key": request.form.get("ah_key"),
            "notes": request.form.get("notes"),
            "enabled": request.form.get("enabled") in ("on", "true", "1"),
            "updated_by": session.get("username") or "ui",
        }
        try:
            new_codcli = _storage().upsert_service_hours(data, tenant_id=_tid())
            flash(f"Orari salvati per {new_codcli}.", "success")
            return redirect(url_for("service_hours.form_view", codcli=new_codcli))
        except ValueError as exc:
            flash(str(exc), "error")

    return render_template("admin/service_hours_form.html",
                           is_new=is_new, record=record, profiles=profiles)


@service_hours_bp.route("/service-hours/<codcli>/delete", methods=["POST"])
@login_required(role="admin")
def delete_view(codcli: str):
    _storage().delete_service_hours(codcli, tenant_id=_tid())
    flash("Orari eliminati.", "success")
    return redirect(url_for("service_hours.list_view"))
