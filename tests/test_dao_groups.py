"""Test DAO sui nuovi metodi del Rule Engine v2."""
from __future__ import annotations

import pytest


def _mk(storage, tenant_id, **kw):
    """Helper: crea una regola e ritorna il dict completo."""
    data = {
        "name": kw.pop("name", "R"),
        "priority": kw.pop("priority", 100),
        "enabled": kw.pop("enabled", True),
        "match_to_domain": kw.pop("match_to_domain", "domarc.it"),
        "action": kw.pop("action", "ignore"),
    }
    data.update(kw)
    rid = storage.upsert_rule(data, tenant_id=tenant_id)
    return storage.get_rule(rid)


def test_create_orphan(storage, tenant_id):
    r = _mk(storage, tenant_id)
    assert r["is_group"] == 0
    assert r["parent_id"] is None
    assert r["exclusive_match"] == 1


def test_create_group_then_child(storage, tenant_id):
    g = _mk(storage, tenant_id, name="Gruppo X", priority=50,
            is_group=1, action="group", group_label="Test")
    c = _mk(storage, tenant_id, name="Figlio 1", priority=60,
            parent_id=g["id"], action="auto_reply",
            action_map={"template_id": 7})
    assert c["parent_id"] == g["id"]
    assert g["is_group"] == 1


def test_list_top_level_excludes_children(storage, tenant_id):
    g = _mk(storage, tenant_id, name="G", priority=50, is_group=1)
    _mk(storage, tenant_id, name="C", priority=60, parent_id=g["id"])
    _mk(storage, tenant_id, name="O", priority=200)
    top = storage.list_top_level_items(tenant_id=tenant_id)
    names = [r["name"] for r in top]
    assert "G" in names
    assert "O" in names
    assert "C" not in names


def test_list_group_children(storage, tenant_id):
    g = _mk(storage, tenant_id, name="G", priority=50, is_group=1)
    _mk(storage, tenant_id, name="C1", priority=70, parent_id=g["id"])
    _mk(storage, tenant_id, name="C2", priority=60, parent_id=g["id"])
    children = storage.list_group_children(g["id"])
    assert [c["name"] for c in children] == ["C2", "C1"]  # priority ASC


def test_list_rules_grouped_structure(storage, tenant_id):
    g = _mk(storage, tenant_id, name="G", priority=50, is_group=1)
    _mk(storage, tenant_id, name="C", priority=60, parent_id=g["id"])
    _mk(storage, tenant_id, name="O", priority=200)
    grouped = storage.list_rules_grouped(tenant_id=tenant_id)
    assert grouped[0]["type"] == "group"
    assert grouped[0]["group"]["name"] == "G"
    assert len(grouped[0]["children"]) == 1
    assert grouped[1]["type"] == "orphan"
    assert grouped[1]["rule"]["name"] == "O"


def test_flatten_for_listener_only_enabled(storage, tenant_id):
    g = _mk(storage, tenant_id, name="G", priority=50, is_group=1, enabled=True)
    _mk(storage, tenant_id, name="C", priority=60, parent_id=g["id"], enabled=True,
        action="auto_reply", action_map={"template_id": 1})
    _mk(storage, tenant_id, name="O_disabled", priority=200, enabled=False)
    flat = storage.flatten_rules_for_listener(tenant_id=tenant_id)
    assert len(flat) == 1
    assert flat[0]["action"] == "auto_reply"


def test_get_rule_with_inheritance_merges_action_map(storage, tenant_id):
    g = _mk(storage, tenant_id, name="G", priority=50, is_group=1,
            action_map={"keep_original_delivery": True, "reply_mode": "to_sender_only"})
    c = _mk(storage, tenant_id, name="C", priority=60, parent_id=g["id"],
            action="auto_reply", action_map={"template_id": 7, "reply_mode": "reply_all"})
    info = storage.get_rule_with_inheritance(c["id"])
    assert info["effective_action_map"]["template_id"] == 7
    assert info["effective_action_map"]["keep_original_delivery"] is True
    assert info["effective_action_map"]["reply_mode"] == "reply_all"  # figlio override
    assert "keep_original_delivery" in info["inherited_keys"]
    assert info["flow_path"] == f"group:{g['id']} → rule:{c['id']}"


def test_promote_rule_to_group_idempotent(storage, tenant_id):
    o = _mk(storage, tenant_id, name="Orfana", priority=100,
            action="auto_reply",
            action_map={"keep_original_delivery": True, "template_id": 5})
    new_group_id = storage.promote_rule_to_group(o["id"], "Gruppo demo")
    assert new_group_id != o["id"]
    promoted_child = storage.get_rule(o["id"])
    assert promoted_child["parent_id"] == new_group_id
    # action_map del figlio: solo template_id (keep_original_delivery passa al padre)
    assert "keep_original_delivery" not in promoted_child["action_map"]
    assert promoted_child["action_map"]["template_id"] == 5
    # idempotenza
    second = storage.promote_rule_to_group(o["id"], "Gruppo demo 2")
    assert second == new_group_id


def test_delete_group_cascades_children(storage, tenant_id):
    g = _mk(storage, tenant_id, name="G", priority=50, is_group=1)
    c = _mk(storage, tenant_id, name="C", priority=60, parent_id=g["id"])
    storage.delete_rule(g["id"])
    assert storage.get_rule(c["id"]) is None, "ON DELETE CASCADE deve eliminare i figli"


def test_detect_groupable_rules_finds_cluster(storage, tenant_id):
    # 3 regole orfane con stessi match_*
    for i in range(3):
        _mk(storage, tenant_id, name=f"R{i}", priority=100 + i * 10,
            match_to_domain="domarc.it", match_in_service=0, match_contract_active=1,
            action="ignore" if i == 0 else "auto_reply" if i == 1 else "flag_only")
    # 1 regola scollegata
    _mk(storage, tenant_id, name="O_alone", priority=500, match_to_domain="other.com")

    clusters = storage.detect_groupable_rules(tenant_id=tenant_id)
    assert len(clusters) == 1
    assert clusters[0]["size"] == 3
    assert "domarc.it" in str(clusters[0]["common_matches"])
    assert "Fuori orario" in clusters[0]["suggested_label"]
