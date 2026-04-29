"""Queue admin: outbound_queue + quarantine + dispatch_queue del listener.

L'admin web non ha le proprie tabelle di queue (sono nel DB del listener
SMTP separato). Per visualizzarle, leggiamo direttamente il DB del listener
in **read-only** (path tipico ``/var/lib/stormshield-smtp-relay/relay.db``).

Path overridabile via setting ``listener_db_path`` se il listener gira in
locazione non standard.

Pagine:
- ``/queue/`` — vista unificata: outbound + quarantine + dispatch (3 tab).
- ``/queue/outbound/<id>`` — dettaglio singola mail in coda outbound.
- ``/queue/quarantine/<id>`` — dettaglio quarantena.
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from flask import (Blueprint, Response, abort, current_app, flash,
                   render_template, request)

from ..auth import login_required

queue_bp = Blueprint("queue", __name__, url_prefix="/queue")


def _listener_db_path() -> Path:
    """Risolve path al DB del listener.

    Priorità:
    1. setting ``listener_db_path`` se valorizzato.
    2. env var ``LISTENER_DB_PATH``.
    3. default ``/var/lib/stormshield-smtp-relay/relay.db``.
    """
    storage = current_app.extensions["domarc_storage"]
    try:
        for s in storage.list_settings():
            if s["key"] == "listener_db_path" and s["value"]:
                return Path(s["value"])
    except Exception:  # noqa: BLE001
        pass
    return Path(os.environ.get("LISTENER_DB_PATH",
                                "/var/lib/stormshield-smtp-relay/relay.db"))


def _open_listener_db() -> sqlite3.Connection | None:
    """Apre il DB del listener in read-only. Returns None se non accessibile."""
    path = _listener_db_path()
    if not path.exists():
        return None
    try:
        # URI read-only: nessuna scrittura possibile a livello driver
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.OperationalError:
        return None


def _safe_int(value, default: int, *, min_val: int = 0,
              max_val: int = 10**12) -> int:
    try:
        v = int(value) if value not in (None, "") else default
    except (TypeError, ValueError):
        v = default
    return max(min_val, min(max_val, v))


@queue_bp.route("/")
@login_required(role="operator")
def index():
    """Vista unificata delle 3 code del listener."""
    conn = _open_listener_db()
    db_status = {"available": conn is not None,
                  "path": str(_listener_db_path())}

    outbound: list[dict] = []
    quarantine: list[dict] = []
    dispatch: list[dict] = []
    stats = {"outbound": {}, "quarantine_count": 0, "dispatch": {}}

    if conn is not None:
        try:
            # Outbound queue (mail in attesa di delivery)
            for r in conn.execute("""
                SELECT id, event_uuid, action, mail_from, rcpt_to_json,
                       smarthost, smarthost_port, smarthost_tls, state, attempts,
                       next_attempt_at, last_error, delivered_at, created_at,
                       length(mime_blob) AS mime_size
                FROM outbound_queue
                ORDER BY id DESC LIMIT 200
            """):
                d = dict(r)
                try:
                    d["recipients"] = json.loads(d["rcpt_to_json"] or "[]")
                except (TypeError, ValueError):
                    d["recipients"] = []
                outbound.append(d)
            for r in conn.execute("""
                SELECT state, COUNT(*) AS n FROM outbound_queue GROUP BY state
            """):
                stats["outbound"][r["state"]] = r["n"]

            # Quarantine
            for r in conn.execute("""
                SELECT id, event_uuid, reason, from_address, to_address,
                       decision, reviewed_at, notes, created_at,
                       length(mime_blob) AS mime_size
                FROM quarantine
                ORDER BY id DESC LIMIT 200
            """):
                quarantine.append(dict(r))
            stats["quarantine_count"] = conn.execute(
                "SELECT COUNT(*) FROM quarantine"
            ).fetchone()[0]

            # Dispatch (ticket pending)
            for r in conn.execute("""
                SELECT id, event_uuid, state, attempts, next_attempt_at,
                       last_error, manager_response, created_at,
                       length(payload_json) AS payload_size
                FROM dispatch_queue
                ORDER BY id DESC LIMIT 200
            """):
                dispatch.append(dict(r))
            for r in conn.execute("""
                SELECT state, COUNT(*) AS n FROM dispatch_queue GROUP BY state
            """):
                stats["dispatch"][r["state"]] = r["n"]
        except sqlite3.Error as exc:
            db_status["error"] = str(exc)
        finally:
            conn.close()

    return render_template(
        "admin/queue_index.html",
        outbound=outbound,
        quarantine=quarantine,
        dispatch=dispatch,
        stats=stats,
        db_status=db_status,
    )


@queue_bp.route("/outbound/<int:queue_id>")
@login_required(role="operator")
def outbound_detail(queue_id: int):
    conn = _open_listener_db()
    if conn is None:
        abort(503)
    try:
        row = conn.execute("""
            SELECT id, event_uuid, action, mail_from, rcpt_to_json, smarthost,
                   smarthost_port, smarthost_tls, state, attempts, next_attempt_at,
                   last_error, delivered_at, created_at, length(mime_blob) AS mime_size
            FROM outbound_queue WHERE id = ?
        """, (queue_id,)).fetchone()
        if not row:
            abort(404)
        d = dict(row)
        try:
            d["recipients"] = json.loads(d["rcpt_to_json"] or "[]")
        except (TypeError, ValueError):
            d["recipients"] = []
    finally:
        conn.close()
    return render_template("admin/queue_outbound_detail.html", item=d)


@queue_bp.route("/quarantine/<int:queue_id>")
@login_required(role="operator")
def quarantine_detail(queue_id: int):
    conn = _open_listener_db()
    if conn is None:
        abort(503)
    try:
        row = conn.execute("""
            SELECT id, event_uuid, reason, from_address, to_address, decision,
                   reviewed_at, notes, created_at, length(mime_blob) AS mime_size
            FROM quarantine WHERE id = ?
        """, (queue_id,)).fetchone()
        if not row:
            abort(404)
    finally:
        conn.close()
    return render_template("admin/queue_quarantine_detail.html", item=dict(row))
