"""Test F3.5 Rule Proposer: clustering decisioni → proposta + accept/reject."""
from __future__ import annotations

import pytest

from domarc_relay_admin.ai_assistant.rule_proposer import (
    accept_proposal,
    generate_proposals,
    reject_proposal,
)


def _seed_decisions(storage, tenant_id: int, *, n: int = 25,
                     intent: str = "problema_tecnico",
                     urgenza: str = "ALTA",
                     suggested_action: str = "create_ticket",
                     subject_prefix: str = "[ALERT] Backup failed",
                     from_email: str = "monitoring@example.com",
                     confidence: float = 0.92,
                     uuid_prefix: str | None = None) -> list[int]:
    """Helper: crea N decisioni IA + eventi correlati per testing."""
    import uuid as _uuid
    decision_ids = []
    prefix = uuid_prefix or _uuid.uuid4().hex[:8]
    for i in range(n):
        event_uuid = f"test-evt-{prefix}-{i:03d}"
        # Inserisce evento
        with storage._connect() as conn:
            conn.execute(
                """INSERT INTO events (tenant_id, relay_event_uuid, received_at, ingested_at,
                                        from_address, to_address, subject, action_taken)
                   VALUES (?, ?, datetime('now'), datetime('now'), ?, ?, ?, ?)""",
                (tenant_id, event_uuid, from_email, "info@domarc.it",
                 f"{subject_prefix} on srv{i:02d}", "create_ticket"),
            )
            conn.commit()
        # Inserisce decisione
        did = storage.insert_ai_decision({
            "tenant_id": tenant_id,
            "event_uuid": event_uuid,
            "job_code": "classify_email",
            "intent": intent,
            "urgenza_proposta": urgenza,
            "summary": f"Errore backup su srv{i:02d}",
            "raw_output_json": {"intent": intent, "suggested_action": suggested_action,
                                  "urgenza": urgenza, "confidence": confidence},
            "confidence": confidence,
            "shadow_mode": True,
        })
        decision_ids.append(did)
    return decision_ids


# ============================================ GENERATE ===

def test_generate_proposal_with_enough_decisions(storage, tenant_id):
    """≥20 decisioni coerenti → 1 proposta."""
    _seed_decisions(storage, tenant_id, n=25)
    created = generate_proposals(storage=storage, tenant_id=tenant_id)
    assert len(created) == 1
    p = created[0]
    assert p["intent"] == "problema_tecnico"
    assert p["suggested_action"] == "create_ticket"
    assert p["decisions_count"] == 25
    assert p["confidence"] >= 0.9


def test_generate_skip_below_threshold(storage, tenant_id):
    """< 20 decisioni → no proposta."""
    _seed_decisions(storage, tenant_id, n=10)
    created = generate_proposals(storage=storage, tenant_id=tenant_id)
    assert created == []


def test_generate_skip_inconsistent_urgenza(storage, tenant_id):
    """Decisioni con urgenza variabile (sotto 80% dominante) → no proposta."""
    # 15 ALTA + 10 BASSA → 60% dominante < 80% threshold
    _seed_decisions(storage, tenant_id, n=15, urgenza="ALTA")
    _seed_decisions(storage, tenant_id, n=10, urgenza="BASSA",
                     subject_prefix="[ALERT] Backup failed")
    # Ma il subject e from sono uguali → stesso cluster di 25 elementi
    created = generate_proposals(storage=storage, tenant_id=tenant_id)
    # Cluster 25 elementi: 15/25 = 60% dominante < 80% → skip
    assert created == []


def test_generate_idempotent_dedup(storage, tenant_id):
    """Re-run del proposer non ricrea proposte già esistenti (dedup fingerprint)."""
    _seed_decisions(storage, tenant_id, n=25)
    first_run = generate_proposals(storage=storage, tenant_id=tenant_id)
    second_run = generate_proposals(storage=storage, tenant_id=tenant_id)
    assert len(first_run) == 1
    assert len(second_run) == 0  # già esistente, skip
    proposals = storage.list_ai_rule_proposals(tenant_id=tenant_id)
    assert len(proposals) == 1


def test_generate_proposal_creates_subject_regex(storage, tenant_id):
    """Il proposer estrae keyword dai subject e produce regex con lookahead AND."""
    _seed_decisions(storage, tenant_id, n=22,
                     subject_prefix="[ERROR] Database connection lost")
    created = generate_proposals(storage=storage, tenant_id=tenant_id)
    assert len(created) == 1
    proposal = storage.get_ai_rule_proposal(created[0]["proposal_id"])
    regex = proposal["suggested_match_subject"]
    assert regex is not None
    assert regex.startswith("(?i)")
    # Deve contenere lookahead per le keyword (database, connection, lost)
    # (le keyword "error" sono strippate dal _normalize_subject perché sono
    # già tra le ERROR_KEYWORDS dell'aggregator, ma se restano alcune di
    # queste sono boostate)
    assert "(?=" in regex  # lookahead present


# ============================================ ACCEPT ===

def test_accept_creates_rule(storage, tenant_id):
    _seed_decisions(storage, tenant_id, n=22)
    created = generate_proposals(storage=storage, tenant_id=tenant_id)
    proposal_id = created[0]["proposal_id"]
    rule_id = accept_proposal(
        storage=storage, proposal_id=proposal_id,
        reviewer="test_admin", review_notes="ok dopo review",
    )
    assert rule_id > 0
    rule = storage.get_rule(rule_id)
    assert rule is not None
    assert rule["created_by"] == f"ai_proposal_{proposal_id}"
    assert rule["action"] == "create_ticket"
    # Proposta marcata accepted
    p = storage.get_ai_rule_proposal(proposal_id)
    assert p["state"] == "accepted"
    assert p["accepted_rule_id"] == rule_id
    assert p["reviewer"] == "test_admin"


def test_accept_already_accepted_raises(storage, tenant_id):
    _seed_decisions(storage, tenant_id, n=22)
    created = generate_proposals(storage=storage, tenant_id=tenant_id)
    pid = created[0]["proposal_id"]
    accept_proposal(storage=storage, proposal_id=pid, reviewer="admin")
    with pytest.raises(ValueError, match="non pending"):
        accept_proposal(storage=storage, proposal_id=pid, reviewer="admin")


def test_accept_nonexistent_raises(storage, tenant_id):
    with pytest.raises(ValueError, match="non trovata"):
        accept_proposal(storage=storage, proposal_id=99999)


# ============================================ REJECT ===

def test_reject_marks_state(storage, tenant_id):
    _seed_decisions(storage, tenant_id, n=22)
    created = generate_proposals(storage=storage, tenant_id=tenant_id)
    pid = created[0]["proposal_id"]
    reject_proposal(storage=storage, proposal_id=pid, reviewer="admin",
                     review_notes="troppe variazioni")
    p = storage.get_ai_rule_proposal(pid)
    assert p["state"] == "rejected"
    assert p["reviewer"] == "admin"
    assert p["review_notes"] == "troppe variazioni"
    assert p.get("accepted_rule_id") is None


def test_reject_dedup_skips_subsequent_runs(storage, tenant_id):
    """Dopo reject, il proposer NON ricrea la stessa proposta."""
    _seed_decisions(storage, tenant_id, n=22)
    created = generate_proposals(storage=storage, tenant_id=tenant_id)
    reject_proposal(storage=storage, proposal_id=created[0]["proposal_id"],
                     reviewer="admin")
    second_run = generate_proposals(storage=storage, tenant_id=tenant_id)
    assert second_run == []  # dedup funziona anche su rejected
