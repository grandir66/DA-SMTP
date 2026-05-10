"""Auth locale (D4 del piano standalone): username + password bcrypt + sessione cookie.

3 ruoli: admin / operator / viewer (definiti su `users.role`).

LDAP/AD → v1.1 (rinviato).
OAuth2/OIDC → v1.2 come prima feature Pro.
"""
from __future__ import annotations

import functools
import logging
import threading
import time
from collections import defaultdict
from typing import Any

from flask import Blueprint, abort, current_app, flash, g, redirect, render_template, request, session, url_for

logger = logging.getLogger(__name__)


auth_bp = Blueprint("auth", __name__)


# Rate limiting in-process per /login:
# - chiave: (username, ip)
# - max 5 tentativi falliti in 15 minuti → lockout 15 min
# In-memory: sufficiente per single-worker. Per Gunicorn multi-worker andrebbe
# spostato su Redis o SQLite; per ora bcrypt e' gia' un anti-bruteforce robusto.
_FAIL_LOCK = threading.Lock()
_FAIL_ATTEMPTS: dict[tuple[str, str], list[float]] = defaultdict(list)
_FAIL_WINDOW_SEC = 15 * 60
_FAIL_MAX = 5
_LOCKOUT_SEC = 15 * 60


def _is_locked(key: tuple[str, str]) -> tuple[bool, int]:
    """Ritorna (locked, seconds_remaining). Lock se >= _FAIL_MAX fail in finestra."""
    now = time.monotonic()
    with _FAIL_LOCK:
        attempts = _FAIL_ATTEMPTS.get(key, [])
        # purge entries fuori finestra
        attempts = [t for t in attempts if (now - t) < _FAIL_WINDOW_SEC]
        _FAIL_ATTEMPTS[key] = attempts
        if len(attempts) < _FAIL_MAX:
            return (False, 0)
        oldest = min(attempts)
        unlock_at = oldest + _LOCKOUT_SEC
        remaining = max(1, int(unlock_at - now))
        return (True, remaining)


def _record_fail(key: tuple[str, str]) -> None:
    now = time.monotonic()
    with _FAIL_LOCK:
        _FAIL_ATTEMPTS[key].append(now)


def _record_success(key: tuple[str, str]) -> None:
    with _FAIL_LOCK:
        _FAIL_ATTEMPTS.pop(key, None)


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        storage = current_app.extensions["domarc_storage"]
        ip = request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0].strip()
        ua = request.headers.get("User-Agent", "")[:255]
        lock_key = (username.lower(), ip)
        locked, remaining = _is_locked(lock_key)
        if locked:
            storage.log_login(username=username, ip=ip, ua=ua, outcome="locked")
            flash(f"Troppi tentativi falliti. Riprova tra {remaining // 60 + 1} minuti.",
                  "error")
            return render_template("admin/login.html"), 429
        user = storage.get_user_by_username(username)
        if user:
            try:
                import bcrypt
                if bcrypt.checkpw(password.encode("utf-8"),
                                  user["password_hash"].encode("utf-8")):
                    session.clear()
                    session["user_id"] = user["id"]
                    session["username"] = user["username"]
                    session["role"] = user.get("role") or "readonly"
                    session["user_tenant_id"] = user.get("tenant_id")  # None per superadmin
                    session.permanent = True
                    _record_success(lock_key)
                    storage.log_login(username=username, ip=ip, ua=ua, outcome="success")
                    flash(f"Benvenuto, {username}!", "success")
                    return redirect(request.args.get("next") or url_for("dashboard.index"))
            except Exception as exc:  # noqa: BLE001
                logger.warning("bcrypt check error: %s", exc)
        _record_fail(lock_key)
        storage.log_login(username=username, ip=ip, ua=ua, outcome="failed")
        flash("Username o password non corretti", "error")
    return render_template("admin/login.html")


@auth_bp.route("/logout")
def logout():
    storage = current_app.extensions.get("domarc_storage")
    if storage and session.get("username"):
        ip = request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0].strip()
        storage.log_login(
            username=session["username"], ip=ip, ua=request.headers.get("User-Agent", "")[:255],
            outcome="logout",
        )
    session.clear()
    flash("Logout effettuato", "info")
    return redirect(url_for("auth.login"))


def login_required(role: str | None = None):
    """Decorator: richiede sessione valida. Opzionalmente filtra per ruolo minimo.

    Ruolo gerarchico: readonly < tech < admin < superadmin

    Retrocompat: i nomi legacy `viewer/operator/admin` continuano a funzionare
    (mappati internamente).
    """
    role_aliases = {"viewer": "readonly", "operator": "tech"}
    role_levels = {"readonly": 1, "tech": 2, "admin": 3, "superadmin": 4}

    def normalize(r: str | None) -> str:
        if not r:
            return "readonly"
        return role_aliases.get(r, r)

    required = normalize(role)

    def decorator(f):
        @functools.wraps(f)
        def wrapper(*args, **kwargs):
            if not session.get("user_id"):
                return redirect(url_for("auth.login", next=request.path))
            user_role = normalize(session.get("role"))
            if role:
                user_lvl = role_levels.get(user_role, 0)
                req_lvl = role_levels.get(required, 999)
                if user_lvl < req_lvl:
                    abort(403)
            g.current_user = {
                "id": session["user_id"],
                "username": session.get("username"),
                "role": user_role,
                "tenant_id": session.get("user_tenant_id"),  # tenant primario (None=superadmin)
            }
            return f(*args, **kwargs)
        return wrapper
    return decorator
