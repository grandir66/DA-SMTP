"""Blueprint UI per il modulo ai_assistant.

Pagine:
- /ai/                   → dashboard (KPI + redirect lazy a /ai/decisions)
- /ai/providers          → CRUD provider (Claude API, DGX Spark)
- /ai/models             → routing per job: tabella binding attivi + edit
- /ai/decisions          → tabella decisioni IA con filtri
- /ai/decisions/<id>     → dettaglio decision (prompt redacted, raw output)
- /ai/pii-dictionary     → gestione lista PII custom
"""
from __future__ import annotations

from flask import (Blueprint, current_app, flash, g, redirect, render_template,
                   request, session, url_for)

from ..auth import login_required

ai_bp = Blueprint("ai", __name__, url_prefix="/ai")


def _storage():
    return current_app.extensions["domarc_storage"]


def _tid() -> int:
    return int(getattr(g, "current_tenant_id", 1))


def _actor() -> str:
    return session.get("username") or "ui"


@ai_bp.route("/")
@login_required(role="admin")
def dashboard():
    storage = _storage()
    tid = _tid()
    decisions_24h = storage.list_ai_decisions(tenant_id=tid, hours=24, limit=2000)
    settings = {s["key"]: s["value"] for s in storage.list_settings()}
    spent_today = storage.sum_ai_decisions_cost_today(tenant_id=tid)
    by_job: dict[str, int] = {}
    by_intent: dict[str, int] = {}
    latencies: list[int] = []
    for d in decisions_24h:
        by_job[d["job_code"]] = by_job.get(d["job_code"], 0) + 1
        intent = d.get("intent") or "—"
        by_intent[intent] = by_intent.get(intent, 0) + 1
        if d.get("latency_ms"):
            latencies.append(int(d["latency_ms"]))
    latencies.sort()
    p50 = latencies[len(latencies) // 2] if latencies else 0
    p95 = latencies[int(len(latencies) * 0.95)] if latencies else 0
    return render_template(
        "admin/ai_dashboard.html",
        decisions_count=len(decisions_24h),
        spent_today=spent_today,
        budget=float(settings.get("ai_daily_budget_usd", "50") or 50),
        ai_enabled=(settings.get("ai_enabled", "false") or "").lower() == "true",
        shadow_mode=(settings.get("ai_shadow_mode", "true") or "").lower() == "true",
        by_job=sorted(by_job.items(), key=lambda x: -x[1]),
        by_intent=sorted(by_intent.items(), key=lambda x: -x[1]),
        latency_p50=p50, latency_p95=p95,
    )


@ai_bp.route("/providers")
@login_required(role="admin")
def providers_list():
    storage = _storage()
    providers = storage.list_ai_providers(tenant_id=_tid())
    return render_template("admin/ai_providers.html", providers=providers)


@ai_bp.route("/providers/new", methods=["GET", "POST"])
@ai_bp.route("/providers/<int:provider_id>", methods=["GET", "POST"])
@login_required(role="admin")
def provider_form(provider_id: int | None = None):
    storage = _storage()
    record: dict = {}
    if provider_id:
        rows = [p for p in storage.list_ai_providers(tenant_id=_tid())
                if p["id"] == provider_id]
        if not rows:
            flash("Provider non trovato.", "error")
            return redirect(url_for("ai.providers_list"))
        record = rows[0]
    if request.method == "POST":
        data = {
            "id": provider_id,
            "name": (request.form.get("name") or "").strip(),
            "kind": (request.form.get("kind") or "claude").strip(),
            "endpoint": (request.form.get("endpoint") or "").strip() or None,
            "api_key_env": (request.form.get("api_key_env") or "").strip() or None,
            "default_model": (request.form.get("default_model") or "").strip() or None,
            "enabled": (request.form.get("enabled") or "").lower() in ("on", "true", "1"),
            "notes": (request.form.get("notes") or "").strip() or None,
        }
        if not data["name"]:
            flash("Nome richiesto.", "error")
        else:
            new_id = storage.upsert_ai_provider(data, tenant_id=_tid(), actor=_actor())
            flash(f"Provider {'aggiornato' if provider_id else 'creato'} (id={new_id}).", "success")
            return redirect(url_for("ai.providers_list"))
    return render_template("admin/ai_provider_form.html", record=record,
                            is_new=provider_id is None)


@ai_bp.route("/providers/<int:provider_id>/test")
@login_required(role="admin")
def provider_test(provider_id: int):
    """Smoke test connettività al provider."""
    from ..ai_assistant.providers import get_ai_provider, AiProviderError
    storage = _storage()
    try:
        provider = get_ai_provider(storage, provider_id)
        health = provider.health()
    except AiProviderError as exc:
        health = {"ok": False, "error": str(exc)}
    if health.get("ok"):
        flash(f"Provider OK — modello: {health.get('model')}, latenza: {health.get('latency_ms')}ms", "success")
    else:
        flash(f"Provider FAIL: {health.get('error')}", "error")
    return redirect(url_for("ai.providers_list"))


@ai_bp.route("/providers/<int:provider_id>/delete", methods=["POST"])
@login_required(role="superadmin")
def provider_delete(provider_id: int):
    _storage().delete_ai_provider(provider_id)
    flash("Provider eliminato.", "success")
    return redirect(url_for("ai.providers_list"))


@ai_bp.route("/models")
@login_required(role="admin")
def models_list():
    storage = _storage()
    tid = _tid()
    bindings = storage.list_ai_job_bindings(tenant_id=tid)
    providers = {p["id"]: p for p in storage.list_ai_providers(tenant_id=tid)}
    jobs = storage.list_ai_jobs()
    # Solo bindings con version maggiore per ogni job_code (cioè la "corrente")
    bindings_grouped: dict[str, list] = {}
    for b in bindings:
        bindings_grouped.setdefault(b["job_code"], []).append(b)
    return render_template("admin/ai_models.html",
                            bindings_grouped=bindings_grouped,
                            providers=providers, jobs=jobs)


@ai_bp.route("/models/new", methods=["GET", "POST"])
@ai_bp.route("/models/<int:binding_id>", methods=["GET", "POST"])
@login_required(role="admin")
def model_form(binding_id: int | None = None):
    storage = _storage()
    tid = _tid()
    providers = storage.list_ai_providers(tenant_id=tid)
    jobs = storage.list_ai_jobs()
    record: dict = {}
    if binding_id:
        rows = [b for b in storage.list_ai_job_bindings(tenant_id=tid)
                if b["id"] == binding_id]
        if not rows:
            flash("Binding non trovato.", "error")
            return redirect(url_for("ai.models_list"))
        record = rows[0]
    if request.method == "POST":
        data = {
            "id": binding_id,
            "job_code": request.form.get("job_code"),
            "provider_id": int(request.form.get("provider_id") or 0) or None,
            "model_id": (request.form.get("model_id") or "").strip(),
            "system_prompt_template": (request.form.get("system_prompt_template") or "").strip() or None,
            "user_prompt_template": (request.form.get("user_prompt_template") or "").strip() or None,
            "temperature": float(request.form.get("temperature") or 0.0),
            "max_tokens": int(request.form.get("max_tokens") or 1024),
            "timeout_ms": int(request.form.get("timeout_ms") or 5000),
            "fallback_provider_id": (
                int(request.form.get("fallback_provider_id") or 0) or None
            ),
            "fallback_model_id": (request.form.get("fallback_model_id") or "").strip() or None,
            "traffic_split": int(request.form.get("traffic_split") or 100),
            "enabled": (request.form.get("enabled") or "").lower() in ("on", "true", "1"),
            "notes": (request.form.get("notes") or "").strip() or None,
        }
        new_version = (request.form.get("new_version") or "").lower() in ("on", "true", "1")
        new_id = storage.upsert_ai_job_binding(
            data, tenant_id=tid, actor=_actor(), new_version=new_version,
        )
        # Invalidate router cache dopo write
        from ..ai_assistant.router import get_ai_router
        get_ai_router(storage, tenant_id=tid).invalidate_cache()
        flash(f"Binding salvato (id={new_id}).", "success")
        return redirect(url_for("ai.models_list"))
    return render_template("admin/ai_model_form.html", record=record,
                            providers=providers, jobs=jobs,
                            is_new=binding_id is None)


@ai_bp.route("/decisions")
@login_required(role="admin")
def decisions_list():
    storage = _storage()
    tid = _tid()
    job_filter = (request.args.get("job") or "").strip() or None
    hours = int(request.args.get("hours") or 24)
    decisions = storage.list_ai_decisions(tenant_id=tid, job_code=job_filter,
                                           hours=hours, limit=200)
    return render_template("admin/ai_decisions.html",
                            decisions=decisions, job_filter=job_filter, hours=hours)


@ai_bp.route("/decisions/<int:decision_id>")
@login_required(role="admin")
def decision_detail(decision_id: int):
    storage = _storage()
    decision = storage.get_ai_decision(decision_id)
    if not decision:
        flash("Decisione non trovata.", "error")
        return redirect(url_for("ai.decisions_list"))
    return render_template("admin/ai_decision_detail.html", decision=decision)


@ai_bp.route("/shadow-mode", methods=["GET", "POST"])
@login_required(role="admin")
def shadow_mode_switch():
    """Pagina di gestione del passaggio shadow ↔ live.

    Sicurezza:
    - Per passare a LIVE servono ≥ N decisioni shadow osservate
      (setting `ai_shadow_min_decisions_before_live`, default 50).
    - Confidence media delle ultime decisioni mostrata per valutare la qualità.
    - Conferma multi-step (textbox "CONFERMO" + ruolo admin/superadmin).
    - Operazione tracciata in `ai_shadow_audit`.
    """
    storage = _storage()
    tid = _tid()
    settings = {s["key"]: s["value"] for s in storage.list_settings()}
    is_shadow = (settings.get("ai_shadow_mode", "true") or "").lower() == "true"
    min_decisions = int(settings.get("ai_shadow_min_decisions_before_live", "50") or 50)
    min_conf = float(settings.get("ai_apply_min_confidence", "0.85") or 0.85)

    # Quanta osservazione abbiamo? (decisioni 7gg)
    decisions_7d = storage.list_ai_decisions(tenant_id=tid, hours=24*7, limit=5000)
    decisions_count = len(decisions_7d)
    valid_decisions = [d for d in decisions_7d if d.get("confidence") is not None
                        and not d.get("error")]
    avg_conf = (sum(float(d.get("confidence") or 0) for d in valid_decisions)
                / len(valid_decisions)) if valid_decisions else 0.0
    error_count = sum(1 for d in decisions_7d if d.get("error"))
    high_conf_count = sum(1 for d in valid_decisions
                           if float(d.get("confidence") or 0) >= min_conf)
    audit = storage.list_ai_shadow_audit(tenant_id=tid, limit=20)

    if request.method == "POST":
        target = (request.form.get("target") or "").lower()
        confirm = (request.form.get("confirm") or "").strip()
        notes = (request.form.get("notes") or "").strip() or None
        if confirm != "CONFERMO":
            flash("Devi scrivere esattamente 'CONFERMO' per procedere.", "error")
            return redirect(url_for("ai.shadow_mode_switch"))

        if target == "live":
            # Check pre-flight
            if not is_shadow:
                flash("Sistema già in LIVE mode.", "error")
                return redirect(url_for("ai.shadow_mode_switch"))
            if decisions_count < min_decisions:
                flash(
                    f"Servono almeno {min_decisions} decisioni shadow osservate prima di passare in live "
                    f"(attualmente: {decisions_count}). Modifica `ai_shadow_min_decisions_before_live` "
                    "in Settings per cambiare la soglia.",
                    "error",
                )
                return redirect(url_for("ai.shadow_mode_switch"))
            with storage.transaction() as conn:
                conn.execute(
                    "UPDATE settings SET value='false' WHERE key='ai_shadow_mode'"
                )
            storage.insert_ai_shadow_audit(
                tenant_id=tid, transition="shadow_to_live",
                actor=_actor(), decisions_seen=decisions_count,
                avg_confidence=avg_conf, notes=notes,
            )
            flash(f"✓ Sistema passato in LIVE mode. Le decisioni IA con confidence ≥ {min_conf} "
                  "saranno applicate. Audit log registrato.", "success")
        elif target == "shadow":
            if is_shadow:
                flash("Sistema già in SHADOW mode.", "error")
                return redirect(url_for("ai.shadow_mode_switch"))
            with storage.transaction() as conn:
                conn.execute(
                    "UPDATE settings SET value='true' WHERE key='ai_shadow_mode'"
                )
            storage.insert_ai_shadow_audit(
                tenant_id=tid, transition="live_to_shadow",
                actor=_actor(), decisions_seen=decisions_count,
                avg_confidence=avg_conf, notes=notes,
            )
            flash("✓ Sistema rientrato in SHADOW mode. Decisioni solo loggate.", "success")
        else:
            flash("Target non valido (deve essere 'live' o 'shadow').", "error")
        return redirect(url_for("ai.shadow_mode_switch"))

    return render_template(
        "admin/ai_shadow_mode.html",
        is_shadow=is_shadow,
        decisions_count=decisions_count,
        min_decisions=min_decisions,
        min_conf=min_conf,
        avg_conf=avg_conf,
        error_count=error_count,
        high_conf_count=high_conf_count,
        valid_decisions_count=len(valid_decisions),
        audit=audit,
        ready_to_go_live=(is_shadow and decisions_count >= min_decisions),
    )


@ai_bp.route("/rules-overview")
@login_required(role="admin")
def rules_overview():
    """Overview di tutte le regole che invocano l'IA + statistiche correlate.

    Mostra:
    - Tutte le regole (orfane + figli) con action ai_classify o ai_critical_check.
    - Binding attivo per il job_code richiesto (provider/model/version).
    - Conteggio decisioni IA delle ultime 24h per quella regola (joint via
      events.payload_metadata.ai_decision_id ↔ ai_decisions, derivato).
    - Costo cumulativo 24h per regola.
    - Stato globale (ai_enabled, ai_shadow_mode, budget remaining).
    """
    storage = _storage()
    tid = _tid()

    # Tutte le regole con action IA
    all_rules = storage.list_rules(tenant_id=tid)
    ai_rules = [r for r in all_rules
                if r.get("action") in ("ai_classify", "ai_critical_check")]

    # Mappa job_code → action_name (oggi 1:1 ma teniamolo flessibile)
    def _job_for_action(action: str) -> str:
        if action == "ai_classify":
            return "classify_email"
        if action == "ai_critical_check":
            return "critical_classify"
        return action

    # Bindings attivi per ciascun job
    bindings = storage.list_ai_job_bindings(tenant_id=tid, only_enabled=True)
    bindings_by_job: dict[str, list[dict]] = {}
    for b in bindings:
        bindings_by_job.setdefault(b["job_code"], []).append(b)
    providers = {p["id"]: p for p in storage.list_ai_providers(tenant_id=tid)}

    # Decisioni IA 24h con event_uuid per join
    decisions_24h = storage.list_ai_decisions(tenant_id=tid, hours=24, limit=2000)
    # Group by event_uuid per fast lookup
    decision_by_event: dict[str, dict] = {
        d["event_uuid"]: d for d in decisions_24h if d.get("event_uuid")
    }
    # Eventi 24h con rule_id per joint-derived stats
    events_24h, _ = storage.list_events(tenant_id=tid, hours=24, page=1,
                                          page_size=10000)

    # Stats per regola
    stats_by_rule: dict[int, dict] = {}
    for r in ai_rules:
        stats_by_rule[r["id"]] = {
            "decisions_count": 0,
            "cost_usd": 0.0,
            "shadow_count": 0,
            "applied_count": 0,
            "error_count": 0,
            "failsafe_count": 0,
        }
    for evt in events_24h:
        rid = evt.get("rule_id")
        if not rid or rid not in stats_by_rule:
            continue
        s = stats_by_rule[rid]
        # ai_decision_id da payload_metadata
        pm = evt.get("payload_metadata") or {}
        if not isinstance(pm, dict):
            continue
        if pm.get("ai_unavailable"):
            s["failsafe_count"] += 1
            continue
        ai_decision_id = pm.get("ai_decision_id")
        if not ai_decision_id:
            continue
        # Risali alla decisione via event_uuid (più affidabile)
        evt_uuid = evt.get("relay_event_uuid")
        d = decision_by_event.get(evt_uuid) if evt_uuid else None
        if d:
            s["decisions_count"] += 1
            s["cost_usd"] += float(d.get("cost_usd") or 0)
            if d.get("error"):
                s["error_count"] += 1
            elif d.get("shadow_mode"):
                s["shadow_count"] += 1
            else:
                s["applied_count"] += 1

    # Settings globali
    settings = {s["key"]: s["value"] for s in storage.list_settings()}
    spent_today = storage.sum_ai_decisions_cost_today(tenant_id=tid)

    return render_template(
        "admin/ai_rules_overview.html",
        ai_rules=ai_rules,
        bindings_by_job=bindings_by_job,
        providers=providers,
        stats_by_rule=stats_by_rule,
        job_for_action=_job_for_action,
        ai_enabled=(settings.get("ai_enabled", "false") or "").lower() == "true",
        shadow_mode=(settings.get("ai_shadow_mode", "true") or "").lower() == "true",
        budget=float(settings.get("ai_daily_budget_usd", "50") or 50),
        spent_today=spent_today,
    )


@ai_bp.route("/pii-dictionary", methods=["GET", "POST"])
@login_required(role="admin")
def pii_dictionary():
    storage = _storage()
    tid = _tid()
    if request.method == "POST":
        kind = (request.form.get("kind") or "person").strip()
        value = (request.form.get("value") or "").strip()
        replacement = (request.form.get("replacement") or "").strip() or "[REDACTED]"
        if value:
            storage.upsert_ai_pii_dictionary_entry(
                tenant_id=tid, kind=kind, value=value,
                replacement=replacement, source="manual",
            )
            flash("Voce aggiunta al dizionario PII.", "success")
        return redirect(url_for("ai.pii_dictionary"))
    entries = storage.list_ai_pii_dictionary(tenant_id=tid)
    return render_template("admin/ai_pii_dictionary.html", entries=entries)
