"""Test DAO migration 012/013: ai_decisions, ai_bindings, api_keys (Fernet)."""
from __future__ import annotations

import pytest


# ============================================ AI DECISIONS ===

def test_insert_decision_minimal(storage, tenant_id):
    decision_id = storage.insert_ai_decision({
        "tenant_id": tenant_id,
        "event_uuid": "test-uuid-001",
        "job_code": "classify_email",
        "provider": "Claude API",
        "model": "claude-haiku-4-5",
        "intent": "problema_tecnico",
        "urgenza_proposta": "ALTA",
        "summary": "Test summary",
        "latency_ms": 420,
        "input_tokens": 100,
        "output_tokens": 50,
        "cost_usd": 0.0007,
        "shadow_mode": True,
    })
    assert decision_id > 0
    fetched = storage.get_ai_decision(decision_id)
    assert fetched["intent"] == "problema_tecnico"
    assert fetched["shadow_mode"] == 1
    assert fetched["cost_usd"] == 0.0007


def test_list_decisions_filters_by_job(storage, tenant_id):
    storage.insert_ai_decision({
        "tenant_id": tenant_id, "job_code": "classify_email",
        "intent": "x", "shadow_mode": True,
    })
    storage.insert_ai_decision({
        "tenant_id": tenant_id, "job_code": "summarize_email",
        "intent": "y", "shadow_mode": True,
    })
    classify = storage.list_ai_decisions(tenant_id=tenant_id, job_code="classify_email")
    assert len(classify) == 1
    assert classify[0]["job_code"] == "classify_email"


def test_sum_cost_today(storage, tenant_id):
    storage.insert_ai_decision({
        "tenant_id": tenant_id, "job_code": "classify_email",
        "cost_usd": 0.0010, "shadow_mode": True,
    })
    storage.insert_ai_decision({
        "tenant_id": tenant_id, "job_code": "classify_email",
        "cost_usd": 0.0020, "shadow_mode": True,
    })
    total = storage.sum_ai_decisions_cost_today(tenant_id=tenant_id)
    assert total == pytest.approx(0.0030, rel=1e-4)


def test_decision_json_fields_decoded(storage, tenant_id):
    """suggested_actions_json e raw_output_json devono essere ridecoded a dict."""
    decision_id = storage.insert_ai_decision({
        "tenant_id": tenant_id,
        "job_code": "classify_email",
        "suggested_actions_json": {"forward_to": "ops@x.com", "open_ticket": True},
        "raw_output_json": {"intent": "test"},
        "shadow_mode": True,
    })
    fetched = storage.get_ai_decision(decision_id)
    assert isinstance(fetched["suggested_actions_json"], dict)
    assert fetched["suggested_actions_json"]["open_ticket"] is True
    assert isinstance(fetched["raw_output_json"], dict)


# ============================================ AI BINDINGS ===

def test_upsert_binding_creates_v1(storage, tenant_id):
    pid = storage.upsert_ai_provider({
        "name": "P1", "kind": "claude", "default_model": "claude-haiku-4-5",
        "enabled": True,
    }, tenant_id=tenant_id)
    bid = storage.upsert_ai_job_binding({
        "job_code": "classify_email", "provider_id": pid,
        "model_id": "claude-haiku-4-5", "enabled": True,
    }, tenant_id=tenant_id, actor="test")
    assert bid > 0
    bindings = storage.list_ai_job_bindings(tenant_id=tenant_id, job_code="classify_email")
    assert len(bindings) == 1
    assert bindings[0]["version"] == 1


def test_upsert_binding_new_version_disables_previous(storage, tenant_id):
    pid = storage.upsert_ai_provider({
        "name": "P1", "kind": "claude", "enabled": True,
    }, tenant_id=tenant_id)
    storage.upsert_ai_job_binding({
        "job_code": "classify_email", "provider_id": pid,
        "model_id": "claude-haiku-4-5", "enabled": True,
    }, tenant_id=tenant_id, actor="test")
    # Nuova versione → disabilita la v1
    storage.upsert_ai_job_binding({
        "job_code": "classify_email", "provider_id": pid,
        "model_id": "claude-sonnet-4-6", "enabled": True,
    }, tenant_id=tenant_id, actor="test", new_version=True)
    all_bindings = storage.list_ai_job_bindings(
        tenant_id=tenant_id, job_code="classify_email",
    )
    assert len(all_bindings) == 2
    enabled = [b for b in all_bindings if b["enabled"]]
    assert len(enabled) == 1
    assert enabled[0]["version"] == 2
    assert enabled[0]["model_id"] == "claude-sonnet-4-6"


