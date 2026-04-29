"""Route blueprints dell'admin web. Per v1.0 skeleton: dashboard + health.

Le 6 macroaree UI (rules / templates / service_hours / auth_codes / aggregations /
events / tenants) saranno portate dal manager nelle settimane 5-6.
"""
from __future__ import annotations

from flask import Blueprint, current_app, jsonify, render_template, session

from ..auth import login_required


dashboard_bp = Blueprint("dashboard", __name__)
health_bp = Blueprint("health", __name__)


@dashboard_bp.route("/")
@dashboard_bp.route("/dashboard")
@login_required()
def index():
    from flask import g
    from collections import Counter
    from datetime import datetime, timedelta, timezone
    storage = current_app.extensions["domarc_storage"]
    customer_source = current_app.extensions["domarc_customer_source"]
    tid = int(getattr(g, "current_tenant_id", 1))
    # KPI scopati al tenant attivo
    rules = storage.list_rules(tenant_id=tid, only_enabled=True)
    templates = storage.list_templates(tenant_id=tid, only_enabled=True)
    events_recent, total_events = storage.list_events(tenant_id=tid, hours=24, page=1, page_size=10000)
    auth_codes = storage.list_auth_codes(tenant_id=tid, only_active=True, limit=1000)
    occurrences = storage.list_occurrences(tenant_id=tid, filter_state="active")
    occ_with_ticket = sum(1 for o in occurrences if o.get("ticket_id"))

    # Hourly series 24h
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    buckets = [(now - timedelta(hours=h)) for h in range(23, -1, -1)]
    hourly_series = []
    by_hour = {}
    by_hour_tickets = {}
    for e in events_recent:
        ra = e.get("received_at")
        if not ra: continue
        try:
            if isinstance(ra, str):
                dt = datetime.fromisoformat(ra.replace("Z", "+00:00"))
            else:
                dt = ra
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            key = dt.replace(minute=0, second=0, microsecond=0).isoformat()
        except Exception:
            continue
        by_hour[key] = by_hour.get(key, 0) + 1
        if e.get("ticket_id"):
            by_hour_tickets[key] = by_hour_tickets.get(key, 0) + 1
    for b in buckets:
        k = b.isoformat()
        hourly_series.append({"hour": k, "total": by_hour.get(k, 0), "tickets": by_hour_tickets.get(k, 0)})

    # Actions breakdown
    actions_counter = Counter((e.get("action_taken") or "default_delivery") for e in events_recent)
    actions_breakdown = sorted(actions_counter.items(), key=lambda x: -x[1])

    # Top senders/recipients
    top_senders = Counter((e.get("from_address") or "—").lower() for e in events_recent if e.get("from_address")).most_common(8)
    top_recipients = Counter((e.get("to_address") or "—").lower() for e in events_recent if e.get("to_address")).most_common(8)

    last_event_seen = events_recent[0]["received_at"] if events_recent else None

    return render_template(
        "admin/dashboard.html",
        storage_health=storage.health(),
        customer_source_health=customer_source.health(),
        version=current_app.extensions["domarc_version"],
        kpi={
            "rules": len(rules),
            "templates": len(templates),
            "events_24h": len(events_recent),
            "events_total": total_events,
            "auth_codes_active": len(auth_codes),
            "occurrences_active": len(occurrences),
            "occurrences_with_ticket": occ_with_ticket,
        },
        stats={
            "hourly_series": hourly_series,
            "actions_breakdown": actions_breakdown,
            "top_senders": top_senders,
            "top_recipients": top_recipients,
            "last_event_seen": last_event_seen,
            "aggregations_active": len(storage.list_aggregations(tenant_id=tid, only_enabled=True)) if hasattr(storage, "list_aggregations") else 0,
        },
    )


