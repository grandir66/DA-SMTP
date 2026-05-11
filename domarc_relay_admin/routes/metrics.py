"""Dashboard metriche live: CPU, RAM, file descriptor, code, latenza mail.

- GET /metrics/                    → pagina HTML con polling JS
- GET /metrics/live                → JSON aggiornato (consumato dal JS)

Le metriche sono lette via psutil + query SQLite. Costo per chiamata:
~30-60ms (psutil cpu_percent + 4-5 query DB). Polling ogni 5s e' ok.
"""
from __future__ import annotations

import logging
import os
import shutil
import time
from pathlib import Path
from typing import Any

from flask import (Blueprint, current_app, jsonify, make_response,
                   render_template, session)

from ..auth import login_required

logger = logging.getLogger(__name__)

metrics_bp = Blueprint("metrics", __name__, url_prefix="/metrics")


# Service names systemd noti
_SERVICES = [
    ("admin", "domarc-smtp-relay-admin"),
    ("listener", "stormshield-smtp-relay-listener"),
    ("scheduler", "stormshield-smtp-relay-scheduler"),
]

_ADMIN_DB = "/var/lib/domarc-smtp-relay-admin/admin.db"
_RELAY_DB = "/var/lib/stormshield-smtp-relay/relay.db"


def _read_pid_from_systemd(unit: str) -> int | None:
    """Legge MainPID di un'unit systemd via /run/systemd/system o cgroup."""
    import subprocess
    try:
        cp = subprocess.run(
            ["systemctl", "show", unit, "-p", "MainPID", "--value"],
            capture_output=True, text=True, timeout=3,
        )
        pid_str = cp.stdout.strip()
        pid = int(pid_str)
        return pid if pid > 0 else None
    except Exception:  # noqa: BLE001
        return None


def _process_metrics(pid: int) -> dict[str, Any]:
    """RSS, VSZ, CPU%, threads, FDs di un processo + figli."""
    import psutil
    try:
        proc = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return {"alive": False}
    try:
        children = proc.children(recursive=True)
        all_procs = [proc] + children
        rss = sum(p.memory_info().rss for p in all_procs if p.is_running())
        vsz = sum(p.memory_info().vms for p in all_procs if p.is_running())
        cpu = sum(p.cpu_percent(interval=0.0) for p in all_procs if p.is_running())
        threads = sum(p.num_threads() for p in all_procs if p.is_running())
        try:
            fds = sum(p.num_fds() for p in all_procs if p.is_running())
        except Exception:  # noqa: BLE001
            fds = None
        return {
            "alive": True,
            "pid": pid,
            "n_proc": len(all_procs),
            "rss_mb": round(rss / 1024 / 1024, 1),
            "vsz_mb": round(vsz / 1024 / 1024, 1),
            "cpu_pct": round(cpu, 1),
            "threads": threads,
            "fds": fds,
            "uptime_sec": int(time.time() - proc.create_time()),
        }
    except Exception as exc:  # noqa: BLE001
        return {"alive": False, "error": str(exc)}


def _system_metrics() -> dict[str, Any]:
    import psutil
    vm = psutil.virtual_memory()
    sm = psutil.swap_memory()
    load1, load5, load15 = os.getloadavg()
    return {
        "cpu_count": psutil.cpu_count(logical=True),
        "cpu_pct": round(psutil.cpu_percent(interval=0.0), 1),
        "load_1": round(load1, 2),
        "load_5": round(load5, 2),
        "load_15": round(load15, 2),
        "mem_total_mb": round(vm.total / 1024 / 1024, 0),
        "mem_used_mb": round(vm.used / 1024 / 1024, 0),
        "mem_pct": round(vm.percent, 1),
        "mem_available_mb": round(vm.available / 1024 / 1024, 0),
        "swap_total_mb": round(sm.total / 1024 / 1024, 0),
        "swap_used_mb": round(sm.used / 1024 / 1024, 0),
        "swap_pct": round(sm.percent, 1),
    }


def _disk_metrics() -> dict[str, Any]:
    out: dict[str, Any] = {}
    for label, path in [("var", "/var"), ("tmp", "/tmp"), ("dev_shm", "/dev/shm")]:
        try:
            u = shutil.disk_usage(path)
            out[label] = {
                "total_gb": round(u.total / 1024**3, 2),
                "used_gb": round(u.used / 1024**3, 2),
                "free_gb": round(u.free / 1024**3, 2),
                "pct": round(u.used / u.total * 100, 1),
            }
        except Exception:  # noqa: BLE001
            out[label] = None
    # DB files size
    for label, path in [("admin_db_mb", _ADMIN_DB), ("relay_db_mb", _RELAY_DB)]:
        try:
            size = Path(path).stat().st_size
            wal_size = Path(path + "-wal").stat().st_size if Path(path + "-wal").exists() else 0
            out[label] = round(size / 1024 / 1024, 1)
            out[label.replace("_db_mb", "_wal_mb")] = round(wal_size / 1024 / 1024, 1)
        except Exception:  # noqa: BLE001
            out[label] = None
    return out


