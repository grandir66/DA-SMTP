"""Queue admin: outbound_queue + quarantine + dispatch_queue del listener.

In aggiunta al dump tabellare, calcoliamo:
- ``age_seconds`` per ogni riga (per highlight messaggi bloccati);
- ``stats.outbound_age_buckets`` (quanti messaggi in coda da 0-1m, 1-5m, 5-30m, >30m);
- ``stats.outbound_stuck`` (numero pending/failed con `next_attempt_at` nel passato).

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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        # Listener salva ISO con TZ; SQLite datetime() salva senza
        s = s.replace("Z", "+00:00") if s.endswith("Z") else s
        dt = datetime.fromisoformat(s.replace(" ", "T"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def _age_seconds(s: str | None) -> int | None:
    dt = _parse_iso(s)
    if dt is None:
        return None
    return max(0, int((datetime.now(timezone.utc) - dt).total_seconds()))


def _format_age(sec: int | None) -> str:
    if sec is None:
        return "—"
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        return f"{sec//60}m {sec%60}s"
    if sec < 86400:
        return f"{sec//3600}h {(sec%3600)//60}m"
    return f"{sec//86400}g {(sec%86400)//3600}h"


# Alias compatti per smarthost noti (M365 tenants, server interni, ecc).
# Ridotti a 1-2 parole per occupare poco spazio in tabella.
_SMARTHOST_ALIAS_MAP = {
    # Microsoft 365 connectors (formato: <tenant>-<tld>.mail.protection.outlook.com)
    "mail.protection.outlook.com": "M365",
}


def _smarthost_alias(host: str | None) -> tuple[str, str]:
    """Restituisce (alias_breve, host_completo_per_tooltip).
    Estrae 'M365' dai connector outlook + tenant friendly se possibile."""
    if not host:
        return ("—", "")
    h = host.strip().lower()
    # Outlook M365: prendi tenant prefix (es. "domarc-it" da "domarc-it.mail.protection.outlook.com")
    if h.endswith(".mail.protection.outlook.com"):
        tenant = h.split(".mail.protection.outlook.com")[0]
        # "domarc-it" → "M365 · domarc.it"
        readable = tenant.replace("-", ".") if tenant else "?"
        return (f"M365 · {readable}", host)
    # Server interni IPv4
    try:
        import ipaddress
        ipaddress.ip_address(h)
        return (f"IP {h}", host)
    except ValueError:
        pass
    # Default: dominio (rimuovi 'smtp.', 'mail.', port)
    short = h.split(":")[0]
    for prefix in ("smtp.", "mail.", "mx."):
        if short.startswith(prefix):
            short = short[len(prefix):]
            break
    return (short, host)


def _classify_quarantine(reason: str | None,
                         last_error: str | None) -> tuple[str, str, str]:
    """Categoria esplicita del problema (label umana, severity, hint risoluzione).

    severity: 'info' | 'warn' | 'error'
    Pattern di matching basati sui codici SMTP enhanced (RFC 3463) restituiti
    dagli smarthost più comuni (Exchange Online, Postfix, Sendmail).
    """
    r = (reason or "").lower()
    e = (last_error or "")
    el = e.lower()

    # Reason del relay (assegnato dalla pipeline/scheduler quando mette in quarantena)
    if r == "pipeline_exception":
        return ("Errore interno pipeline (DLQ)", "error",
                "Bug applicativo: indagare nei log del listener.")
    if r == "loop_marker_detected":
        return ("Mail loop sospetto (X-Domarc-Forwarded-By)", "warn",
                "La mail è già passata dal relay → potenziale loop.")
    if r == "too_many_hops":
        return ("Troppi hop SMTP (>25 Received)", "warn",
                "La mail ha attraversato troppi server prima di arrivare.")

    # forward_dead_letter: il vero motivo è nel last_error dello smarthost
    if "5.4.1" in el and "access denied" in el:
        return ("Casella inesistente sullo smarthost", "error",
                "Il smarthost (M365/Exchange) rifiuta: la casella destinatario "
                "NON esiste. Crea la casella o aggiungi un alias di redirect.")
    if "5.1.1" in el or "user unknown" in el or "no such user" in el:
        return ("Utente sconosciuto", "error",
                "Casella inesistente sul server destinatario. "
                "Crea la casella oppure intercetta l'indirizzo con una regola.")
    if "4.5.3" in el and "multiple tenants" in el:
        return ("Mail multi-tenant rifiutata (legacy)", "warn",
                "Caso storico: la mail aveva destinatari su più tenant M365. "
                "Bug pipeline risolto il 2026-05-11 — non si ripresenta.")
    if "5.7.1" in el and "relay" in el:
        return ("Relay rifiutato dal smarthost", "error",
                "Il smarthost non accetta il relay per questo destinatario. "
                "Verifica connector M365 o whitelist IP del relay.")
    if "5.7." in el and ("spam" in el or "policy" in el or "blocked" in el):
        return ("Bloccato per policy spam", "warn",
                "Il smarthost ha rifiutato per policy antispam.")
    if "5.2.2" in el or "quota" in el or "mailbox full" in el:
        return ("Casella destinatario piena", "warn",
                "Quota superata: il destinatario deve liberare spazio.")
    if "timeout" in el or "timed out" in el:
        return ("Smarthost irraggiungibile (timeout)", "error",
                "Connessione al smarthost in timeout. "
                "Verifica rete/firewall o stato smarthost.")
    if "connection refused" in el or "connect" in el and "refused" in el:
        return ("Smarthost down (connection refused)", "error",
                "Lo smarthost rifiuta la connessione.")
    if "smtprecipientsrefused" in el:
        return ("Destinatario rifiutato dallo smarthost", "error",
                "Codice SMTP permanente (5xx) sul rcpt — vedi dettaglio errore.")
    if r == "forward_dead_letter":
        return ("Forward fallito (max retry)", "warn",
                "Dopo i retry massimi la consegna è fallita. Vedi errore.")
    # Fallback
    return ((reason or "—"), "info", "")


def _format_short_ts(s: str | None, now: datetime | None = None) -> str:
    """Data/ora compatta:
       - oggi  → 'HH:MM'
       - questo anno → 'DD/MM HH:MM'
       - più vecchio → 'DD/MM/YY'
    """
    dt = _parse_iso(s)
    if dt is None:
        return "—"
    cur = now or datetime.now(timezone.utc)
    if dt.date() == cur.date():
        return dt.strftime("%H:%M")
    if dt.year == cur.year:
        return dt.strftime("%d/%m %H:%M")
    return dt.strftime("%d/%m/%y")

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

    state_filter = (request.args.get("state") or "").strip().lower()
    only_active = request.args.get("only_active") == "1"

    outbound: list[dict] = []
    quarantine: list[dict] = []
    dispatch: list[dict] = []
    stats: dict = {
        "outbound": {},
        "quarantine_count": 0,
        "dispatch": {},
        "outbound_age_buckets": {"0_1m": 0, "1_5m": 0, "5_30m": 0, "over_30m": 0},
        "outbound_stuck": 0,
        "outbound_oldest_pending_age": None,
    }
    age_threshold_pending = 600  # 10 min — sopra è "stuck"

    if conn is not None:
        try:
            # Outbound queue
            outbound_rows = conn.execute("""
                SELECT id, event_uuid, action, mail_from, rcpt_to_json,
                       smarthost, smarthost_port, smarthost_tls, state, attempts,
                       next_attempt_at, last_error, delivered_at, created_at,
                       length(mime_blob) AS mime_size
                FROM outbound_queue
                ORDER BY
                    CASE state
                        WHEN 'pending' THEN 1
                        WHEN 'failed'  THEN 2
                        WHEN 'sent'    THEN 3
                        ELSE 4 END,
                    id DESC
                LIMIT 300
            """).fetchall()

            now = datetime.now(timezone.utc)
            oldest_pending: int | None = None
            for r in outbound_rows:
                d = dict(r)
                try:
                    d["recipients"] = json.loads(d["rcpt_to_json"] or "[]")
                except (TypeError, ValueError):
                    d["recipients"] = []
                age = _age_seconds(d.get("created_at"))
                d["age_seconds"] = age
                d["age_human"] = _format_age(age)
                # Pre-compute formattazioni compatte per il template
                alias, full = _smarthost_alias(d.get("smarthost"))
                d["smarthost_alias"] = alias
                d["smarthost_full"] = full
                d["created_short"] = _format_short_ts(d.get("created_at"), now)
                d["delivered_short"] = _format_short_ts(d.get("delivered_at"), now)
                d["next_attempt_short"] = _format_short_ts(d.get("next_attempt_at"), now)
                # Bucket
                if age is not None and d.get("state") not in ("sent", "delivered"):
                    if age < 60:
                        stats["outbound_age_buckets"]["0_1m"] += 1
                    elif age < 300:
                        stats["outbound_age_buckets"]["1_5m"] += 1
                    elif age < 1800:
                        stats["outbound_age_buckets"]["5_30m"] += 1
                    else:
                        stats["outbound_age_buckets"]["over_30m"] += 1
                # Stuck = pending/failed con prossimo retry nel passato (lo scheduler avrebbe già dovuto)
                # oppure pending da > 10 min senza retry pianificato.
                stuck = False
                if d.get("state") in ("pending", "failed"):
                    nxt = _parse_iso(d.get("next_attempt_at"))
                    if nxt is not None and nxt < now:
                        stuck = True
                    elif nxt is None and age is not None and age > age_threshold_pending:
                        stuck = True
                d["stuck"] = stuck
                if stuck:
                    stats["outbound_stuck"] += 1
                if d.get("state") == "pending" and age is not None:
                    if oldest_pending is None or age > oldest_pending:
                        oldest_pending = age

                # Filtri
                if state_filter and d.get("state") != state_filter:
                    continue
                if only_active and d.get("state") in ("sent", "delivered"):
                    continue
                outbound.append(d)

            stats["outbound_oldest_pending_age"] = _format_age(oldest_pending) if oldest_pending else None
            for r in conn.execute("SELECT state, COUNT(*) AS n FROM outbound_queue GROUP BY state"):
                stats["outbound"][r["state"]] = r["n"]

            # Quarantine — JOIN con outbound_queue per recuperare last_error
            # del smarthost (è il vero motivo per cui la mail è dead-letter,
            # diverso dal generico reason="forward_dead_letter").
            for r in conn.execute("""
                SELECT q.id, q.event_uuid, q.reason, q.from_address, q.to_address,
                       q.decision, q.reviewed_at, q.notes, q.created_at,
                       length(q.mime_blob) AS mime_size,
                       (SELECT ob.last_error FROM outbound_queue ob
                          WHERE ob.event_uuid = q.event_uuid
                          ORDER BY ob.id DESC LIMIT 1) AS smarthost_error
                FROM quarantine q
                ORDER BY q.id DESC LIMIT 200
            """):
                qd = dict(r)
                qd["created_short"] = _format_short_ts(qd.get("created_at"), now)
                qd["reviewed_short"] = _format_short_ts(qd.get("reviewed_at"), now)
                label, severity, hint = _classify_quarantine(
                    qd.get("reason"), qd.get("smarthost_error"))
                qd["problem_label"] = label
                qd["problem_severity"] = severity
                qd["problem_hint"] = hint
                quarantine.append(qd)
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
                dd = dict(r)
                dd["created_short"] = _format_short_ts(dd.get("created_at"), now)
                dd["next_attempt_short"] = _format_short_ts(dd.get("next_attempt_at"), now)
                dispatch.append(dd)
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
            SELECT q.id, q.event_uuid, q.reason, q.from_address, q.to_address,
                   q.decision, q.reviewed_at, q.notes, q.created_at,
                   length(q.mime_blob) AS mime_size,
                   (SELECT ob.last_error FROM outbound_queue ob
                      WHERE ob.event_uuid = q.event_uuid
                      ORDER BY ob.id DESC LIMIT 1) AS smarthost_error,
                   (SELECT ob.smarthost FROM outbound_queue ob
                      WHERE ob.event_uuid = q.event_uuid
                      ORDER BY ob.id DESC LIMIT 1) AS smarthost,
                   (SELECT ob.attempts FROM outbound_queue ob
                      WHERE ob.event_uuid = q.event_uuid
                      ORDER BY ob.id DESC LIMIT 1) AS attempts
            FROM quarantine q WHERE q.id = ?
        """, (queue_id,)).fetchone()
        if not row:
            abort(404)
    finally:
        conn.close()
    item = dict(row)
    label, severity, hint = _classify_quarantine(
        item.get("reason"), item.get("smarthost_error"))
    item["problem_label"] = label
    item["problem_severity"] = severity
    item["problem_hint"] = hint
    return render_template("admin/queue_quarantine_detail.html", item=item)