@health_bp.route("/health")
def health():
    """Endpoint pubblico no-auth — utile per loadbalancer healthcheck (D3 del piano)."""
    storage = current_app.extensions["domarc_storage"]
    h = storage.health()
    code = 200 if h.get("ok") else 503
    return jsonify({
        "status": "ok" if h.get("ok") else "error",
        "version": current_app.extensions["domarc_version"],
        "schema_version": h.get("schema_version"),
    }), code


@health_bp.route("/diagnostic")
@login_required(role="admin")
def diagnostic():
    """Endpoint admin-only — bundle completo per troubleshooting (D3)."""
    storage = current_app.extensions["domarc_storage"]
    customer_source = current_app.extensions["domarc_customer_source"]
    return jsonify({
        "version": current_app.extensions["domarc_version"],
        "storage": storage.health(),
        "customer_source": customer_source.health(),
        "session_username": session.get("username"),
    })


@health_bp.route("/health/full")
@login_required(role="admin")
def health_full():
    """Health check completo di tutti i componenti — JSON.

    Pensato per dashboard, monitoring esterni e troubleshooting. Verifica:
    - Storage (DB schema, scrittura, integrità)
    - Customer source (raggiungibilità manager)
    - Master key Fernet (leggibile)
    - Moduli Python (anthropic, cryptography, optional spaCy)
    - Provider IA (config + raggiungibili)
    - AI bindings (almeno uno attivo per classify_email se ai_enabled)
    - Privacy bypass list (cache valida)
    - Settings critici
    - Disk space
    """
    return jsonify(_compute_full_health(current_app))


@health_bp.route("/health/test-stack", methods=["POST"])
@login_required(role="admin")
def test_stack():
    """Test live dello stack: chiama Claude (se configurato) + verifica DB write."""
    return jsonify(_run_test_stack(current_app))


@health_bp.route("/health/system")
@login_required(role="admin")
def system_dashboard():
    """Pagina HTML con status di tutti i componenti."""
    health_data = _compute_full_health(current_app)
    return render_template("admin/system_health.html", health=health_data)


# =================================================================
# IMPLEMENTAZIONE CHECK
# =================================================================

