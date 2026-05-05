"""Rules CRUD blueprint per Domarc SMTP Relay Admin."""
from __future__ import annotations

import csv
import io
import json
import re
from datetime import datetime, timezone

from flask import Blueprint, Response, abort, current_app, flash, g, jsonify, redirect, render_template, request, session, url_for

from ..auth import login_required
from ..rules.wizard_presets import (
    CATEGORIES as RULE_WIZARD_CATEGORIES,
    PRESETS as RULE_WIZARD_PRESETS,
    get_preset,
    presets_by_category,
)

rules_bp = Blueprint("rules", __name__)


def _storage():
    return current_app.extensions["domarc_storage"]


def _tid() -> int:
    return int(getattr(g, "current_tenant_id", 1))


@rules_bp.route("/rules")
@login_required()
def list_view():
    state = (request.args.get("state") or "all").lower()
    only_enabled = True if state == "enabled" else (False if state == "disabled" else None)
    storage = _storage()
    # M029: filtro per rule_set tramite tab/query string
    set_param = request.args.get("set")
    rule_set_id: int | None = None
    if set_param and set_param != "all":
        try:
            rule_set_id = int(set_param)
        except ValueError:
            rule_set_id = None
    items = storage.list_rules_grouped(
        tenant_id=_tid(), only_enabled=only_enabled, rule_set_id=rule_set_id,
    )
    sets = storage.list_rule_sets(tenant_id=_tid())
    counts = storage.count_rules_per_set(tenant_id=_tid())
    return render_template(
        "admin/rules_list.html",
        items=items,
        filter_state=state,
        rule_sets=sets,
        rule_set_counts=counts,
        active_set_id=rule_set_id,
    )


@rules_bp.route("/rules/wizard", methods=["GET", "POST"])
@login_required(role="operator")
def wizard_view():
    """Wizard guidato per creare regole comuni in 3 step.

    GET: mostra catalogo preset + form personalizzazione + preview.
    POST: applica preset + override utente, crea regola, redirect al form
          completo per rifinitura.
    """
    storage = _storage()

    if request.method == "POST":
        preset_id = (request.form.get("preset_id") or "").strip()
        preset = get_preset(preset_id)
        if not preset:
            flash("Preset non valido.", "error")
            return redirect(url_for("rules.wizard_view"))

        # Costruisci la regola: defaults del preset + override dal form
        defaults = dict(preset.get("defaults") or {})
        action_map = dict(defaults.get("action_map") or {})

        # Risoluzione rule_set_id da code (preset o override)
        rule_set_code = (request.form.get("rule_set_code")
                         or defaults.get("rule_set_code") or "globali").strip()
        rs = storage.get_rule_set_by_code(rule_set_code, tenant_id=_tid())
        if not rs:
            rs = storage.get_rule_set_by_code("globali", tenant_id=_tid())
        rule_set_id = rs["id"] if rs else None

        # Override testuali dal form (vuoti -> non sovrascrivono il preset)
        def _take(key: str) -> str | None:
            v = (request.form.get(key) or "").strip()
            return v if v else None

        match_from_regex = _take("match_from_regex") or defaults.get("match_from_regex")
        match_to_regex = _take("match_to_regex") or defaults.get("match_to_regex")
        match_subject_regex = _take("match_subject_regex") or defaults.get("match_subject_regex")
        match_from_domain = _take("match_from_domain") or defaults.get("match_from_domain")
        match_to_domain = _take("match_to_domain") or defaults.get("match_to_domain")

        # Recipient group e forward
        match_to_group_id = request.form.get("match_to_group_id")
        match_to_group_id = int(match_to_group_id) if match_to_group_id and match_to_group_id.isdigit() else None
        forward_to_emails = _take("forward_to_emails")
        forward_to_group_id = request.form.get("forward_to_group_id")
        forward_to_group_id = int(forward_to_group_id) if forward_to_group_id and forward_to_group_id.isdigit() else None

        # action_map fields dal form
        for k in ("settore", "urgenza", "reason", "ack_template_id",
                  "reject_template_id"):
            v = _take(f"am_{k}")
            if v is not None:
                if k.endswith("_id"):
                    try:
                        action_map[k] = int(v)
                    except ValueError:
                        pass
                else:
                    action_map[k] = v
        if request.form.get("am_keep_original_delivery") in ("on", "true", "1"):
            action_map["keep_original_delivery"] = True
        # Template per auto_reply
        tpl_id = _take("am_template_id")
        if tpl_id:
            try:
                action_map["template_id"] = int(tpl_id)
            except ValueError:
                pass

        # match_in_service tristate (preset o override)
        mis_form = request.form.get("match_in_service")
        if mis_form in ("true", "false"):
            match_in_service = (mis_form == "true")
        elif mis_form == "":
            match_in_service = None
        else:
            match_in_service = defaults.get("match_in_service")

        # Action eventualmente customizzabile (solo per il preset specifico_contratto)
        action = (request.form.get("action") or defaults.get("action") or "ignore").strip()

        # Nome auto-generato + descrizione che ricorda il preset di origine
        name = (request.form.get("name") or "").strip() or f"{preset['title']}"
        description = (request.form.get("description") or "").strip() or (
            f"Creata da wizard ({preset['id']}). {preset['description']}"
        )

        # Priority: prendi suggerita dal preset, ma cerca un valore libero nel set/scope
        priority = int(request.form.get("priority")
                       or defaults.get("priority_hint") or 200)

        rule_data = {
            "name": name,
            "description": description,
            "rule_set_id": rule_set_id,
            "scope_type": "global",
            "priority": priority,
            "enabled": True,
            "match_from_regex": match_from_regex,
            "match_to_regex": match_to_regex,
            "match_subject_regex": match_subject_regex,
            "match_from_domain": match_from_domain,
            "match_to_domain": match_to_domain,
            "match_to_group_id": match_to_group_id,
            "match_in_service": match_in_service,
            "forward_to_emails": forward_to_emails,
            "forward_to_group_id": forward_to_group_id,
            "action": action,
            "action_map": action_map if action_map else None,
        }

        try:
            new_id = storage.upsert_rule(
                rule_data, tenant_id=_tid(),
                created_by=session.get("username") or "wizard",
            )
            flash(f"Regola creata dal wizard (id={new_id}). "
                  f"Personalizzala se necessario.", "success")
            return redirect(url_for("rules.form_view", rule_id=new_id))
        except ValueError as exc:
            flash(f"Errore validazione: {exc}", "error")
            return redirect(url_for("rules.wizard_view") + f"?preset={preset_id}")

    # GET: mostra wizard
    rule_sets = storage.list_rule_sets(tenant_id=_tid(), only_enabled=True)
    templates = storage.list_templates(tenant_id=_tid(), only_enabled=True)
    recipient_groups = storage.list_recipient_groups(tenant_id=_tid(), only_enabled=True)
    initial_preset = (request.args.get("preset") or "").strip() or None

    return render_template(
        "admin/rules_wizard.html",
        presets=RULE_WIZARD_PRESETS,
        categories=RULE_WIZARD_CATEGORIES,
        presets_grouped=presets_by_category(),
        rule_sets=rule_sets,
        templates=templates,
        recipient_groups=recipient_groups,
        initial_preset=initial_preset,
    )