def test_list_only_enabled_filter(storage, tenant_id):
    pid = storage.upsert_ai_provider({
        "name": "P1", "kind": "claude", "enabled": True,
    }, tenant_id=tenant_id)
    storage.upsert_ai_job_binding({
        "job_code": "classify_email", "provider_id": pid,
        "model_id": "x", "enabled": True,
    }, tenant_id=tenant_id, actor="test")
    storage.upsert_ai_job_binding({
        "job_code": "summarize_email", "provider_id": pid,
        "model_id": "y", "enabled": False,
    }, tenant_id=tenant_id, actor="test")
    only_enabled = storage.list_ai_job_bindings(tenant_id=tenant_id, only_enabled=True)
    assert len(only_enabled) == 1
    assert only_enabled[0]["job_code"] == "classify_email"


# ============================================ API KEYS (Fernet) ===

def test_api_key_encryption_roundtrip(storage, tenant_id, tmp_path, monkeypatch):
    """Cifra valore → salva → rilegge → decifra → uguale."""
    monkeypatch.setenv("DOMARC_RELAY_MASTER_KEY_PATH", str(tmp_path / "test.key"))
    # Forza ricreazione del singleton SecretsManager con il nuovo path
    from domarc_relay_admin import secrets_manager
    secrets_manager._singleton = None
    sm = secrets_manager.get_secrets_manager()

    plaintext = "sk-ant-api03-supersecret-12345-abcdef"
    encrypted = sm.encrypt(plaintext)
    masked = sm.mask(plaintext)

    key_id = storage.upsert_api_key(
        tenant_id=tenant_id, name="Test Claude", env_var_name="ANTHROPIC_API_KEY",
        value_encrypted=encrypted, masked_preview=masked,
        description="test", enabled=True, actor="test",
    )

    fetched = storage.get_api_key(key_id)
    assert fetched["env_var_name"] == "ANTHROPIC_API_KEY"
    assert fetched["enabled"] == 1
    assert fetched["masked_preview"] == masked

    # Roundtrip decifratura
    decrypted = sm.decrypt(fetched["value_encrypted"])
    assert decrypted == plaintext


def test_api_key_toggle(storage, tenant_id, tmp_path, monkeypatch):
    monkeypatch.setenv("DOMARC_RELAY_MASTER_KEY_PATH", str(tmp_path / "test.key"))
    from domarc_relay_admin import secrets_manager
    secrets_manager._singleton = None
    sm = secrets_manager.get_secrets_manager()

    key_id = storage.upsert_api_key(
        tenant_id=tenant_id, name="K1", env_var_name="X",
        value_encrypted=sm.encrypt("xxx"), masked_preview="x...",
    )
    assert storage.get_api_key(key_id)["enabled"] == 1
    new_state = storage.toggle_api_key(key_id)
    assert new_state is False
    assert storage.get_api_key(key_id)["enabled"] == 0


def test_secrets_manager_mask_short_value(tmp_path, monkeypatch):
    monkeypatch.setenv("DOMARC_RELAY_MASTER_KEY_PATH", str(tmp_path / "test.key"))
    from domarc_relay_admin import secrets_manager
    secrets_manager._singleton = None
    sm = secrets_manager.get_secrets_manager()
    assert sm.mask("abc") == "***"
    assert sm.mask("") == "***"
    assert sm.mask("sk-ant-supersecret-12345") == "sk-ant-s...2345"


def test_secrets_manager_decrypt_invalid_token_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("DOMARC_RELAY_MASTER_KEY_PATH", str(tmp_path / "test.key"))
    from domarc_relay_admin import secrets_manager
    secrets_manager._singleton = None
    sm = secrets_manager.get_secrets_manager()
    with pytest.raises(ValueError, match="Decifratura fallita"):
        sm.decrypt(b"not-a-valid-fernet-token")


# ============================================ MODULE INSTALL LOG ===

def test_module_install_log_lifecycle(storage):
    # Insert running
    log_id = storage.insert_module_install_log(
        module_code="anthropic", operation="install",
        status="running", actor="test",
    )
    assert log_id > 0
    rows = storage.list_module_install_log(limit=10)
    assert any(r["id"] == log_id and r["status"] == "running" for r in rows)

    # Update success
    storage.update_module_install_log(
        log_id, status="success", output="installed ok",
        return_code=0, duration_ms=1234,
    )
    rows = storage.list_module_install_log(limit=10)
    updated = next(r for r in rows if r["id"] == log_id)
    assert updated["status"] == "success"
    assert updated["return_code"] == 0
    assert updated["duration_ms"] == 1234


def test_module_install_log_filters_by_code(storage):
    storage.insert_module_install_log(
        module_code="anthropic", operation="install", status="success",
        return_code=0, duration_ms=100, actor="t",
    )
    storage.insert_module_install_log(
        module_code="spacy", operation="install", status="failed",
        return_code=1, duration_ms=200, actor="t",
    )
    spacy_only = storage.list_module_install_log(module_code="spacy")
    assert len(spacy_only) == 1
    assert spacy_only[0]["status"] == "failed"
