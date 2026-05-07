"""AI Rule Wizard blueprint — genera regole guidate via prompt utente.

Flusso UI:
1. GET  /rules/ai-wizard           → form 2 modalità (description | samples)
2. POST /rules/ai-wizard           → chiama AI, mostra anteprima inline
3. POST /rules/ai-wizard/save      → upsert regola + redirect a form standard

L'output AI **non** scrive direttamente in DB: passa per ``upsert_rule`` che
applica gli stessi vincoli del form regole standard (V001-V008, regex check,
mutex, range priority).
"""
from __future__ import annotations

import logging

from flask import (
    Blueprint, current_app, flash, g, redirect, render_template, request,
    session, url_for,
)

from ..ai_assistant.rule_generator import (
    JOB_CODE,
    RuleGeneratorError,
    fetch_event_samples,
    generate_rule,
)
from ..auth import login_required

logger = logging.getLogger(__name__)

ai_rule_wizard_bp = Blueprint("ai_rule_wizard", __name__)

SESSION_KEY = "ai_rule_wizard_proposal"


def _storage():
    return current_app.extensions["domarc_storage"]


def _tid() -> int:
    return int(getattr(g, "current_tenant_id", 1))


def _binding_status() -> dict:
    """Verifica se c'è almeno un binding attivo per il job rule_generator."""
    try:
        bindings = _storage().list_ai_job_bindings(
            tenant_id=_tid(), only_enabled=True,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Impossibile leggere ai_job_bindings: %s", exc)
        return {"configured": False, "error": str(exc)}
    matching = [b for b in bindings if b.get("job_code") == JOB_CODE]
    if not matching:
        return {"configured": False, "error": None}
    return {
        "configured": True,
        "binding_count": len(matching),
        "providers": sorted({b.get("provider_id") for b in matching}),
    }


@ai_rule_wizard_bp.route("/rules/ai-wizard", methods=["GET", "POST"])
@login_required(role="operator")
def wizard_view():
    storage = _storage()
    binding_status = _binding_status()
    rule_sets = storage.list_rule_sets(tenant_id=_tid(), only_enabled=True)

    proposal = None
    error = None
    form_state = {
        "mode": request.form.get("mode", "description"),
        "description": request.form.get("description", "").strip(),
        "rule_set_code": request.form.get("rule_set_code", "globali").strip(),
        "from_like": request.form.get("from_like", "").strip(),
        "subject_like": request.form.get("subject_like", "").strip(),
        "sample_hours": int(request.form.get("sample_hours") or 168),
        "sample_limit": int(request.form.get("sample_limit") or 20),
        "user_hint": request.form.get("user_hint", "").strip(),
    }

    if request.method == "POST":
        if not binding_status.get("configured"):
            flash(
                "Nessun binding AI configurato per il job 'rule_generator'. "
                "Vai in /ai/models e creane uno (consigliato Claude Haiku 4.5).",
                "error",
            )
        else:
            try:
                if form_state["mode"] == "samples":
                    samples = fetch_event_samples(
                        storage,
                        tenant_id=_tid(),
                        hours=form_state["sample_hours"],
                        from_like=form_state["from_like"] or None,
                        subject_like=form_state["subject_like"] or None,
                        limit=form_state["sample_limit"],
                    )
                    if not samples:
                        flash(
                            f"Nessuna mail trovata negli ultimi {form_state['sample_hours']}h "
                            f"con i filtri specificati. Prova ad allargare il range o rimuovere i filtri.",
                            "warning",
                        )
                    else:
                        proposal = generate_rule(
                            storage=storage,
                            mode="samples",
                            samples=samples,
                            sample_hours=form_state["sample_hours"],
                            user_hint=form_state["user_hint"] or None,
                            rule_set_code=form_state["rule_set_code"] or None,
                            tenant_id=_tid(),
                        )
                        proposal["mode_used"] = "samples"
                        proposal["samples_count"] = len(samples)
                else:
                    if not form_state["description"]:
                        flash("Descrivi a parole la regola che vuoi creare.", "error")
                    else:
                        proposal = generate_rule(
                            storage=storage,
                            mode="description",
                            description=form_state["description"],
                            rule_set_code=form_state["rule_set_code"] or None,
                            tenant_id=_tid(),
                        )
                        proposal["mode_used"] = "description"

                if proposal:
                    # Salva in session per il successivo POST /save
                    session[SESSION_KEY] = {
                        "rule": proposal["rule"],
                        "rule_set_code": form_state["rule_set_code"] or "globali",
                    }
            except RuleGeneratorError as exc:
                error = str(exc)
                flash(f"Generazione fallita: {exc}", "error")
            except Exception as exc:  # noqa: BLE001
                logger.exception("AI Rule Wizard: errore inatteso")
                error = f"Errore inatteso: {exc}"
                flash(error, "error")

    return render_template(
        "admin/ai_rule_wizard.html",
        binding_status=binding_status,
        rule_sets=rule_sets,
        form_state=form_state,
        proposal=proposal,
        error=error,
    )


@ai_rule_wizard_bp.route("/rules/ai-wizard/save", methods=["POST"])
@login_required(role="operator")
def save_proposal():
    """Salva la proposta corrente (presa dalla session) come regola."""
    storage = _storage()
    proposal = session.get(SESSION_KEY)
    if not proposal:
        flash("Nessuna proposta in sessione. Genera prima una regola.", "error")
        return redirect(url_for("ai_rule_wizard.wizard_view"))

    rule_data = dict(proposal.get("rule") or {})
    rule_set_code = (proposal.get("rule_set_code") or "globali").strip()

    # Risoluzione rule_set_code → rule_set_id
    rs = storage.get_rule_set_by_code(rule_set_code, tenant_id=_tid())
    if not rs:
        rs = storage.get_rule_set_by_code("globali", tenant_id=_tid())
    if not rs:
        flash("Rule set 'globali' non trovato. Crea prima un rule_set.", "error")
        return redirect(url_for("ai_rule_wizard.wizard_view"))

    rule_data["rule_set_id"] = rs["id"]
    rule_data["scope_type"] = "global"
    rule_data["enabled"] = False  # Sempre disabilitata al primo save: l'admin la abilita dopo review

    # Override: se l'admin ha modificato il name nel form di conferma
    name_override = (request.form.get("name_override") or "").strip()
    if name_override:
        rule_data["name"] = name_override

    try:
        new_id = storage.upsert_rule(
            rule_data,
            tenant_id=_tid(),
            created_by=session.get("username") or "ai_wizard",
        )
    except ValueError as exc:
        flash(f"Validazione regola fallita: {exc}. Modifica nel form standard.", "error")
        return redirect(url_for("ai_rule_wizard.wizard_view"))

    # Pulisci la session
    session.pop(SESSION_KEY, None)

    flash(
        f"Regola creata da AI Wizard (id={new_id}), DISABILITATA per default. "
        f"Verifica nei dettagli e abilitala quando sei pronto.",
        "success",
    )
    return redirect(url_for("rules.form_view", rule_id=new_id))


@ai_rule_wizard_bp.route("/rules/ai-wizard/discard", methods=["POST"])
@login_required(role="operator")
def discard_proposal():
    session.pop(SESSION_KEY, None)
    flash("Proposta scartata.", "info")
    return redirect(url_for("ai_rule_wizard.wizard_view"))