@rules_bp.route("/rules/new", methods=["GET", "POST"])
@rules_bp.route("/rules/<int:rule_id>", methods=["GET", "POST"])
@login_required(role="operator")
def form_view(rule_id: int | None = None):
    is_new = rule_id is None
    record: dict = {}
    if not is_new:
        record = _storage().get_rule(rule_id) or {}
        if not record:
            flash("Regola non trovata", "error")
            return redirect(url_for("rules.list_view"))
    elif request.method == "GET":
        # Prefill da query string (es. da event_detail / events_list)
        pre_from = request.args.get("prefill_from")
        pre_to = request.args.get("prefill_to")
        pre_subject = request.args.get("prefill_subject")
        if pre_from:
            record["match_from_regex"] = "(?i)^" + re.escape(pre_from.strip()) + "$"
        if pre_to:
            record["match_to_regex"] = "(?i)^" + re.escape(pre_to.strip()) + "$"
        if pre_subject:
            record["match_subject_regex"] = "(?i)^" + re.escape(pre_subject.strip()[:80])
        if pre_from or pre_to or pre_subject:
            record.setdefault("name", "Da evento — " + (pre_from or pre_to or pre_subject or "")[:60])
            record.setdefault("priority", 100)

    templates = _storage().list_templates(tenant_id=_tid(), only_enabled=True)

    # Lista domini noti (autocomplete del campo From dominio)
    known_domains: list[str] = []
    cs = current_app.extensions.get("domarc_customer_source")
    if cs:
        seen: set[str] = set()
        for c in cs.list_customers():
            for d in (c.domains or []):
                d2 = (d or "").strip().lower()
                if d2 and d2 not in seen:
                    seen.add(d2)
                    known_domains.append(d2)
        known_domains.sort()

    # Info per banner "creata da evento" nel form (se presente)
    from_event_id = None
    if is_new and request.method == "GET":
        try:
            fe = request.args.get("from_event")
            if fe:
                from_event_id = int(fe)
        except (TypeError, ValueError):
            pass

    if request.method == "POST":
        data = _parse_form(request.form)
        # Validazione: match_to_regex e match_to_group_id sono mutuamente esclusivi
        if data.get("match_to_regex") and data.get("match_to_group_id"):
            flash("Impossibile usare match_to_regex e match_to_group_id contemporaneamente. "
                  "Scegli una delle due modalità.", "error")
            return render_template("admin/rule_form.html", is_new=is_new,
                                     record={**(record or {}), **data},
                                     templates=templates, from_event_id=from_event_id,
                                     known_domains=known_domains,
                                     customer_groups=[],
                                     recipient_groups=_storage().list_recipient_groups(
                                         tenant_id=_tid(), only_enabled=True))
        try:
            if not is_new:
                data["id"] = rule_id
            new_id = _storage().upsert_rule(data, tenant_id=_tid(),
                                             created_by=session.get("username") or "ui")
            flash(f"Regola {'creata' if is_new else 'aggiornata'}.", "success")
            return redirect(url_for("rules.form_view", rule_id=new_id))
        except ValueError as exc:
            flash(str(exc), "error")
            record = {**record, **data}

    # Context IA: popolato sempre, il template mostra il pannello solo se
    # action == 'ai_classify' / 'ai_critical_check'.
    ai_active_bindings, ai_providers_map, ai_global_status, ai_recent_decisions = \
        _build_ai_form_context(rule_id)

    from ..customer_groups_virtuals import merge_with_virtuals
    real_groups = _storage().list_customer_groups(tenant_id=_tid())
    db_path = current_app.extensions["domarc_config"].db_path
    customer_groups = merge_with_virtuals(real_groups, db_path, _tid())
    recipient_groups = _storage().list_recipient_groups(tenant_id=_tid(),
                                                          only_enabled=True)
    # Profili orari per preset orario (Refactor form 2026-05-05).
    try:
        profiles = _storage().list_profiles(tenant_id=_tid())
    except Exception:  # noqa: BLE001
        profiles = []

    # M029: lista rule_sets disponibili + prefill da query string
    rule_sets = _storage().list_rule_sets(tenant_id=_tid(), only_enabled=True)
    prefill_set_id: int | None = None
    if is_new:
        try:
            sp = request.args.get("set")
            if sp:
                prefill_set_id = int(sp)
        except (TypeError, ValueError):
            prefill_set_id = None

    return render_template(
        "admin/rule_form.html",
        is_new=is_new,
        record=record,
        templates=templates,
        from_event_id=from_event_id,
        known_domains=known_domains,
        customer_groups=customer_groups,
        recipient_groups=recipient_groups,
        profiles=profiles,
        ai_active_bindings=ai_active_bindings,
        ai_providers=ai_providers_map,
        ai_global_status=ai_global_status,
        ai_recent_decisions=ai_recent_decisions,
        rule_sets=rule_sets,
        prefill_set_id=prefill_set_id,
    )


