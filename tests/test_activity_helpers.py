"""Test degli helper del blueprint Activity (stream incrementale eventi)."""
from __future__ import annotations

from domarc_relay_admin.routes.activity import _safe_int


def test_safe_int_default_for_none_or_empty():
    assert _safe_int(None, 0) == 0
    assert _safe_int("", 42) == 42


def test_safe_int_default_for_garbage():
    assert _safe_int("abc", 7) == 7
    assert _safe_int("12x", 9) == 9


def test_safe_int_clamps_into_min_max():
    # limite (range tipico 1..500)
    assert _safe_int("1000", 50, min_val=1, max_val=500) == 500
    assert _safe_int("0", 50, min_val=1, max_val=500) == 1
    assert _safe_int("250", 50, min_val=1, max_val=500) == 250


def test_safe_int_negative_clamped():
    # since_event_id può ricevere negativi via URL malformato
    assert _safe_int("-5", 0) == 0
    assert _safe_int("-1", 10, min_val=10) == 10


def test_safe_int_passthrough_normal_value():
    assert _safe_int("123", 0) == 123
    assert _safe_int(123, 0) == 123
