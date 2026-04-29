"""Test dei validatori V001-V008 + V_PRI_RANGE + warning soft."""
from __future__ import annotations

import pytest

from domarc_relay_admin.rules.validators import (
    PRIORITY_MAX,
    ValidationError,
    validate_group_consistency,
    validate_rule,
)


def _orphan(**kw):
    base = {
        "is_group": 0, "parent_id": None, "priority": 100,
        "match_to_domain": "domarc.it", "action": "ignore",
    }
    base.update(kw)
    return base


def _group(**kw):
    base = {
        "is_group": 1, "parent_id": None, "priority": 50,
        "match_to_domain": "domarc.it", "exclusive_match": 1,
    }
    base.update(kw)
    return base


def _child(parent, **kw):
    base = {
        "is_group": 0, "parent_id": parent["id"], "priority": parent["priority"] + 10,
        "action": "ignore",
    }
    base.update(kw)
    return base


def test_orphan_valid():
    errors, warnings = validate_rule(_orphan())
    assert errors == []


def test_v001_group_with_parent():
    rule = _group(parent_id=99)
    errors, _ = validate_rule(rule)
    codes = [e.code for e in errors]
    assert "V001" in codes


def test_v002_parent_not_group():
    parent = _orphan(id=1, is_group=0)
    child = _child(parent, id=2)
    errors, _ = validate_rule(child, parent=parent)
    assert "V002" in [e.code for e in errors]


def test_v003_group_with_action():
    rule = _group(action="auto_reply")
    errors, _ = validate_rule(rule)
    assert "V003" in [e.code for e in errors]


def test_v003_group_with_child_only_action_map_key():
    rule = _group(action_map={"template_id": 7})
    errors, _ = validate_rule(rule)
    assert "V003" in [e.code for e in errors]


def test_v004_group_without_match():
    rule = _group(match_to_domain=None, match_in_service=None)
    errors, _ = validate_rule(rule)
    assert "V004" in [e.code for e in errors]


def test_v005_self_reference():
    rule = _orphan(id=42, parent_id=42)
    errors, _ = validate_rule(rule)
    assert "V005" in [e.code for e in errors]


def test_v006_incompatible_to_domain():
    parent = _group(id=1, match_to_domain="domarc.it")
    child = _child(parent, id=2, match_to_domain="other.com")
    errors, _ = validate_rule(child, parent=parent)
    assert "V006" in [e.code for e in errors]


def test_v006_incompatible_tristate():
    parent = _group(id=1, match_in_service=1)
    child = _child(parent, id=2, match_in_service=0)
    errors, _ = validate_rule(child, parent=parent)
    assert "V006" in [e.code for e in errors]


def test_v007_priority_out_of_range():
    rule = _orphan(priority=PRIORITY_MAX + 1)
    errors, _ = validate_rule(rule)
    assert "V007" in [e.code for e in errors]


def test_v008_group_as_child():
    parent = _group(id=1)
    rule = _group(id=2, parent_id=1)
    errors, _ = validate_rule(rule, parent=parent)
    assert "V008" in [e.code for e in errors]


def test_pri_range_child_below_parent():
    parent = _group(id=1, priority=100)
    child = _child(parent, id=2, priority=50)  # < padre
    errors, _ = validate_rule(child, parent=parent)
    assert "V_PRI_RANGE" in [e.code for e in errors]


def test_pri_range_child_overlaps_next_top_level():
    parent = _group(id=1, priority=100)
    child = _child(parent, id=2, priority=210)
    errors, _ = validate_rule(child, parent=parent, next_top_level_priority=200)
    assert "V_PRI_RANGE" in [e.code for e in errors]


def test_pri_range_child_in_range_ok():
    parent = _group(id=1, priority=100)
    child = _child(parent, id=2, priority=110)
    errors, _ = validate_rule(child, parent=parent, next_top_level_priority=200)
    assert errors == []


def test_warning_redundant_match():
    parent = _group(id=1, match_to_domain="domarc.it")
    child = _child(parent, id=2, match_to_domain="domarc.it")
    errors, warnings = validate_rule(child, parent=parent)
    assert errors == []
    assert any("W004" in w for w in warnings)


def test_warning_pri_gap_too_small():
    parent = _group(id=1, priority=100)
    sibling = _child(parent, id=2, priority=110)
    new_child = _child(parent, id=3, priority=112)
    _, warnings = validate_rule(new_child, parent=parent, siblings=[sibling])
    assert any("W_PRI_GAP" in w for w in warnings)


def test_w001_group_without_children():
    group = _group(id=1)
    warnings = validate_group_consistency(group, [])
    assert any("W001" in w for w in warnings)


def test_w005_non_exclusive_with_stop_last_child():
    group = _group(id=1, exclusive_match=0)
    children = [
        _child(group, id=2, priority=110, continue_in_group=0, exit_group_continue=0),
        _child(group, id=3, priority=120, continue_in_group=0, exit_group_continue=0),
    ]
    warnings = validate_group_consistency(group, children)
    assert any("W005" in w for w in warnings)


def test_validation_error_has_code():
    e = ValidationError("V001", "test")
    assert e.code == "V001"
    assert "V001" in str(e)