def _build_ai_form_context(rule_id: int | None) -> tuple[dict, dict, dict, list]:
    """Costruisce il context IA mostrato nei form regola (orfana/figlio).

    Returns:
        (active_bindings_by_job, providers_by_id, global_status, recent_decisions_for_rule)
    """
    storage = _storage()
    tid = _tid()
    bindings = storage.list_ai_job_bindings(tenant_id=tid, only_enabled=True)
    active_bindings: dict[str, list] = {}
    for b in bindings:
        active_bindings.setdefault(b["job_code"], []).append(b)
    providers = {p["id"]: p for p in storage.list_ai_providers(tenant_id=tid)}
    settings = {s["key"]: s["value"] for s in storage.list_settings()}
    spent_today = 0.0
    try:
        spent_today = storage.sum_ai_decisions_cost_today(tenant_id=tid)
    except (NotImplementedError, AttributeError):
        spent_today = 0.0
    global_status = {
        "master_enabled": (settings.get("ai_enabled", "false") or "").lower() == "true",
        "shadow_mode": (settings.get("ai_shadow_mode", "true") or "").lower() == "true",
        "spent_today": spent_today,
        "budget": float(settings.get("ai_daily_budget_usd", "50") or 50),
    }
    # Ultime 5 decisioni invocate da questa regola (joint via events.payload_metadata.ai_decision_id)
    recent_decisions: list = []
    if rule_id:
        events_24h, _ = storage.list_events(tenant_id=tid, hours=72, page=1, page_size=2000)
        decision_ids = []
        for evt in events_24h:
            if evt.get("rule_id") != rule_id:
                continue
            pm = evt.get("payload_metadata") or {}
            if isinstance(pm, dict) and pm.get("ai_decision_id"):
                decision_ids.append(pm["ai_decision_id"])
        # Carica decisioni
        if decision_ids:
            all_dec = storage.list_ai_decisions(tenant_id=tid, hours=72, limit=500)
            ids_set = set(decision_ids)
            recent_decisions = [d for d in all_dec if d["id"] in ids_set][:5]
    return active_bindings, providers, global_status, recent_decisions


