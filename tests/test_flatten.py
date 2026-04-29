"""Test del flatten gerarchia → regole flat."""
from __future__ import annotations

import pytest

from domarc_relay_admin.rules.flatten import (
    combine_and,
    combine_and_domain,
    derive_continue_flag,
    flatten_rules,
    tristate_and,
)


def test_combine_and_regex_both_empty():
    assert combine_and(None, None) is None
    assert combine_and("", "") is None


def test_combine_and_regex_one_side():
    assert combine_and("foo", None) == "foo"
    assert combine_and(None, "bar") == "bar"


def test_combine_and_regex_both_lookahead():
    result = combine_and("foo", "bar")
    assert result == "(?=.*foo)(?=.*bar).*"


def test_combine_and_domain_compatible():
    assert combine_and_domain("DOMARC.it", "domarc.it") == "domarc.it"
    assert combine_and_domain(None, "domarc.it") == "domarc.it"
    assert combine_and_domain("domarc.it", None) == "domarc.it"
    assert combine_and_domain(None, None) is None


def test_combine_and_domain_incompatible_raises():
    with pytest.raises(ValueError):
        combine_and_domain("domarc.it", "other.com")


def test_tristate_and():
    assert tristate_and(None, None) is None
    assert tristate_and(None, 1) == 1
    assert tristate_and(0, None) == 0
    assert tristate_and(1, 1) == 1
    with pytest.raises(ValueError):
        tristate_and(1, 0)


def test_derive_continue_flag_continue_in_group():
    g = {"exclusive_match": 1}
    c = {"continue_in_group": 1}
    assert derive_continue_flag(g, c, is_last_child=False) is True


def test_derive_continue_flag_exit_group_continue():
    g = {"exclusive_match": 1}
    c = {"continue_in_group": 0, "exit_group_continue": 1}
    assert derive_continue_flag(g, c, is_last_child=False) is True


def test_derive_continue_flag_default_stop():
    g = {"exclusive_match": 1}
    c = {"continue_in_group": 0, "exit_group_continue": 0}
    assert derive_continue_flag(g, c, is_last_child=True) is False


def test_derive_continue_flag_non_exclusive_last_child_continues():
    g = {"exclusive_match": 0}
    c = {"continue_in_group": 0, "exit_group_continue": 0}
    assert derive_continue_flag(g, c, is_last_child=True) is True
    # Ma non per i non-ultimi figli
    assert derive_continue_flag(g, c, is_last_child=False) is False


def test_flatten_orphans_idempotent():
    """Le orfane vengono emesse identiche, niente moltiplicazione priority."""
    orphans = [
        {"id": 1, "name": "R1", "priority": 100, "enabled": 1, "is_group": 0,
         "match_to_domain": "domarc.it", "action": "ignore", "action_map": {},
         "continue_after_match": 0, "scope_type": "global"},
        {"id": 2, "name": "R2", "priority": 50, "enabled": 1, "is_group": 0,
         "match_to_domain": "domarc.it", "action": "auto_reply", "action_map": {"template_id": 1},
         "continue_after_match": 0, "scope_type": "global"},
    ]
    flat = flatten_rules(orphans, {}, only_enabled=True)
    assert len(flat) == 2
    # Ordinato per priority ASC
    assert flat[0]["id"] == 2 and flat[0]["priority"] == 50
    assert flat[1]["id"] == 1 and flat[1]["priority"] == 100


def test_flatten_group_emits_one_per_child():
    group = {
        "id": 100, "name": "Gruppo X", "priority": 50, "enabled": 1, "is_group": 1,
        "match_to_domain": "domarc.it", "match_in_service": 0,
        "action": "group", "action_map": {"keep_original_delivery": True, "also_deliver_to": "ticket@x.com"},
        "exclusive_match": 1, "scope_type": "global",
    }
    children = [
        {"id": 101, "name": "Auto-reply", "priority": 60, "enabled": 1, "is_group": 0,
         "parent_id": 100, "match_to_domain": None, "match_in_service": None,
         "action": "auto_reply", "action_map": {"template_id": 7},
         "continue_in_group": 1, "exit_group_continue": 0, "scope_type": "global"},
        {"id": 102, "name": "Ticket", "priority": 70, "enabled": 1, "is_group": 0,
         "parent_id": 100, "match_to_domain": None, "match_in_service": None,
         "action": "create_ticket", "action_map": {"settore": "assistenza", "urgenza": "NORMALE"},
         "continue_in_group": 0, "exit_group_continue": 0, "scope_type": "global"},
    ]
    flat = flatten_rules([group], {100: children}, only_enabled=True)
    assert len(flat) == 2
    # Match merged
    assert flat[0]["match_to_domain"] == "domarc.it"
    assert flat[0]["match_in_service"] == 0
    assert flat[1]["match_to_domain"] == "domarc.it"
    # Action_map ereditata
    assert flat[0]["action_map"]["template_id"] == 7
    assert flat[0]["action_map"]["keep_original_delivery"] is True
    assert flat[0]["action_map"]["also_deliver_to"] == "ticket@x.com"
    # continue derivato
    assert flat[0]["continue_after_match"] is True   # continue_in_group=1
    assert flat[1]["continue_after_match"] is False  # ultimo figlio, exclusive_match=1


def test_flatten_skips_disabled():
    group = {"id": 100, "priority": 50, "enabled": 0, "is_group": 1,
             "match_to_domain": "x.com", "exclusive_match": 1, "scope_type": "global"}
    flat = flatten_rules([group], {100: [{"id": 101, "enabled": 1, "priority": 60}]},
                         only_enabled=True)
    assert flat == []
