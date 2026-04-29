"""F3.5 Rule Proposer — learning loop AI → regole statiche.

Scansiona le ``ai_decisions`` recenti, raggruppa per pattern simili (intent +
subject normalizzato + from_domain) e quando un cluster supera le 2 soglie
genera una proposta di regola statica salvata in ``ai_rule_proposals``.

Le 2 soglie operano insieme:

- ``ai_proposal_min_decisions`` (default 20): volume minimo cluster.
- ``ai_proposal_consistency_threshold`` (default 0.80): ≥80% delle decisioni
  devono condividere stesso intent + suggested_action.

Output: una riga in ``ai_rule_proposals`` con stato ``pending`` per ciascun
cluster qualificato. L'admin la rivede in UI e può accettare (→ crea regola
in ``rules``) o rifiutare.

Idempotente: re-run dopo accept/reject NON ricrea proposte già processate
(usa fingerprint per dedup).

Nota: il proposer NON tiene conto delle proposte già accettate/rifiutate
con fingerprint identico, evitando loop di proposte.
"""
from __future__ import annotations

import hashlib
import logging
from collections import Counter, defaultdict
from typing import TYPE_CHECKING, Any

from .error_aggregator import _normalize_subject

if TYPE_CHECKING:
    from ..storage.base import Storage

logger = logging.getLogger(__name__)


def _decision_pattern_key(d: dict[str, Any]) -> tuple[str, str]:
    """Chiave di clustering: (intent, subject_normalizzato + from_domain).

    Decisioni con stesso intent + stesso subject normalizzato + stesso
    from_domain finiscono nello stesso cluster.
    """
    intent = d.get("intent") or "_unknown_"
    raw = d.get("raw_output_json") or {}
    if isinstance(raw, str):
        import json as _j
        try:
            raw = _j.loads(raw)
        except (TypeError, ValueError):
            raw = {}
    # subject: lo recuperiamo dall'event correlato (joint via event_uuid)
    # Per ora derivo solo da intent + suggested_action, e lascio il subject
    # esplicito al caller che ha l'evento sotto mano.
    suggested = (raw.get("suggested_action") if isinstance(raw, dict) else None) or "_unknown_"
    return (intent, suggested)


def _proposal_fingerprint(intent: str, suggested_action: str,
                           subject_pattern: str, from_domain: str) -> str:
    """Hash stabile della proposta per dedup."""
    s = f"{intent}|{suggested_action}|{subject_pattern}|{from_domain}"
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:32]


def generate_proposals(*, storage: "Storage", tenant_id: int = 1) -> list[dict]:
    """Esegue il proposer sulle decisioni recenti del tenant.

    Returns:
        Lista delle proposte CREATE (escluse quelle skippate per dedup).
    """
    settings = {s["key"]: s["value"] for s in storage.list_settings()}
    min_decisions = int(settings.get("ai_proposal_min_decisions", "20") or 20)
    consistency = float(settings.get("ai_proposal_consistency_threshold", "0.80") or 0.80)
    window_days = int(settings.get("ai_proposal_window_days", "14") or 14)

    # Carica decisioni dell'ultima finestra (con confidence valida e senza errori)
    decisions = storage.list_ai_decisions(
        tenant_id=tenant_id, hours=window_days * 24, limit=10000,
    )
    decisions = [d for d in decisions if not d.get("error") and d.get("intent")]
    if not decisions:
        return []

    # Carica eventi correlati per recuperare subject + from_domain
    # (le decisioni hanno event_uuid, gli eventi hanno relay_event_uuid)
    events_recent, _ = storage.list_events(
        tenant_id=tenant_id, hours=window_days * 24, page=1, page_size=20000,
    )
    event_by_uuid = {e["relay_event_uuid"]: e for e in events_recent if e.get("relay_event_uuid")}

    # Cluster: chiave = (intent, suggested_action, subject_pattern, from_domain)
    clusters: dict[tuple, list[dict]] = defaultdict(list)
    for d in decisions:
        evt = event_by_uuid.get(d.get("event_uuid") or "")
        if not evt:
            continue
        subject_pattern = _normalize_subject(evt.get("subject") or "")
        from_domain = (evt.get("from_address") or "").rpartition("@")[2].lower()
        raw = d.get("raw_output_json") or {}
        if isinstance(raw, dict):
            suggested_action = raw.get("suggested_action") or "_unknown_"
        else:
            suggested_action = "_unknown_"
        key = (d["intent"], suggested_action, subject_pattern, from_domain)
        clusters[key].append({
            "decision": d, "event": evt,
            "subject_raw": evt.get("subject") or "",
        })

    # Carica proposte già esistenti (fingerprint match) per dedup
    existing = storage.list_ai_rule_proposals(tenant_id=tenant_id)
    existing_fps = {p.get("fingerprint_hex") for p in existing if p.get("fingerprint_hex")}

    proposals_created: list[dict] = []
    for (intent, sugg_action, subj_pattern, from_dom), items in clusters.items():
        if len(items) < min_decisions:
            continue

        # Verifica consistency: devono avere stesso urgenza dominante
        urgenze = Counter(
            (it["decision"].get("urgenza_proposta") or "_none_") for it in items
        )
        dominant_urgenza, dominant_count = urgenze.most_common(1)[0]
        if dominant_count / len(items) < consistency:
            continue

        # Confidence aggregato: media delle confidence delle decisioni
        # con urgenza dominante
        dominant_items = [it for it in items
                           if (it["decision"].get("urgenza_proposta") or "_none_") == dominant_urgenza]
        confidences = [float(it["decision"].get("confidence") or 0)
                        for it in dominant_items if it["decision"].get("confidence") is not None]
        avg_confidence = sum(confidences) / len(confidences) if confidences else 0.0

        # Fingerprint dedup
        fp = _proposal_fingerprint(intent, sugg_action, subj_pattern, from_dom)
        if fp in existing_fps:
            continue  # già proposta (pending/accepted/rejected) — skip

        # Pattern match suggeriti per la regola
        suggested_match_subject = None
        if subj_pattern.strip():
            # Costruisci regex case-insensitive con le keyword del pattern
            # (parole significative ≥ 3 char, escluse <host>/<n>/<ip>/<time>)
            tokens = [t for t in subj_pattern.split()
                       if len(t) >= 3 and t not in ("<host>", "<n>", "<ip>", "<time>")]
            if tokens:
                # Pattern: (?i)\bword1\b.*\bword2\b — match in qualsiasi ordine via lookahead
                import re as _re
                escaped = [_re.escape(t) for t in tokens[:5]]  # max 5 keyword
                lookaheads = "".join(f"(?=.*\\b{e}\\b)" for e in escaped)
                suggested_match_subject = f"(?i){lookaheads}.*"

        suggested_action_map: dict = {}
        if sugg_action == "create_ticket":
            suggested_action_map = {
                "settore": "assistenza",
                "urgenza": dominant_urgenza if dominant_urgenza != "_none_" else "NORMALE",
            }
        elif sugg_action == "ignore":
            suggested_action_map = {"reason": "auto_proposal_from_ai"}
        elif sugg_action == "flag_only":
            suggested_action_map = {}

        sample_subjects_csv = " | ".join(
            it["subject_raw"][:60] for it in items[:5]
        )
        evidence_ids_csv = ",".join(str(it["decision"]["id"]) for it in items[:30])

        proposal_id = storage.upsert_ai_rule_proposal({
            "tenant_id": tenant_id,
            "fingerprint_hex": fp,
            "suggested_match_subject": suggested_match_subject,
            "suggested_match_from": (f"@{from_dom}$" if from_dom else None),
            "suggested_action": sugg_action if sugg_action != "_unknown_" else "create_ticket",
            "suggested_action_map_json": suggested_action_map,
            "confidence": avg_confidence,
            "evidence_decision_ids": evidence_ids_csv,
            "sample_subjects": sample_subjects_csv,
            "state": "pending",
        })
        logger.info("Rule proposal CREATED #%s: intent=%s action=%s fp=%s n_decisioni=%d conf=%.2f",
                    proposal_id, intent, sugg_action, fp[:8], len(items), avg_confidence)
        proposals_created.append({
            "proposal_id": proposal_id,
            "intent": intent,
            "suggested_action": sugg_action,
            "decisions_count": len(items),
            "dominant_urgenza": dominant_urgenza,
            "confidence": avg_confidence,
            "fingerprint": fp,
        })

    return proposals_created


