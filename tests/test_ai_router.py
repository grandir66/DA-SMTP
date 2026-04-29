"""Test AiRouter: lookup binding, traffic split A/B, cache invalidation."""
from __future__ import annotations

from collections import Counter

import pytest

from domarc_relay_admin.ai_assistant.router import AiRouter, reset_router


@pytest.fixture
def storage_with_bindings(storage, tenant_id):
    """Setup: 1 provider + 2 bindings A/B sul job classify_email (80/20)."""
    pid = storage.upsert_ai_provider({
        "name": "Claude API", "kind": "claude",
        "api_key_env": "ANTHROPIC_API_KEY",
        "default_model": "claude-haiku-4-5", "enabled": True,
    }, tenant_id=tenant_id)
    storage.upsert_ai_job_binding({
        "job_code": "classify_email", "provider_id": pid,
        "model_id": "claude-haiku-4-5", "temperature": 0.0,
        "max_tokens": 500, "timeout_ms": 5000, "enabled": True,
        "traffic_split": 80,
    }, tenant_id=tenant_id, actor="test")
    storage.upsert_ai_job_binding({
        "job_code": "classify_email", "provider_id": pid,
        "model_id": "claude-sonnet-4-6", "temperature": 0.0,
        "max_tokens": 500, "timeout_ms": 5000, "enabled": True,
        "traffic_split": 20,
    }, tenant_id=tenant_id, actor="test")
    return storage


def test_pick_binding_returns_none_if_no_binding(storage, tenant_id):
    reset_router()
    router = AiRouter(storage, tenant_id=tenant_id)
    assert router.pick_binding("nonexistent_job") is None


def test_pick_binding_single_binding(storage_with_bindings, tenant_id):
    """Con 2 bindings 80/20, pick_binding sceglie sempre uno dei due."""
    reset_router()
    router = AiRouter(storage_with_bindings, tenant_id=tenant_id)
    binding = router.pick_binding("classify_email")
    assert binding is not None
    assert binding.job_code == "classify_email"
    assert binding.model_id in ("claude-haiku-4-5", "claude-sonnet-4-6")
    assert binding.provider_name == "Claude API"


def test_traffic_split_distribution(storage_with_bindings, tenant_id):
    """Su molti pick, la distribuzione deve seguire approssimativamente 80/20."""
    reset_router()
    router = AiRouter(storage_with_bindings, tenant_id=tenant_id)
    counts: Counter = Counter()
    for _ in range(2000):
        binding = router.pick_binding("classify_email")
        counts[binding.model_id] += 1
    # Tolleranza ±5% su 2000 sample
    haiku_pct = counts["claude-haiku-4-5"] / 2000
    sonnet_pct = counts["claude-sonnet-4-6"] / 2000
    assert 0.75 < haiku_pct < 0.85, f"Haiku: {haiku_pct:.2%} (atteso ~80%)"
    assert 0.15 < sonnet_pct < 0.25, f"Sonnet: {sonnet_pct:.2%} (atteso ~20%)"


def test_cache_invalidation(storage_with_bindings, tenant_id):
    """Dopo invalidate_cache(), il router rilegge i binding dal DB."""
    reset_router()
    router = AiRouter(storage_with_bindings, tenant_id=tenant_id)
    b1 = router.pick_binding("classify_email")
    assert b1 is not None

    # Disabilita TUTTI i binding direttamente in DB
    with storage_with_bindings._connect() as conn:
        conn.execute("UPDATE ai_job_bindings SET enabled = 0")

    # Senza invalidate, la cache vede ancora i bindings vecchi
    b_cached = router.pick_binding("classify_email")
    assert b_cached is not None  # cache hit

    # Dopo invalidate, vede DB aggiornato
    router.invalidate_cache()
    b_after = router.pick_binding("classify_email")
    assert b_after is None  # niente più binding attivi


def test_render_prompts_with_inline_template(storage_with_bindings, tenant_id):
    """Se binding ha template inline, viene renderizzato."""
    reset_router()
    router = AiRouter(storage_with_bindings, tenant_id=tenant_id)

    # Aggiorna il primo binding con template Jinja2 inline
    bindings = storage_with_bindings.list_ai_job_bindings(
        tenant_id=tenant_id, job_code="classify_email", only_enabled=True,
    )
    storage_with_bindings.upsert_ai_job_binding({
        "id": bindings[0]["id"],
        "job_code": "classify_email",
        "provider_id": bindings[0]["provider_id"],
        "model_id": bindings[0]["model_id"],
        "system_prompt_template": "Sei classificatore di email per {{ tenant }}.",
        "user_prompt_template": "Subject: {{ subject }}\nBody: {{ body }}",
        "enabled": True, "traffic_split": 100,
    }, tenant_id=tenant_id, actor="test")

    # Disabilita il secondo per pickare sicuro il primo
    storage_with_bindings.upsert_ai_job_binding({
        "id": bindings[1]["id"], **{k: v for k, v in bindings[1].items() if k != "id"},
        "enabled": False,
    }, tenant_id=tenant_id, actor="test")

    router.invalidate_cache()
    binding = router.pick_binding("classify_email")
    assert binding is not None

    sys, usr = router.render_prompts(binding, {
        "tenant": "DOMARC", "subject": "test", "body": "hello",
    })
    assert "DOMARC" in sys
    assert "Subject: test" in usr
    assert "hello" in usr