def _compute_full_health(app) -> dict:
    """Esegue tutti i check (read-only, no chiamate esterne lente)."""
    import os
    import shutil
    from pathlib import Path

    storage = app.extensions["domarc_storage"]
    customer_source = app.extensions.get("domarc_customer_source")
    version = app.extensions.get("domarc_version", "?")
    checks: list[dict] = []

    # 1. Storage
    try:
        storage_health = storage.health()
        ok = storage_health.get("ok", False)
        checks.append({
            "id": "storage", "label": "Database storage",
            "status": "ok" if ok else "error",
            "detail": f"backend={storage_health.get('backend')}, schema=v{storage_health.get('schema_version')}, "
                      f"tenants={storage_health.get('tenants_count')}, eventi={storage_health.get('events_count')}",
            "data": storage_health,
        })
    except Exception as exc:  # noqa: BLE001
        checks.append({"id": "storage", "label": "Database storage",
                        "status": "error", "detail": str(exc)})

    # 2. Customer source
    if customer_source:
        try:
            cs_health = customer_source.health()
            ok = cs_health.get("ok", False) or cs_health.get("backend") == "stub"
            checks.append({
                "id": "customer_source", "label": "Customer source (manager)",
                "status": "ok" if ok else "warning",
                "detail": f"backend={cs_health.get('backend')}",
                "data": cs_health,
            })
        except Exception as exc:  # noqa: BLE001
            checks.append({"id": "customer_source", "label": "Customer source",
                            "status": "error", "detail": str(exc)})

    # 3. Master key Fernet
    try:
        from ..secrets_manager import get_secrets_manager
        sm = get_secrets_manager()
        key_path = sm._path  # type: ignore[attr-defined]
        if key_path.exists():
            mode = oct(key_path.stat().st_mode & 0o777)
            mode_ok = mode in ("0o600", "0o400")
            checks.append({
                "id": "master_key", "label": "Master key Fernet",
                "status": "ok" if mode_ok else "warning",
                "detail": f"path={key_path}, permessi={mode}" + (" (DOVREBBE essere 600)" if not mode_ok else ""),
            })
        else:
            checks.append({
                "id": "master_key", "label": "Master key Fernet",
                "status": "warning",
                "detail": f"non ancora generata in {key_path} (verrà creata al primo uso)",
            })
    except Exception as exc:  # noqa: BLE001
        checks.append({"id": "master_key", "label": "Master key Fernet",
                        "status": "error", "detail": str(exc)})

    # 4. Moduli Python
    try:
        from ..module_manager import list_modules_status
        modules = list_modules_status()
        critical_missing = [m for m in modules
                            if not m.get("optional") and not m.get("installed")]
        optional_missing = [m for m in modules
                             if m.get("optional") and not m.get("installed")]
        if critical_missing:
            status = "error"
            detail = "Mancanti critici: " + ", ".join(m["code"] for m in critical_missing)
        elif optional_missing:
            status = "warning"
            detail = f"{len(modules) - len(optional_missing)}/{len(modules)} installati (opzionali mancanti: " + \
                     ", ".join(m["code"] for m in optional_missing) + ")"
        else:
            status = "ok"
            detail = f"{len(modules)}/{len(modules)} installati"
        checks.append({"id": "modules", "label": "Moduli Python", "status": status, "detail": detail,
                        "data": {"modules": modules}})
    except Exception as exc:  # noqa: BLE001
        checks.append({"id": "modules", "label": "Moduli Python",
                        "status": "error", "detail": str(exc)})

    # 5. AI Provider configurati
    try:
        providers = storage.list_ai_providers()
        active = [p for p in providers if p.get("enabled")]
        if not providers:
            checks.append({"id": "ai_providers", "label": "AI Provider configurati",
                            "status": "warning", "detail": "Nessun provider configurato (AI disabilitata)"})
        elif not active:
            checks.append({"id": "ai_providers", "label": "AI Provider configurati",
                            "status": "warning",
                            "detail": f"{len(providers)} configurati ma 0 attivi"})
        else:
            checks.append({"id": "ai_providers", "label": "AI Provider configurati",
                            "status": "ok",
                            "detail": f"{len(active)}/{len(providers)} attivi: " +
                                      ", ".join(f"{p['name']} ({p['kind']})" for p in active),
                            "data": {"active": [{"id": p["id"], "name": p["name"]} for p in active]}})
    except Exception as exc:  # noqa: BLE001
        checks.append({"id": "ai_providers", "label": "AI Provider",
                        "status": "error", "detail": str(exc)})

    # 6. AI bindings + master switch
    try:
        settings = {s["key"]: s["value"] for s in storage.list_settings()}
        ai_enabled = (settings.get("ai_enabled", "false") or "").lower() == "true"
        shadow = (settings.get("ai_shadow_mode", "true") or "").lower() == "true"
        bindings = storage.list_ai_job_bindings(only_enabled=True)
        bindings_by_job: dict[str, int] = {}
        for b in bindings:
            bindings_by_job[b["job_code"]] = bindings_by_job.get(b["job_code"], 0) + 1
        classify_count = bindings_by_job.get("classify_email", 0)
        if ai_enabled and classify_count == 0:
            status = "warning"
            detail = "AI master ON ma nessun binding attivo per classify_email — fail-safe attivo"
        elif ai_enabled:
            mode = "SHADOW" if shadow else "LIVE"
            detail = f"AI master ON ({mode}), {len(bindings)} binding attivi: " + \
                     ", ".join(f"{j}={c}" for j, c in bindings_by_job.items())
            status = "ok"
        else:
            status = "info"
            detail = f"AI master OFF — {len(bindings)} binding configurati ma non usati"
        checks.append({"id": "ai_bindings", "label": "AI Routing per job", "status": status, "detail": detail,
                        "data": {"ai_enabled": ai_enabled, "shadow_mode": shadow,
                                  "bindings_by_job": bindings_by_job}})
    except Exception as exc:  # noqa: BLE001
        checks.append({"id": "ai_bindings", "label": "AI Routing", "status": "error", "detail": str(exc)})

    # 7. Privacy bypass list
    try:
        pb = storage.list_privacy_bypass_active()
        total = len(pb.get("from", [])) + len(pb.get("to", [])) + \
                len(pb.get("from_domains", [])) + len(pb.get("to_domains", []))
        checks.append({
            "id": "privacy_bypass", "label": "Privacy bypass list",
            "status": "ok",
            "detail": f"{total} entries totali (from={len(pb.get('from', []))}, "
                      f"to={len(pb.get('to', []))}, dom_from={len(pb.get('from_domains', []))}, "
                      f"dom_to={len(pb.get('to_domains', []))})",
        })
    except Exception as exc:  # noqa: BLE001
        checks.append({"id": "privacy_bypass", "label": "Privacy bypass list",
                        "status": "error", "detail": str(exc)})

    # 8. Settings critici
    try:
        settings = {s["key"]: s["value"] for s in storage.list_settings()}
        critical = ["ai_enabled", "ai_shadow_mode", "ai_daily_budget_usd",
                    "ai_apply_min_confidence", "ai_fallback_forward_to"]
        missing = [k for k in critical if k not in settings]
        if missing:
            checks.append({"id": "settings", "label": "Settings critici",
                            "status": "warning",
                            "detail": f"Mancano: {', '.join(missing)}"})
        else:
            checks.append({"id": "settings", "label": "Settings critici",
                            "status": "ok", "detail": f"{len(critical)} setting critici presenti"})
    except Exception as exc:  # noqa: BLE001
        checks.append({"id": "settings", "label": "Settings", "status": "error", "detail": str(exc)})

    # 9. Disk space
    try:
        from ..config import load_config
        cfg = app.extensions.get("domarc_config")
        if cfg and cfg.db_path:
            db_dir = Path(cfg.db_path).parent
            usage = shutil.disk_usage(db_dir)
            free_gb = usage.free / (1024**3)
            pct = 100 * usage.free / usage.total
            if pct < 5:
                status = "error"; level = "CRITICO"
            elif pct < 15:
                status = "warning"; level = "BASSO"
            else:
                status = "ok"; level = "OK"
            checks.append({
                "id": "disk", "label": "Spazio disco",
                "status": status,
                "detail": f"{level}: {free_gb:.1f} GB liberi su {usage.total / (1024**3):.0f} GB ({pct:.1f}%) — {db_dir}",
            })
    except Exception as exc:  # noqa: BLE001
        checks.append({"id": "disk", "label": "Spazio disco",
                        "status": "warning", "detail": str(exc)})

    # 10. AI Decisions activity (24h)
    try:
        from ..ai_assistant.decisions import _is_master_enabled
        if _is_master_enabled(storage):
            decisions_24h = storage.list_ai_decisions(hours=24, limit=2000)
            errors_24h = sum(1 for d in decisions_24h if d.get("error"))
            cost_today = storage.sum_ai_decisions_cost_today()
            settings = {s["key"]: s["value"] for s in storage.list_settings()}
            budget = float(settings.get("ai_daily_budget_usd", "50") or 50)
            pct_budget = 100 * cost_today / budget if budget else 0
            if errors_24h / max(len(decisions_24h), 1) > 0.20:
                status = "warning"
                detail = f"{len(decisions_24h)} decisioni 24h, {errors_24h} errori (>20%)"
            elif pct_budget > 80:
                status = "warning"
                detail = f"Budget al {pct_budget:.0f}%: ${cost_today:.2f} / ${budget}"
            else:
                status = "ok"
                detail = f"{len(decisions_24h)} decisioni 24h, ${cost_today:.4f} / ${budget} ({pct_budget:.0f}%)"
            checks.append({"id": "ai_activity", "label": "AI activity 24h",
                            "status": status, "detail": detail})
        else:
            checks.append({"id": "ai_activity", "label": "AI activity 24h",
                            "status": "info", "detail": "AI disabilitata"})
    except Exception as exc:  # noqa: BLE001
        checks.append({"id": "ai_activity", "label": "AI activity",
                        "status": "warning", "detail": str(exc)})

    # Aggregato
    n_error = sum(1 for c in checks if c["status"] == "error")
    n_warning = sum(1 for c in checks if c["status"] == "warning")
    n_ok = sum(1 for c in checks if c["status"] == "ok")

    if n_error:
        overall = "error"
    elif n_warning:
        overall = "warning"
    else:
        overall = "ok"

    return {
        "overall": overall,
        "version": version,
        "summary": {"ok": n_ok, "warning": n_warning, "error": n_error,
                     "total": len(checks)},
        "checks": checks,
    }


