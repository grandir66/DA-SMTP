"""Profili orari (built-in canonici + custom per tenant)."""
from __future__ import annotations

import json

from flask import Blueprint, current_app, flash, g, redirect, render_template, request, session, url_for

from ..auth import login_required

profiles_bp = Blueprint("profiles", __name__)


def _storage():
    return current_app.extensions["domarc_storage"]


def _tid() -> int:
    return int(getattr(g, "current_tenant_id", 1))


@profiles_bp.route("/profiles")
@login_required()
def list_view():
    profiles = _storage().list_profiles(tenant_id=_tid())
    builtin = [p for p in profiles if p.get("is_builtin")]
    custom = [p for p in profiles if not p.get("is_builtin")]
    return render_template(
        "admin/profiles_list.html",
        profiles=profiles, builtin=builtin, custom=custom,
    )


@profiles_bp.route("/profiles/new", methods=["GET", "POST"])
@profiles_bp.route("/profiles/<int:profile_id>", methods=["GET", "POST"])
@login_required(role="operator")
def form_view(profile_id: int | None = None):
    is_new = profile_id is None
    record: dict = {}
    if not is_new:
        record = _storage().get_profile(profile_id) or {}
        if not record:
            flash("Profilo non trovato", "error")
            return redirect(url_for("profiles.list_view"))
    # I built-in sono modificabili (orari/festività): solo l'eliminazione è bloccata.
    is_builtin = bool(record.get("is_builtin"))

    if request.method == "POST":
        try:
            schedule = json.loads(request.form.get("schedule_json") or "{}")
            holidays = json.loads(request.form.get("holidays_json") or "[]")
        except json.JSONDecodeError as exc:
            flash(f"JSON invalido: {exc}", "error")
            return render_template("admin/profile_form.html", is_new=is_new, record=record)
        data = {
            "code": (request.form.get("code") or "").strip().upper() or (record.get("code") if is_builtin else None),
            "name": request.form.get("name") or record.get("name"),
            "description": request.form.get("description"),
            "details": request.form.get("details"),
            "schedule": schedule,
            "holidays": holidays,
            "holidays_auto": request.form.get("holidays_auto") in ("on", "true", "1"),
            "is_builtin": is_builtin,  # mantieni il flag built-in se lo era
            "enabled": request.form.get("enabled") in ("on", "true", "1"),
            "exclude_holidays": request.form.get("exclude_holidays") in ("on", "true", "1"),
            "requires_authorization_always": request.form.get("requires_authorization_always") in ("on", "true", "1"),
            "authorize_outside_hours": request.form.get("authorize_outside_hours") in ("on", "true", "1"),
            "timezone": request.form.get("timezone") or "Europe/Rome",
            "updated_by": session.get("username") or "ui",
        }
        if not is_new:
            data["id"] = profile_id
        try:
            new_id = _storage().upsert_profile(data, tenant_id=record.get("tenant_id") if not is_new else _tid())
            flash(f"Profilo {'creato' if is_new else 'aggiornato'}.", "success")
            return redirect(url_for("profiles.form_view", profile_id=new_id))
        except ValueError as exc:
            flash(str(exc), "error")

    return render_template("admin/profile_form.html", is_new=is_new, record=record, is_builtin=is_builtin)


@profiles_bp.route("/profiles/<int:profile_id>/delete", methods=["POST"])
@login_required(role="admin")
def delete_view(profile_id: int):
    try:
        _storage().delete_profile(profile_id)
        flash("Profilo eliminato.", "success")
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(url_for("profiles.list_view"))


@profiles_bp.route("/profiles/refresh-holidays", methods=["POST"])
@login_required(role="admin")
def refresh_holidays():
    """Ricalcola le festività italiane (anno corrente + prossimo) e aggiorna
    tutti i profili con holidays_auto=1.
    """
    res = _storage().refresh_holidays_italian()
    flash(
        f"Festività italiane ricalcolate ({res['holidays_count']} date, anno {res['year']}). "
        f"Aggiornati {res['updated_profiles']} profili con holidays_auto=ON.",
        "success",
    )
    return redirect(url_for("profiles.list_view"))