@rules_bp.route("/rules/<int:rule_id>/delete", methods=["POST"])
@login_required(role="admin")
def delete_view(rule_id: int):
    _storage().delete_rule(rule_id)
    flash("Regola eliminata.", "success")
    return redirect(url_for("rules.list_view"))


@rules_bp.route("/rules/<int:rule_id>/toggle", methods=["POST"])
@login_required(role="operator")
def toggle_view(rule_id: int):
    _storage().toggle_rule(rule_id)
    return redirect(request.referrer or url_for("rules.list_view"))


@rules_bp.route("/rules/bulk", methods=["POST"])
@login_required(role="operator")
def bulk_action_view():
    """Bulk: 'duplicate_to_set' | 'move_to_set' | 'enable' | 'disable' | 'delete'.

    Form fields: action, target_set_id (per duplicate/move), rule_ids[] (lista id).
    """
    storage = _storage()
    action = (request.form.get("action") or "").strip()
    rule_ids = [int(x) for x in request.form.getlist("rule_ids") if x.isdigit()]
    if not rule_ids:
        flash("Nessuna regola selezionata.", "error")
        return redirect(request.referrer or url_for("rules.list_view"))

    target_set_id_raw = request.form.get("target_set_id")
    target_set_id: int | None = None
    if target_set_id_raw:
        try:
            target_set_id = int(target_set_id_raw)
        except ValueError:
            target_set_id = None

    actor = session.get("username") or "ui"

    try:
        if action == "duplicate_to_set":
            if target_set_id is None:
                flash("Set di destinazione obbligatorio per la duplicazione.", "error")
                return redirect(request.referrer or url_for("rules.list_view"))
            n = 0
            for rid in rule_ids:
                try:
                    storage.duplicate_rule(rid, target_set_id=target_set_id, actor=actor)
                    n += 1
                except Exception:  # noqa: BLE001
                    pass
            target_set = storage.get_rule_set(target_set_id)
            tname = target_set.get("name") if target_set else f"#{target_set_id}"
            flash(f"Duplicate {n} regole nel set '{tname}'.", "success")
        elif action == "move_to_set":
            if target_set_id is None:
                flash("Set di destinazione obbligatorio per lo spostamento.", "error")
                return redirect(request.referrer or url_for("rules.list_view"))
            n = storage.move_rules_to_set(rule_ids, target_set_id)
            target_set = storage.get_rule_set(target_set_id)
            tname = target_set.get("name") if target_set else f"#{target_set_id}"
            flash(f"Spostate {n} regole nel set '{tname}'.", "success")
        elif action == "enable":
            for rid in rule_ids:
                try:
                    r = storage.get_rule(rid)
                    if r and not r.get("enabled"):
                        storage.toggle_rule(rid)
                except Exception:  # noqa: BLE001
                    pass
            flash(f"Abilitate {len(rule_ids)} regole.", "success")
        elif action == "disable":
            for rid in rule_ids:
                try:
                    r = storage.get_rule(rid)
                    if r and r.get("enabled"):
                        storage.toggle_rule(rid)
                except Exception:  # noqa: BLE001
                    pass
            flash(f"Disabilitate {len(rule_ids)} regole.", "success")
        else:
            flash(f"Azione bulk non supportata: {action}", "error")
    except Exception as exc:  # noqa: BLE001
        flash(f"Errore bulk: {exc}", "error")
    return redirect(request.referrer or url_for("rules.list_view"))