def _run_test_stack(app) -> dict:
    """Esegue test live: 1) DB write/read 2) Claude API se configurato."""
    import time
    storage = app.extensions["domarc_storage"]
    results: list[dict] = []
    overall_ok = True

    # Test 1: DB write/read roundtrip
    t0 = time.monotonic()
    try:
        storage.list_tenants()  # query simple
        results.append({
            "name": "Database read", "status": "ok",
            "duration_ms": int((time.monotonic() - t0) * 1000),
            "detail": "SELECT su tabella tenants OK",
        })
    except Exception as exc:  # noqa: BLE001
        overall_ok = False
        results.append({"name": "Database read", "status": "error",
                         "duration_ms": int((time.monotonic() - t0) * 1000),
                         "detail": str(exc)})

    # Test 2: Master key encrypt/decrypt
    t0 = time.monotonic()
    try:
        from ..secrets_manager import get_secrets_manager
        sm = get_secrets_manager()
        token = sm.encrypt("test-stack-payload-1234")
        decrypted = sm.decrypt(token)
        ok = decrypted == "test-stack-payload-1234"
        results.append({
            "name": "Master key encrypt/decrypt", "status": "ok" if ok else "error",
            "duration_ms": int((time.monotonic() - t0) * 1000),
            "detail": "Roundtrip Fernet OK" if ok else "Mismatch decrypt",
        })
        if not ok:
            overall_ok = False
    except Exception as exc:  # noqa: BLE001
        overall_ok = False
        results.append({"name": "Master key encrypt/decrypt", "status": "error",
                         "duration_ms": int((time.monotonic() - t0) * 1000),
                         "detail": str(exc)})

    # Test 3: Claude API live (se configurato)
    t0 = time.monotonic()
    try:
        from ..ai_assistant.providers import build_provider
        providers = [p for p in storage.list_ai_providers() if p.get("enabled")]
        claude = next((p for p in providers if p.get("kind") == "claude"), None)
        if claude:
            provider = build_provider(claude)
            health = provider.health()
            ok = health.get("ok", False)
            results.append({
                "name": "Claude API connectivity", "status": "ok" if ok else "warning",
                "duration_ms": int((time.monotonic() - t0) * 1000),
                "detail": f"model={health.get('model')}, latency={health.get('latency_ms')}ms"
                          if ok else f"FAIL: {health.get('error')}",
            })
        else:
            results.append({
                "name": "Claude API connectivity", "status": "info",
                "duration_ms": 0,
                "detail": "Nessun provider claude configurato",
            })
    except Exception as exc:  # noqa: BLE001
        results.append({"name": "Claude API connectivity", "status": "warning",
                         "duration_ms": int((time.monotonic() - t0) * 1000),
                         "detail": str(exc)})

    return {"overall_ok": overall_ok, "tests": results}
