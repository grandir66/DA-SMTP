"""Test del merge action_map padre/figlio (rules.inheritance)."""
from __future__ import annotations

from domarc_relay_admin.rules.inheritance import (
    deep_merge_action_map,
    split_inherited_keys,
)


def test_merge_empty():
    assert deep_merge_action_map(None, None) == {}
    assert deep_merge_action_map({}, {}) == {}


def test_merge_parent_only_whitelist():
    parent = {
        "keep_original_delivery": True,
        "also_deliver_to": "ticket@domarc.it",
        "reply_mode": "to_sender_only",
        "settore": "assistenza",  # NON ereditabile (CHILD_ONLY)
    }
    child = {}
    result = deep_merge_action_map(parent, child)
    assert result["keep_original_delivery"] is True
    assert result["also_deliver_to"] == "ticket@domarc.it"
    assert result["reply_mode"] == "to_sender_only"
    assert "settore" not in result, "child-only key non deve passare dal padre"


def test_child_overrides_parent():
    parent = {"reply_mode": "to_sender_only", "auth_code_ttl_hours": 24}
    child = {"reply_mode": "reply_all", "template_id": 7}
    result = deep_merge_action_map(parent, child)
    assert result["reply_mode"] == "reply_all"
    assert result["auth_code_ttl_hours"] == 24
    assert result["template_id"] == 7


def test_child_none_does_not_override():
    parent = {"keep_original_delivery": True}
    child = {"keep_original_delivery": None, "template_id": 5}
    result = deep_merge_action_map(parent, child)
    assert result["keep_original_delivery"] is True
    assert result["template_id"] == 5


def test_also_deliver_to_replaces_not_concat():
    parent = {"also_deliver_to": "a@a.com,b@a.com"}
    child = {"also_deliver_to": "c@c.com"}
    result = deep_merge_action_map(parent, child)
    assert result["also_deliver_to"] == "c@c.com"


def test_split_inherited_keys():
    parent = {"keep_original_delivery": True, "reply_mode": "to_sender_only"}
    effective = {
        "keep_original_delivery": True,       # ereditata
        "reply_mode": "reply_all",            # override (own)
        "template_id": 7,                     # own (child-only)
    }
    inherited, own = split_inherited_keys(parent, effective)
    assert inherited == {"keep_original_delivery"}
    assert own == {"reply_mode", "template_id"}