@rules_bp.route("/rules/<int:rule_id>/duplicate", methods=["POST"])
@login_required(role="operator")
def duplicate_view(rule_id: int):
    """Duplica una regola. Param ``target_set_id`` opzionale per copia
    cross-set (M029)."""
    src = _storage().get_rule(rule_id)
    if not src:
        flash("Regola non trovata", "error")
        return redirect(url_for("rules.list_view"))
    target_set_id_raw = request.form.get("target_set_id")
    target_set_id: int | None = None
    if target_set_id_raw:
        try:
            target_set_id = int(target_set_id_raw)
        except ValueError:
            target_set_id = None
    try:
        new_id = _storage().duplicate_rule(
            rule_id,
            target_set_id=target_set_id,
            actor=session.get("username") or "ui",
        )
        if target_set_id:
            target_set = _storage().get_rule_set(target_set_id)
            target_name = target_set.get("name") if target_set else f"#{target_set_id}"
            flash(f"Regola duplicata nel set '{target_name}' (id={new_id}).", "success")
        else:
            flash(f"Regola duplicata (id={new_id}).", "success")
        return redirect(url_for("rules.form_view", rule_id=new_id))
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("rules.list_view"))


@rules_bp.route("/rules/export.<fmt>")
@login_required()
def export_view(fmt: str):
    if fmt not in ("csv", "json"):
        abort(404)
    rules = _storage().list_rules(tenant_id=_tid())
    fname_ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    if fmt == "json":
        return jsonify({"tenant_id": _tid(), "count": len(rules), "rules": rules})
    buf = io.StringIO()
    if rules:
        # Espandi action_map (dict) → JSON string per CSV
        flat = []
        for r in rules:
            row = dict(r)
            am = row.get("action_map")
            if isinstance(am, (dict, list)):
                row["action_map"] = json.dumps(am, ensure_ascii=False)
            flat.append(row)
        keys = sorted({k for r in flat for k in r.keys()})
        writer = csv.DictWriter(buf, fieldnames=keys)
        writer.writeheader()
        for r in flat:
            writer.writerow({k: ("" if r.get(k) is None else str(r.get(k))) for k in keys})
    return Response(
        buf.getvalue(), mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition":
                 f"attachment; filename=domarc-rules-tenant{_tid()}-{fname_ts}.csv"},
    )


@rules_bp.route("/rules/lookup-domain", methods=["GET"])
@login_required(role="tech")
def lookup_domain():
    """Risolve un dominio mittente nei dati del customer source attivo.

    Ritorna info live: codcli, ragione_sociale, profilo orari, contratto attivo,
    eccezione attiva oggi (se ha service_hours). Usato dalla UI rule form per
    mostrare contesto quando si compila `match_from_domain`.
    """
    from datetime import date as _date
    domain = (request.args.get("domain") or "").strip().lower()
    if not domain:
        return jsonify({"ok": False, "error": "domain richiesto"})
    cs = current_app.extensions.get("domarc_customer_source")
    if not cs:
        return jsonify({"ok": False, "error": "customer source non disponibile"})
    matches: list[dict] = []
    for c in cs.list_customers():
        if domain in [d.lower() for d in (c.domains or [])]:
            matches.append({
                "codice_cliente": c.codice_cliente,
                "ragione_sociale": c.ragione_sociale,
                "profile_code": c.tipologia_servizio,
                "profile_description": c.profile_description,
                "is_active": bool(c.is_active),
                "contract_type": c.contract_type,
            })
    # Eccezioni attive oggi (da service_hours.schedule_exceptions)
    today_iso = _date.today().isoformat()
    exceptions_today: list[dict] = []
    storage = _storage()
    for m in matches:
        try:
            sh = storage.get_service_hours(m["codice_cliente"], _tid())
        except Exception:
            sh = None
        if not sh:
            continue
        for exc in (sh.get("schedule_exceptions") or []):
            if (exc.get("date") or "") == today_iso:
                exceptions_today.append({
                    "codcli": m["codice_cliente"],
                    "schedule": exc.get("schedule"),
                    "note": exc.get("note"),
                })
    return jsonify({
        "ok": True,
        "domain": domain,
        "is_known": len(matches) > 0,
        "matches": matches,
        "ambiguous": len(matches) > 1,
        "exceptions_today": exceptions_today,
        "today": today_iso,
    })


@rules_bp.route("/rules/test-regex", methods=["POST"])
@login_required(role="operator")
def test_regex():
    payload = request.get_json(silent=True) or {}
    pattern = (payload.get("pattern") or "").strip()
    sample = payload.get("sample") or ""
    if not pattern:
        return jsonify({"ok": False, "error": "Pattern vuoto"})
    try:
        m = re.search(pattern, sample[:16384], re.IGNORECASE)
    except re.error as exc:
        return jsonify({"ok": False, "error": f"Regex invalida: {exc}"})
    if not m:
        return jsonify({"ok": True, "matched": False})
    return jsonify({
        "ok": True, "matched": True, "match": m.group(0),
        "groups": list(m.groups()),
    })