def accept_proposal(*, storage: "Storage", proposal_id: int,
                     reviewer: str | None = None,
                     review_notes: str | None = None,
                     priority: int = 200) -> int:
    """Accetta una proposta: crea regola in `rules` e aggiorna stato.

    Returns:
        rule_id della regola creata.
    """
    proposal = storage.get_ai_rule_proposal(proposal_id)
    if not proposal:
        raise ValueError(f"Proposta {proposal_id} non trovata")
    if proposal.get("state") != "pending":
        raise ValueError(f"Proposta in stato {proposal.get('state')}, non pending")

    suggested_action_map = proposal.get("suggested_action_map_json") or {}
    if isinstance(suggested_action_map, str):
        import json as _j
        try:
            suggested_action_map = _j.loads(suggested_action_map)
        except (TypeError, ValueError):
            suggested_action_map = {}

    name = f"[AI-PROPOSED #{proposal_id}] " + (
        proposal.get("suggested_match_subject", "")[:50] or "auto-proposal"
    )
    rule_id = storage.upsert_rule({
        "name": name,
        "scope_type": "global",
        "priority": priority,
        "enabled": True,
        "match_subject_regex": proposal.get("suggested_match_subject"),
        "match_from_regex": proposal.get("suggested_match_from"),
        "action": proposal.get("suggested_action") or "flag_only",
        "action_map": suggested_action_map,
        "continue_after_match": False,
    }, tenant_id=int(proposal.get("tenant_id") or 1),
       created_by=f"ai_proposal_{proposal_id}")

    storage.upsert_ai_rule_proposal({
        "id": proposal_id,
        "tenant_id": proposal.get("tenant_id"),
        "fingerprint_hex": proposal.get("fingerprint_hex"),
        "state": "accepted",
        "accepted_rule_id": rule_id,
        "reviewer": reviewer,
        "review_notes": review_notes,
        "review_at": "datetime('now')",
    })
    logger.info("Rule proposal #%s ACCEPTED → rule #%s by %s",
                proposal_id, rule_id, reviewer or "unknown")
    return rule_id


def reject_proposal(*, storage: "Storage", proposal_id: int,
                     reviewer: str | None = None,
                     review_notes: str | None = None) -> None:
    proposal = storage.get_ai_rule_proposal(proposal_id)
    if not proposal:
        raise ValueError(f"Proposta {proposal_id} non trovata")
    storage.upsert_ai_rule_proposal({
        "id": proposal_id,
        "tenant_id": proposal.get("tenant_id"),
        "fingerprint_hex": proposal.get("fingerprint_hex"),
        "state": "rejected",
        "reviewer": reviewer,
        "review_notes": review_notes,
        "review_at": "datetime('now')",
    })
    logger.info("Rule proposal #%s REJECTED by %s", proposal_id, reviewer or "unknown")
