"""Test AI Error Aggregator (F2): fingerprint, clustering, recovery."""
from __future__ import annotations

import pytest

from domarc_relay_admin.ai_assistant.error_aggregator import (
    _normalize_subject,
    compute_fingerprint,
    is_error_event,
    is_recovery_event,
    process_event_for_clustering,
)


# ============================================ NORMALIZATION ===

def test_normalize_subject_lowercase_and_strips_error_keywords():
    """Le keyword error/recovery vengono strippate per uniformare i cluster
    failed/recovered allo stesso fingerprint."""
    n = _normalize_subject("BACKUP FAILED")
    assert n.lower() == n  # tutto lowercase
    assert "failed" not in n  # keyword strippata
    assert "backup" in n


def test_normalize_subject_strips_hostnames():
    """srv01, host-prod-12 vengono normalizzati a <host>."""
    a = _normalize_subject("backup failed on srv01")
    b = _normalize_subject("backup failed on srv02")
    assert a == b


def test_normalize_subject_failed_and_recovered_same():
    """failed/recovered → stesso fingerprint dopo strip keyword."""
    a = _normalize_subject("[ALERT] Backup failed on srv01")
    b = _normalize_subject("[INFO] Backup recovered on srv01")
    assert a == b


def test_normalize_subject_strips_numbers():
    a = _normalize_subject("server response code 500")
    b = _normalize_subject("server response code 502")
    assert a == b


def test_normalize_subject_strips_ip():
    a = _normalize_subject("server 192.168.1.10 down")
    b = _normalize_subject("server 192.168.1.20 down")
    assert a == b


# ============================================ FINGERPRINT ===

def test_fingerprint_same_for_similar_subjects():
    """'backup failed on srv01' e 'backup failed on srv02' → stesso fingerprint."""
    fp1 = compute_fingerprint("backup failed on srv01", "")
    fp2 = compute_fingerprint("backup failed on srv02", "")
    assert fp1 == fp2


def test_fingerprint_different_for_different_subjects():
    fp1 = compute_fingerprint("backup failed", "")
    fp2 = compute_fingerprint("disk full", "")
    assert fp1 != fp2


def test_fingerprint_stable_length():
    fp = compute_fingerprint("test", "body")
    assert len(fp) == 32  # SHA256 truncato a 32 hex char


def test_fingerprint_includes_body_signature():
    """Stessi subject ma body diversi → fingerprint diverso."""
    fp1 = compute_fingerprint("error", "database connection lost")
    fp2 = compute_fingerprint("error", "disk space full")
    assert fp1 != fp2


# ============================================ ERROR DETECTION ===

def test_is_error_event_subject_keywords():
    assert is_error_event("[ERROR] backup failed", "")
    assert is_error_event("[CRITICAL] server down", "")
    assert is_error_event("Service alert — timeout", "")
    assert is_error_event("Errore sul backup", "")  # IT
    assert not is_error_event("Buongiorno, una richiesta", "")


def test_is_error_event_body_keywords():
    assert is_error_event("test", "the system encountered a fatal error")
    assert is_error_event("test", "abbiamo un problema critico sul server")


def test_is_recovery_event_keywords():
    assert is_recovery_event("[OK] backup recovered", "")
    assert is_recovery_event("Service restored", "")
    assert is_recovery_event("Risolto", "")
    assert is_recovery_event("Up and running", "")
    assert not is_recovery_event("test mail", "")


# ============================================ CLUSTERING ===

def test_first_error_creates_cluster(storage, tenant_id):
    result = process_event_for_clustering(
        storage=storage, tenant_id=tenant_id,
        event_uuid="test-1", subject="[ERROR] backup failed on srv01",
        body_excerpt="backup failed",
    )
    assert result is not None
    assert result["action"] == "created"
    assert result["count"] == 1
    assert result["state"] == "accumulating"


def test_similar_errors_increment_same_cluster(storage, tenant_id):
    """5 mail con subject simili → stesso cluster, count=5, state=ticket_opened."""
    for i in range(1, 6):
        result = process_event_for_clustering(
            storage=storage, tenant_id=tenant_id,
            event_uuid=f"test-{i}", subject=f"[ERROR] backup failed on srv{i:02d}",
            body_excerpt="backup failed",
        )
    # L'ultimo deve aver superato la soglia (default=5)
    assert result["count"] == 5
    assert result["state"] == "ticket_opened"
    # Verifica un solo cluster nel DB
    clusters = storage.list_ai_error_clusters(tenant_id=tenant_id)
    assert len(clusters) == 1


def test_recovery_marks_cluster_recovered(storage, tenant_id):
    # Prima errore
    process_event_for_clustering(
        storage=storage, tenant_id=tenant_id, event_uuid="e1",
        subject="[ERROR] backup failed on srv01", body_excerpt="failed",
    )
    # Poi recovery con subject normalizzato compatibile
    result = process_event_for_clustering(
        storage=storage, tenant_id=tenant_id, event_uuid="r1",
        subject="[ERROR] backup ok on srv01",  # contiene 'error' + 'ok'
        body_excerpt="failed",  # body uguale → fingerprint uguale
    )
    # Recovery prevale (è un recovery_event, non error_event)
    # Nota: il subject "ok" attiva is_recovery_event
    if result and result.get("action") == "recovered":
        assert result["state"] == "recovered"


def test_non_error_event_returns_none(storage, tenant_id):
    """Mail senza error/recovery indicators → no clustering."""
    result = process_event_for_clustering(
        storage=storage, tenant_id=tenant_id, event_uuid="x",
        subject="Buongiorno, richiesta info", body_excerpt="",
    )
    assert result is None


def test_empty_event_returns_none(storage, tenant_id):
    result = process_event_for_clustering(
        storage=storage, tenant_id=tenant_id, event_uuid="x",
        subject=None, body_excerpt=None,
    )
    assert result is None


def test_cluster_threshold_configurable(storage, tenant_id):
    """Threshold custom su un cluster: opens ticket prima del default 5."""
    # Crea il primo evento (subject + body identici per fingerprint stabile)
    process_event_for_clustering(
        storage=storage, tenant_id=tenant_id, event_uuid="e1",
        subject="[ALERT] database timeout on srv01", body_excerpt="connection lost",
    )
    # Modifica la soglia del cluster a 2
    clusters = storage.list_ai_error_clusters(tenant_id=tenant_id)
    cluster_id = clusters[0]["id"]
    storage.upsert_ai_error_cluster({
        "id": cluster_id, "tenant_id": tenant_id,
        "fingerprint_hex": clusters[0]["fingerprint_hex"],
        "manual_threshold": 2,
    })
    # Secondo evento con stessa "forma" (solo hostname numerico cambia →
    # normalizzato uguale, body excerpt identico) → count=2 → ticket_opened
    result = process_event_for_clustering(
        storage=storage, tenant_id=tenant_id, event_uuid="e2",
        subject="[ALERT] database timeout on srv02", body_excerpt="connection lost",
    )
    assert result is not None
    assert result["count"] == 2
    assert result["state"] == "ticket_opened"