@rules_bp.route("/rules/groups/new", methods=["GET", "POST"])
@rules_bp.route("/rules/groups/<int:group_id>", methods=["GET", "POST"])
@login_required(role="operator")
def group_form_view(group_id: int | None = None):
    is_new = group_id is None
    record: dict = {}
    if not is_new:
        record = _storage().get_rule(group_id) or {}
        if not record or not record.get("is_group"):
            flash("Gruppo non trovato", "error")
            return redirect(url_for("rules.list_view"))

    if request.method == "POST":
        from ..rules.action_map_schema import PARENT_ACTION_MAP_DEFAULTS
        # Action_map del gruppo: SOLO whitelist defaults ereditabili
        action_map: dict = {}
        for k in PARENT_ACTION_MAP_DEFAULTS:
            v = (request.form.get(f"action_map_{k}") or "").strip()
            if v:
                if k in ("auth_code_ttl_hours",):
                    try: action_map[k] = int(v)
                    except ValueError: pass
                else:
                    action_map[k] = v
        for flag in ("keep_original_delivery", "generate_auth_code", "apply_rules",
                     "reply_quote_original", "reply_attach_original"):
            if (request.form.get(f"action_map_{flag}") or "").lower() in ("on", "true", "1"):
                action_map[flag] = True

        def _tristate(v):
            if not v: return None
            return v == "true"

        data = {
            "name": request.form.get("name") or f"[GRUPPO] {request.form.get('group_label') or 'senza nome'}",
            "group_label": request.form.get("group_label") or None,
            "is_group": 1,
            "action": "group",
            "scope_type": request.form.get("scope_type") or "global",
            "scope_ref": request.form.get("scope_ref") or None,
            "priority": int(request.form.get("priority") or 100),
            "enabled": (request.form.get("enabled") or "").lower() in ("on", "true", "1"),
            "match_from_regex": request.form.get("match_from_regex") or None,
            "match_from_domain": request.form.get("match_from_domain") or None,
            "match_to_regex": request.form.get("match_to_regex") or None,
            "match_subject_regex": request.form.get("match_subject_regex") or None,
            "match_body_regex": request.form.get("match_body_regex") or None,
            "match_to_domain": request.form.get("match_to_domain") or None,
            "match_at_hours": request.form.get("match_at_hours") or None,
            "match_in_service": _tristate(request.form.get("match_in_service")),
            "match_contract_active": _tristate(request.form.get("match_contract_active")),
            "match_known_customer": _tristate(request.form.get("match_known_customer")),
            "match_has_exception_today": _tristate(request.form.get("match_has_exception_today")),
            "action_map": action_map or None,
            "exclusive_match": (request.form.get("exclusive_match") or "").lower() in ("on", "true", "1"),
        }
        try:
            if not is_new:
                data["id"] = group_id
            new_id = _storage().upsert_rule(data, tenant_id=_tid(),
                                             created_by=session.get("username") or "ui")
            flash(f"Gruppo {'creato' if is_new else 'aggiornato'}.", "success")
            return redirect(url_for("rules.group_form_view", group_id=new_id))
        except ValueError as exc:
            flash(str(exc), "error")
            record = {**record, **data}

    children = _storage().list_group_children(group_id) if not is_new else []
    return render_template(
        "admin/rule_group_form.html",
        is_new=is_new,
        record=record,
        children=children,
    )


