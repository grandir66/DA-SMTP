"""Integrazioni esterne — UI configurazione + test live.

3 sezioni:
1. **Customer source** — connessione al DB clienti (PostgreSQL del gestionale).
   Parametri: host, port, user/password, stormshield_db, solution_db (con
   credenziali separate), sync_interval. Test connessione live + force refresh
   sync + ultimo log.
2. **Ticket API** — manager esterno per `create_ticket`. Parametri: base_url,
   api_key (cifrata), timeout, max_retries. Test `GET /api/v1/health` live.
3. **AI provider** — Anthropic API key cifrata. Test 1 chiamata Claude Haiku.

Tutti i parametri sono persistiti in `settings` (key-value) per i non-secret
e in `api_keys` (Fernet cifrato) per i secret. La modifica via UI è
**audit-tracked** (`updated_by`, `updated_at` nei settings).

I valori da settings vengono letti da `PostgresCustomerSource` e da
`manager_client` con fallback a env vars per backward compat.
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from typing import Any

from flask import (Blueprint, current_app, flash, jsonify, redirect, request,
                   render_template, session, url_for)

from ..auth import login_required

integrations_bp = Blueprint("integrations", __name__, url_prefix="/integrations")


def _storage():
    return current_app.extensions["domarc_storage"]


def _actor() -> str:
    return session.get("username") or "?"


# Lista parametri con metadata (sezione, type, default)
PARAMS = {
    # Customer source
    "customer_source.backend":           ("customer_source", "select", "stormshield",
                                            ["stormshield", "postgres", "yaml", "sqlite", "rest"]),
    "customer_source.pg.host":           ("customer_source", "text", "192.168.4.41"),
    "customer_source.pg.port":           ("customer_source", "int", "5432"),
    "customer_source.pg.user":           ("customer_source", "text", "stormshield"),
    "customer_source.pg.password":       ("customer_source", "secret", ""),
    "customer_source.pg.stormshield_db": ("customer_source", "text", "stormshield"),
    "customer_source.pg.solution_db":    ("customer_source", "text", "solution"),
    "customer_source.pg.solution_user":  ("customer_source", "text", "solution_user"),
    "customer_source.pg.solution_password": ("customer_source", "secret", ""),
    "customer_source.pg.sync_interval_sec": ("customer_source", "int", "300"),
    "customer_source.pg.sslmode":           ("customer_source", "select", "prefer",
                                                ["disable", "allow", "prefer", "require", "verify-ca", "verify-full"]),

    # Ticket API
    "ticket_api.base_url":               ("ticket_api", "text", "https://manager.domarc.it"),
    "ticket_api.api_key":                ("ticket_api", "secret", ""),
    "ticket_api.timeout_sec":            ("ticket_api", "int", "10"),
    "ticket_api.max_retries":            ("ticket_api", "int", "3"),
    "ticket_api.verify_tls":             ("ticket_api", "bool", "false"),
    "ticket_api.health_path":            ("ticket_api", "text", "/api/v1/tickets/?limit=1"),
    "ticket_api.create_path":            ("ticket_api", "text", "/api/v1/tickets/"),

    # AI provider
    "ai.anthropic.api_key":              ("ai", "secret", ""),
    "ai.default_model":                  ("ai", "text", "claude-haiku-4-5"),
    "ai.timeout_sec":                    ("ai", "int", "5"),
    "ai.daily_budget_usd":               ("ai", "float", "50"),
}


def _read_param(key: str) -> str:
    storage = _storage()
    v = storage.get_setting(key)
    if v is not None:
        return v
    # Fallback env vars (back-compat)
    env_map = {
        "customer_source.pg.host": "GESTIONALE_PG_HOST",
        "customer_source.pg.port": "GESTIONALE_PG_PORT",
        "customer_source.pg.user": "GESTIONALE_PG_USER",
        "customer_source.pg.password": "GESTIONALE_PG_PASSWORD",
        "customer_source.pg.stormshield_db": "GESTIONALE_PG_STORMSHIELD_DB",
        "customer_source.pg.solution_db": "GESTIONALE_PG_SOLUTION_DB",
        "customer_source.pg.solution_user": "GESTIONALE_PG_SOLUTION_USER",
        "customer_source.pg.solution_password": "GESTIONALE_PG_SOLUTION_PASSWORD",
        "customer_source.pg.sync_interval_sec": "GESTIONALE_PG_SYNC_INTERVAL_SEC",
        "customer_source.backend": "DOMARC_RELAY_CUSTOMER_SOURCE",
        "ticket_api.base_url": "MANAGER_BASE_URL",
        "ticket_api.api_key": "MANAGER_API_KEY",
        "ai.anthropic.api_key": "ANTHROPIC_API_KEY",
    }
    if key in env_map:
        return os.environ.get(env_map[key], "") or PARAMS[key][2]
    return PARAMS[key][2] if key in PARAMS else ""


def _write_param(key: str, value: str) -> None:
    storage = _storage()
    storage.upsert_setting(key, value, description=f"Integrations UI ({_actor()})")


def _is_secret_key(key: str) -> bool:
    return key in PARAMS and PARAMS[key][1] == "secret"


def _mask(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "•" * len(value)
    return value[:4] + "•" * (len(value) - 8) + value[-4:]


@integrations_bp.route("/", methods=["GET"])
@login_required(role="admin")
def index():
    """Pagina principale con 3 sezioni."""
    storage = _storage()
    values: dict[str, Any] = {}
    for key, (section, ptype, default, *opts) in [
        (k, (*v, [])) for k, v in PARAMS.items()
    ]:
        v = _read_param(key)
        values[key] = {
            "value": v,
            "display": _mask(v) if ptype == "secret" else v,
            "section": section,
            "type": ptype,
            "default": default,
            "options": opts[0] if opts else [],
        }

    # Customer source health (last sync, count)
    cs_health = None
    try:
        cs = current_app.extensions["domarc_customer_source"]
        if hasattr(cs, "health"):
            cs_health = cs.health()
    except Exception:  # noqa: BLE001
        pass

    return render_template(
        "admin/integrations.html",
        params=PARAMS,
        values=values,
        cs_health=cs_health,
    )


@integrations_bp.route("/save", methods=["POST"])
@login_required(role="admin")
def save():
    """Salva tutti i parametri modificati (i secret vuoti vengono ignorati)."""
    saved = 0
    skipped_secrets = 0
    for key, (section, ptype, default, *_) in PARAMS.items():
        form_key = key.replace(".", "__")
        v = (request.form.get(form_key) or "").strip()
        # I secret vuoti li ignoriamo (non sovrascrivono il valore esistente con stringa vuota)
        if ptype == "secret" and not v:
            skipped_secrets += 1
            continue
        # Bool checkbox: presence == true
        if ptype == "bool":
            v = "true" if request.form.get(form_key) else "false"
        # Numeric clamp
        if ptype == "int":
            try:
                v = str(int(v)) if v else default
            except (TypeError, ValueError):
                continue
        if ptype == "float":
            try:
                v = str(float(v)) if v else default
            except (TypeError, ValueError):
                continue
        _write_param(key, v)
        saved += 1
    flash(f"✓ Salvati {saved} parametri ({skipped_secrets} secret invariati). "
          f"Riavvio servizi consigliato per attivare modifiche al backend.", "success")
    return redirect(url_for("integrations.index"))


# ============================================================ Test live


@integrations_bp.route("/test/customer-source", methods=["POST"])
@login_required(role="admin")
def test_customer_source():
    """Test connessione DB clienti."""
    try:
        import psycopg2
    except ImportError:
        return jsonify({"ok": False, "error": "psycopg2 non installato"}), 500

    host = _read_param("customer_source.pg.host")
    port = int(_read_param("customer_source.pg.port") or 5432)
    user = _read_param("customer_source.pg.user")
    password = _read_param("customer_source.pg.password")
    stormshield_db = _read_param("customer_source.pg.stormshield_db")
    solution_db = _read_param("customer_source.pg.solution_db")
    solution_user = _read_param("customer_source.pg.solution_user") or user
    solution_password = _read_param("customer_source.pg.solution_password") or password
    sslmode = _read_param("customer_source.pg.sslmode") or "prefer"

    results = {"stormshield": None, "solution": None}

    # Test stormshield DB
    try:
        t0 = time.monotonic()
        conn = psycopg2.connect(host=host, port=port, database=stormshield_db,
                                  user=user, password=password,
                                  sslmode=sslmode, connect_timeout=5)
        cur = conn.cursor()
        cur.execute("SELECT current_database(), current_user, "
                     "(SELECT COUNT(*) FROM customer_settings) AS n_settings, "
                     "(SELECT COUNT(*) FROM client_domains WHERE COALESCE(excluded,FALSE)=FALSE) AS n_domains")
        db, u, n_settings, n_domains = cur.fetchone()
        conn.close()
        results["stormshield"] = {
            "ok": True, "duration_ms": int((time.monotonic() - t0) * 1000),
            "db": db, "user": u, "n_customer_settings": n_settings,
            "n_client_domains": n_domains,
        }
    except Exception as exc:  # noqa: BLE001
        results["stormshield"] = {"ok": False, "error": str(exc)[:300]}

    # Test solution DB
    try:
        t0 = time.monotonic()
        conn = psycopg2.connect(host=host, port=port, database=solution_db,
                                  user=solution_user, password=solution_password,
                                  sslmode=sslmode, connect_timeout=5)
        cur = conn.cursor()
        cur.execute("SELECT current_database(), current_user, "
                     "(SELECT COUNT(*) FROM clienti WHERE COALESCE(aescluso,FALSE)=FALSE)")
        db, u, n_clienti = cur.fetchone()
        conn.close()
        results["solution"] = {
            "ok": True, "duration_ms": int((time.monotonic() - t0) * 1000),
            "db": db, "user": u, "n_clienti_attivi": n_clienti,
        }
    except Exception as exc:  # noqa: BLE001
        results["solution"] = {"ok": False, "error": str(exc)[:300]}

    overall_ok = all(r and r.get("ok") for r in results.values())
    return jsonify({"ok": overall_ok, "results": results})


@integrations_bp.route("/test/ticket-api", methods=["POST"])
@login_required(role="admin")
def test_ticket_api():
    """Test GET /api/v1/health del manager."""
    try:
        import httpx
    except ImportError:
        return jsonify({"ok": False, "error": "httpx non installato"}), 500

    base_url = _read_param("ticket_api.base_url").rstrip("/")
    api_key = _read_param("ticket_api.api_key")
    timeout = int(_read_param("ticket_api.timeout_sec") or 10)
    verify = (_read_param("ticket_api.verify_tls") or "false").lower() in ("true", "1", "yes")
    health_path = _read_param("ticket_api.health_path") or "/api/v1/tickets/?limit=1"

    if not base_url or not api_key:
        return jsonify({
            "ok": False,
            "info": "Compila base_url + api_key del manager esterno e clicca 'Salva tutte le modifiche' prima di testare.",
            "missing": [k for k, v in {"base_url": base_url, "api_key": api_key}.items() if not v],
        }), 400

    # Provo prima health_path configurato, poi fallback a path tipici
    candidates = [health_path]
    if health_path != "/api/v1/tickets/?limit=1":
        candidates.append("/api/v1/tickets/?limit=1")
    if health_path != "/api/v1/health":
        candidates.append("/api/v1/health")

    last = None
    for path in candidates:
        try:
            t0 = time.monotonic()
            r = httpx.get(f"{base_url}{path}",
                           headers={"X-API-Key": api_key},
                           verify=verify, timeout=timeout)
            last = {
                "tried_path": path,
                "status_code": r.status_code,
                "response": r.text[:300],
                "duration_ms": int((time.monotonic() - t0) * 1000),
            }
            if 200 <= r.status_code < 300:
                last["ok"] = True
                return jsonify(last)
        except Exception as exc:  # noqa: BLE001
            last = {"tried_path": path, "error": str(exc)[:300]}
    # Tutti i tentativi hanno fallito
    last["ok"] = False
    last["all_paths_tried"] = candidates
    last["hint"] = ("Verifica che il path sia corretto (campo 'Health path'). "
                    "Path standard del manager Domarc: /api/v1/tickets/?limit=1")
    return jsonify(last)


@integrations_bp.route("/test/ai", methods=["POST"])
@login_required(role="admin")
def test_ai():
    """Test 1 chiamata Anthropic Claude Haiku con prompt minimo."""
    api_key = _read_param("ai.anthropic.api_key")
    model = _read_param("ai.default_model") or "claude-haiku-4-5"
    timeout = int(_read_param("ai.timeout_sec") or 5)

    if not api_key:
        return jsonify({
            "ok": False,
            "info": "Compila la API key Anthropic (formato sk-ant-...) e clicca 'Salva' prima di testare.",
        }), 400
    if not api_key.startswith("sk-ant-"):
        return jsonify({
            "ok": False,
            "error": f"api_key non valida (deve iniziare con 'sk-ant-', ricevuto prefix '{api_key[:8]}...')",
        }), 400

    try:
        import anthropic
    except ImportError:
        return jsonify({"ok": False, "error": "pacchetto anthropic non installato"}), 500

    try:
        client = anthropic.Anthropic(api_key=api_key, timeout=timeout)
        t0 = time.monotonic()
        resp = client.messages.create(
            model=model,
            max_tokens=20,
            messages=[{"role": "user", "content": "Rispondi solo con la parola: PONG"}],
        )
        txt = resp.content[0].text if resp.content else ""
        usage = resp.usage if hasattr(resp, "usage") else None
        return jsonify({
            "ok": True,
            "model": model,
            "response": txt,
            "duration_ms": int((time.monotonic() - t0) * 1000),
            "input_tokens": getattr(usage, "input_tokens", None) if usage else None,
            "output_tokens": getattr(usage, "output_tokens", None) if usage else None,
        })
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)[:500]}), 500


@integrations_bp.route("/sync/customer-source", methods=["POST"])
@login_required(role="admin")
def sync_customer_source_now():
    """Forza un sync immediato del customer source (PG → cache locale)."""
    cs = current_app.extensions.get("domarc_customer_source")
    if not cs or not hasattr(cs, "sync_now"):
        return jsonify({"ok": False, "error": "Customer source attivo non supporta sync (backend non postgres)"}), 400
    report = cs.sync_now(triggered_by=f"manual:{_actor()}")
    return jsonify(report)