def _admin_queue_metrics(storage) -> dict[str, Any]:
    try:
        with storage._connect() as conn:
            out: dict[str, Any] = {}
            for tbl in ("events", "rules", "customers"):
                try:
                    out[tbl + "_count"] = conn.execute(
                        f"SELECT COUNT(*) FROM {tbl}"
                    ).fetchone()[0]
                except Exception:  # noqa: BLE001
                    out[tbl + "_count"] = None
            # Events unsent (non flushed)
            try:
                out["events_unsent"] = conn.execute(
                    "SELECT COUNT(*) FROM events WHERE sent_to_manager_at IS NULL"
                ).fetchone()[0]
            except Exception:  # noqa: BLE001
                out["events_unsent"] = None
            return out
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


def _relay_queue_metrics() -> dict[str, Any]:
    """Query diretta su relay.db (DB del listener) per code + latenza."""
    import sqlite3
    out: dict[str, Any] = {}
    try:
        if not Path(_RELAY_DB).exists():
            return {"error": "relay.db non presente sul nodo admin"}
        conn = sqlite3.connect(f"file:{_RELAY_DB}?mode=ro", uri=True, timeout=3)
        conn.row_factory = sqlite3.Row
        try:
            # Outbound queue per stato
            rows = conn.execute(
                """SELECT state, COUNT(*) as n FROM outbound_queue GROUP BY state"""
            ).fetchall()
            out["outbound_by_state"] = {r["state"]: r["n"] for r in rows}
            # Dispatch queue per stato
            rows = conn.execute(
                """SELECT state, COUNT(*) as n FROM dispatch_queue GROUP BY state"""
            ).fetchall()
            out["dispatch_by_state"] = {r["state"]: r["n"] for r in rows}
            # Quarantine per decisione
            rows = conn.execute(
                """SELECT decision, COUNT(*) as n FROM quarantine GROUP BY decision"""
            ).fetchall()
            out["quarantine_by_decision"] = {r["decision"]: r["n"] for r in rows}
            # Events recenti (last 60 min)
            row = conn.execute(
                """SELECT COUNT(*) as n FROM events_log
                    WHERE received_at >= datetime('now','-60 minutes')"""
            ).fetchone()
            out["mail_last_hour"] = row["n"]
            row = conn.execute(
                """SELECT COUNT(*) as n FROM events_log
                    WHERE received_at >= datetime('now','-5 minutes')"""
            ).fetchone()
            out["mail_last_5min"] = row["n"]
            # Latenza outbound: tempo medio attempt → delivered (ultimi 30 min)
            row = conn.execute(
                """SELECT
                       AVG(duration_ms) as avg_ms,
                       MAX(duration_ms) as max_ms,
                       COUNT(*) as n
                   FROM forward_log
                   WHERE attempted_at >= datetime('now','-30 minutes')
                     AND ok = 1"""
            ).fetchone()
            if row and row["n"]:
                out["outbound_latency_avg_ms"] = round(row["avg_ms"] or 0, 1)
                out["outbound_latency_max_ms"] = row["max_ms"]
                out["outbound_count_30min"] = row["n"]
            # Heartbeat loop scheduler
            rows = conn.execute(
                """SELECT name, last_sync_at FROM sync_meta
                    WHERE name LIKE 'heartbeat.%'
                    ORDER BY name"""
            ).fetchall()
            out["heartbeats"] = {
                r["name"].replace("heartbeat.", ""): r["last_sync_at"]
                for r in rows
            }
            # Cache stale flag
            row = conn.execute(
                """SELECT last_sync_at FROM sync_meta
                    WHERE name = 'heartbeat.cache_stale'"""
            ).fetchone()
            out["cache_stale"] = row["last_sync_at"] if row else None
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        out["error"] = str(exc)
    return out


def _compute_live_metrics(storage) -> dict[str, Any]:
    snapshot = {
        "ts": int(time.time()),
        "system": _system_metrics(),
        "disk": _disk_metrics(),
        "services": {},
        "admin_queue": _admin_queue_metrics(storage),
        "relay_queue": _relay_queue_metrics(),
    }
    for key, unit in _SERVICES:
        pid = _read_pid_from_systemd(unit)
        if pid:
            snapshot["services"][key] = _process_metrics(pid)
        else:
            snapshot["services"][key] = {"alive": False, "error": "no MainPID"}
    return snapshot


def _no_cache(resp):
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, private"
    resp.headers["Pragma"] = "no-cache"
    return resp


@metrics_bp.route("/")
@login_required(role="admin")
def index():
    return _no_cache(make_response(render_template("admin/metrics.html")))


@metrics_bp.route("/live")
@login_required(role="admin")
def live():
    storage = current_app.extensions["domarc_storage"]
    return _no_cache(jsonify(_compute_live_metrics(storage)))