@rules_bp.route("/rules/groups/<int:group_id>/children/new", methods=["GET", "POST"])
@rules_bp.route("/rules/groups/<int:group_id>/children/<int:child_id>", methods=["GET", "POST"])
@login_required(role="operator")
def child_form_view(group_id: int, child_id: int | None = None):
    parent = _storage().get_rule(group_id)
    if not parent or not parent.get("is_group"):
        flash("Gruppo padre non trovato", "error")
        return redirect(url_for("rules.list_view"))

    is_new = child_id is None
    record: dict = {}
    if not is_new:
        record = _storage().get_rule(child_id) or {}
        if not record or record.get("parent_id") != group_id:
            flash("Figlio non trovato in questo gruppo", "error")
            return redirect(url_for("rules.group_form_view", group_id=group_id))

    if request.method == "POST":
        data = _parse_form(request.form)
        data["parent_id"] = group_id
        data["is_group"] = 0
        data["continue_in_group"] = (request.form.get("continue_in_group") or "").lower() in ("on", "true", "1")
        data["exit_group_continue"] = (request.form.get("exit_group_continue") or "").lower() in ("on", "true", "1")
        try:
            if not is_new:
                data["id"] = child_id
            new_id = _storage().upsert_rule(data, tenant_id=_tid(),
                                             created_by=session.get("username") or "ui")
            flash(f"Figlio {'creato' if is_new else 'aggiornato'}.", "success")
            return redirect(url_for("rules.child_form_view", group_id=group_id, child_id=new_id))
        except ValueError as exc:
            flash(str(exc), "error")
            record = {**record, **data}

    templates = _storage().list_templates(tenant_id=_tid(), only_enabled=True)
    # Anteprima action_map effettiva
    from ..rules.inheritance import deep_merge_action_map
    effective_action_map = deep_merge_action_map(
        parent.get("action_map") or {},
        record.get("action_map") or {},
    )
    ai_active_bindings, ai_providers_map, ai_global_status, ai_recent_decisions = \
        _build_ai_form_context(child_id)

    recipient_groups = _storage().list_recipient_groups(tenant_id=_tid(),
                                                          only_enabled=True)
    return render_template(
        "admin/rule_child_form.html",
        is_new=is_new,
        record=record,
        parent=parent,
        templates=templates,
        effective_action_map=effective_action_map,
        recipient_groups=recipient_groups,
        ai_active_bindings=ai_active_bindings,
        ai_providers=ai_providers_map,
        ai_global_status=ai_global_status,
        ai_recent_decisions=ai_recent_decisions,
    )


@rules_bp.route("/rules/<int:rule_id>/promote", methods=["POST"])
@login_required(role="admin")
def promote_view(rule_id: int):
    label = (request.form.get("group_label") or "").strip()
    if not label:
        flash("Etichetta gruppo richiesta.", "error")
        return redirect(url_for("rules.list_view"))
    try:
        new_group_id = _storage().promote_rule_to_group(
            rule_id, label, created_by=session.get("username") or "ui",
        )
        flash(f"Regola promossa a gruppo (id={new_group_id}).", "success")
        return redirect(url_for("rules.group_form_view", group_id=new_group_id))
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("rules.list_view"))


@rules_bp.route("/rules/flatten-preview")
@login_required()
def flatten_preview_view():
    flat = _storage().flatten_rules_for_listener(tenant_id=_tid())
    return render_template("admin/rule_flatten_preview.html", flat=flat)


@rules_bp.route("/rules/simulate", methods=["GET", "POST"])
@rules_bp.route("/rules/<int:rule_id>/simulate", methods=["GET", "POST"])
@login_required(role="operator")
def simulate_view(rule_id: int | None = None):
    from ..rules.evaluator import evaluate_v2

    storage = _storage()
    top = storage.list_top_level_items(tenant_id=_tid(), only_enabled=True)
    children_by_parent: dict[int, list[dict]] = {}
    for item in top:
        if item.get("is_group"):
            children_by_parent[item["id"]] = storage.list_group_children(
                item["id"], only_enabled=True,
            )

    outcome = None
    event = {
        "from_address": request.form.get("from_address") or "alice@cliente-noto.it",
        "to_address": request.form.get("to_address") or "info@domarc.it",
        "to_domain": (request.form.get("to_domain") or "domarc.it").lower(),
        "subject": request.form.get("subject") or "richiesta supporto",
        "body_text": request.form.get("body_text") or "",
    }
    in_service_raw = request.form.get("in_service") or "true"
    in_service = None if in_service_raw == "null" else (in_service_raw == "true")
    context = {
        "in_service": in_service,
        "sector": request.form.get("sector") or None,
    }

    if request.method == "POST":
        outcome = evaluate_v2(top, children_by_parent, event, context)

    return render_template(
        "admin/rule_simulate.html",
        outcome=outcome,
        event=event,
        context=context,
        rule_id=rule_id,
    )


@rules_bp.route("/rules/groupable-suggestions")
@login_required(role="admin")
def groupable_wizard_view():
    clusters = _storage().detect_groupable_rules(tenant_id=_tid(), min_cluster_size=2)
    return render_template("admin/rule_groupable_wizard.html", clusters=clusters)


