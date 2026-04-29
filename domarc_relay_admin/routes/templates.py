"""Reply templates CRUD blueprint + upload allegati per auto_reply."""
from __future__ import annotations

import csv
import io
import json
import os
import uuid
from datetime import datetime
from pathlib import Path

from flask import Blueprint, Response, abort, current_app, flash, g, jsonify, redirect, render_template, request, send_file, session, url_for
from werkzeug.utils import secure_filename

from ..auth import login_required

# Limite allegati conforme al piano standalone
MAX_ATTACHMENT_SIZE = 10 * 1024 * 1024  # 10 MiB
MAX_ATTACHMENTS_PER_TEMPLATE = 20
ATTACHMENTS_BASE_DIR = "/var/lib/domarc-smtp-relay-admin/attachments"

templates_bp = Blueprint("templates", __name__)


def _storage():
    return current_app.extensions["domarc_storage"]


def _tid() -> int:
    return int(getattr(g, "current_tenant_id", 1))


@templates_bp.route("/templates")
@login_required()
def list_view():
    tpls = _storage().list_templates(tenant_id=_tid())
    return render_template("admin/templates_list.html", templates=tpls)


@templates_bp.route("/templates/new", methods=["GET", "POST"])
@templates_bp.route("/templates/<int:template_id>", methods=["GET", "POST"])
@login_required(role="operator")
def form_view(template_id: int | None = None):
    is_new = template_id is None
    record: dict = {}
    if not is_new:
        record = _storage().get_template(template_id) or {}
        if not record:
            flash("Template non trovato", "error")
            return redirect(url_for("templates.list_view"))

    if request.method == "POST":
        data = {
            "id": template_id if not is_new else None,
            "name": request.form.get("name"),
            "description": request.form.get("description"),
            "subject_tmpl": request.form.get("subject_tmpl"),
            "body_html_tmpl": request.form.get("body_html_tmpl"),
            "body_text_tmpl": request.form.get("body_text_tmpl"),
            "reply_from_name": request.form.get("reply_from_name"),
            "reply_from_email": request.form.get("reply_from_email"),
            "enabled": request.form.get("enabled") in ("on", "true", "1"),
            "attachment_paths": record.get("attachment_paths") or [],
            "updated_by": session.get("username") or "ui",
        }
        try:
            new_id = _storage().upsert_template(data, tenant_id=_tid())
            flash(f"Template {'creato' if is_new else 'aggiornato'}.", "success")
            return redirect(url_for("templates.form_view", template_id=new_id))
        except ValueError as exc:
            flash(str(exc), "error")

    return render_template("admin/template_form.html", is_new=is_new, record=record)


@templates_bp.route("/templates/<int:template_id>/delete", methods=["POST"])
@login_required(role="admin")
def delete_view(template_id: int):
    _storage().delete_template(template_id)
    flash("Template eliminato.", "success")
    return redirect(url_for("templates.list_view"))


@templates_bp.route("/templates/export.<fmt>")
@login_required()
def export_view(fmt: str):
    if fmt not in ("csv", "json"):
        abort(404)
    rows = _storage().list_templates(tenant_id=_tid())
    fname_ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    if fmt == "json":
        return jsonify({"tenant_id": _tid(), "count": len(rows), "templates": rows})
    buf = io.StringIO()
    if rows:
        flat = []
        for r in rows:
            row = dict(r)
            atts = row.get("attachment_paths")
            if isinstance(atts, list):
                row["attachment_paths"] = json.dumps(atts, ensure_ascii=False)
            flat.append(row)
        keys = sorted({k for r in flat for k in r.keys()})
        writer = csv.DictWriter(buf, fieldnames=keys)
        writer.writeheader()
        for r in flat:
            writer.writerow({k: ("" if r.get(k) is None else str(r.get(k))) for k in keys})
    return Response(
        buf.getvalue(), mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition":
                 f"attachment; filename=domarc-templates-tenant{_tid()}-{fname_ts}.csv"},
    )


def _template_dir(template_id: int) -> Path:
    p = Path(ATTACHMENTS_BASE_DIR) / f"tpl_{template_id}"
    p.mkdir(parents=True, exist_ok=True)
    return p


@templates_bp.route("/templates/<int:template_id>/attachments/upload", methods=["POST"])
@login_required(role="operator")
def attachment_upload(template_id: int):
    record = _storage().get_template(template_id)
    if not record:
        return jsonify({"ok": False, "error": "Template non trovato"}), 404
    if "file" not in request.files:
        return jsonify({"ok": False, "error": "Nessun file"}), 400
    f = request.files["file"]
    if not f.filename:
        return jsonify({"ok": False, "error": "Filename vuoto"}), 400
    # Limite numero
    existing = record.get("attachment_paths") or []
    if len(existing) >= MAX_ATTACHMENTS_PER_TEMPLATE:
        return jsonify({"ok": False, "error": f"Massimo {MAX_ATTACHMENTS_PER_TEMPLATE} allegati per template"}), 400
    # Salva
    safe_name = secure_filename(f.filename) or "file"
    stored_filename = f"{uuid.uuid4().hex}_{safe_name}"
    target = _template_dir(template_id) / stored_filename
    # Lettura/scrittura con check size
    data = f.read(MAX_ATTACHMENT_SIZE + 1)
    if len(data) > MAX_ATTACHMENT_SIZE:
        return jsonify({"ok": False, "error": f"File >{MAX_ATTACHMENT_SIZE // 1024 // 1024} MiB"}), 400
    with open(target, "wb") as out:
        out.write(data)
    # Aggiorna lista allegati
    new_attachment = {
        "filename": stored_filename,
        "original_name": safe_name,
        "size": len(data),
        "mimetype": f.mimetype or "application/octet-stream",
    }
    updated = list(existing) + [new_attachment]
    record["attachment_paths"] = updated
    _storage().upsert_template(record, tenant_id=record.get("tenant_id") or _tid())
    return jsonify({"ok": True, "attachment": new_attachment})


@templates_bp.route("/templates/<int:template_id>/attachments/<filename>", methods=["GET", "DELETE"])
@login_required()
def attachment_handler(template_id: int, filename: str):
    record = _storage().get_template(template_id)
    if not record:
        abort(404)
    safe = secure_filename(filename)
    target = _template_dir(template_id) / safe
    existing = list(record.get("attachment_paths") or [])
    matched = next((a for a in existing if a.get("filename") == safe), None)
    if request.method == "DELETE":
        if not matched:
            return jsonify({"ok": False, "error": "Allegato non trovato"}), 404
        try:
            target.unlink(missing_ok=True)
        except Exception:
            pass
        record["attachment_paths"] = [a for a in existing if a.get("filename") != safe]
        _storage().upsert_template(record, tenant_id=record.get("tenant_id") or _tid())
        return jsonify({"ok": True})
    # GET = download
    if not matched or not target.exists():
        abort(404)
    return send_file(str(target), as_attachment=True,
                     download_name=matched.get("original_name") or safe,
                     mimetype=matched.get("mimetype") or "application/octet-stream")