@rules_bp.route("/rules/groupable-suggestions/promote", methods=["POST"])
@login_required(role="admin")
def groupable_promote_view():
    """Promuove un cluster suggerito a gruppo: prende la prima regola del cluster
    come base e ci aggancia tutte le altre come figli."""
    label = (request.form.get("group_label") or "Gruppo da cluster").strip()
    rule_ids = [int(x) for x in request.form.getlist("rule_ids") if x.isdigit()]
    if not rule_ids:
        flash("Nessuna regola selezionata.", "error")
        return redirect(url_for("rules.groupable_wizard_view"))
    storage = _storage()
    new_group_id = storage.promote_rule_to_group(
        rule_ids[0], label, created_by=session.get("username") or "ui",
    )
    # Aggancia le rimanenti come figli del nuovo gruppo
    for rid in rule_ids[1:]:
        storage.upsert_rule({
            "id": rid,
            **{k: v for k, v in (storage.get_rule(rid) or {}).items()
               if k not in ("id", "created_at", "created_by")},
            "parent_id": new_group_id,
        }, tenant_id=_tid(), created_by=session.get("username") or "ui")
    flash(f"Cluster di {len(rule_ids)} regole promosso al gruppo {new_group_id}.", "success")
    return redirect(url_for("rules.group_form_view", group_id=new_group_id))


def _parse_form(form) -> dict:
    """Form HTML → dict per upsert_rule. action_map costruito dai campi action_map_*."""
    action_map: dict = {}
    # Campi stringa/numerici
    for k in ("urgenza", "settore", "addetto_gestione", "referente", "note_extra",
              "template_id", "forward_target", "forward_port", "forward_tls",
              "redirect_to", "reason", "also_deliver_to", "auth_code_ttl_hours",
              # Reply-mode (auto_reply)
              "reply_mode", "reply_subject_prefix", "reply_to"):
        v = (form.get(f"action_map_{k}") or "").strip()
        if v:
            if k in ("forward_port", "auth_code_ttl_hours", "template_id"):
                try: action_map[k] = int(v)
                except ValueError: pass
            else:
                action_map[k] = v
    # Flag booleani
    for flag in ("generate_auth_code", "keep_original_delivery", "apply_rules",
                 "reply_quote_original", "reply_attach_original"):
        if (form.get(f"action_map_{flag}") or "").lower() in ("on", "true", "1"):
            action_map[flag] = True

    def _tristate(v):
        if not v: return None
        return v == "true"

    return {
        "name": form.get("name"),
        "description": form.get("description") or None,
        # M029: rule_set_id (FK -> rule_sets.id, default 'globali' se assente)
        "rule_set_id": _to_int(form.get("rule_set_id")),
        "scope_type": form.get("scope_type") or "global",
        "scope_ref": form.get("scope_ref") or None,
        "priority": form.get("priority") or 100,
        "enabled": (form.get("enabled") or "").lower() in ("on", "true", "1"),
        "match_from_regex": form.get("match_from_regex"),
        "match_from_domain": form.get("match_from_domain"),
        "match_to_regex": form.get("match_to_regex"),
        "match_subject_regex": form.get("match_subject_regex"),
        "match_body_regex": form.get("match_body_regex"),
        "match_to_domain": form.get("match_to_domain"),
        "match_at_hours": form.get("match_at_hours"),
        "match_in_service": _tristate(form.get("match_in_service")),
        "match_contract_active": _tristate(form.get("match_contract_active")),
        "match_known_customer": _tristate(form.get("match_known_customer")),
        "match_has_exception_today": _tristate(form.get("match_has_exception_today")),
        "match_customer_groups": ",".join(
            sorted(set(g.strip() for g in form.getlist("match_customer_groups") if g.strip()))
        ) or None,
        "match_tag": form.get("match_tag"),
        # Recipient groups (Migration 027)
        # NOTA: match_to_regex e match_to_group_id sono mutuamente esclusivi
        "match_to_group_id": _to_int(form.get("match_to_group_id")),
        "forward_to_emails": _normalize_emails_list(form.get("forward_to_emails")),
        "forward_to_group_id": _to_int(form.get("forward_to_group_id")),
        "action": form.get("action"),
        "action_map": action_map if action_map else None,
        "severity": form.get("severity"),
        "continue_after_match": (form.get("continue_after_match") or "").lower() in ("on", "true", "1"),
    }


def _to_int(v):
    try:
        return int(v) if v not in (None, "", "0") else None
    except (ValueError, TypeError):
        return None


def _normalize_emails_list(raw):
    """Lista email separate da ; , whitespace o newline → string ';'-separated."""
    import re as _re
    if not raw:
        return None
    parts = [p.strip().lower() for p in _re.split(r"[\s,;]+", raw or "") if p.strip()]
    valid = [p for p in parts if "@" in p]
    return ";".join(valid) if valid else None
