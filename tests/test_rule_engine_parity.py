"""Test di parità tra Rule Engine v2 e listener legacy.

Per ogni evento sintetico, deve valere::

    [r["id"] for r in evaluate_v2(top, children, event, ctx).winners]
        ==
    [r["id"] for r in evaluate_legacy(flatten(top, children), event, ctx)]

Coverage richiesta: tabella di verità completa di
``continue_in_group × exit_group_continue × exclusive_match`` (sezione 2.3 spec).
"""
from __future__ import annotations

from typing import Any

import pytest

from domarc_relay_admin.rules.evaluator import evaluate_v2
from domarc_relay_admin.rules.flatten import flatten_rules
from domarc_relay_admin.rules.legacy_evaluator import evaluate_legacy


def _orphan(rid, prio, *, action="ignore", continue_after=False, **kw):
    base: dict[str, Any] = {
        "id": rid, "name": f"orphan_{rid}", "priority": prio, "enabled": 1,
        "is_group": 0, "parent_id": None, "scope_type": "global",
        "action": action, "action_map": {}, "continue_after_match": int(continue_after),
        "match_in_service": None,
    }
    base.update(kw)
    return base


def _group(rid, prio, **kw):
    base: dict[str, Any] = {
        "id": rid, "name": f"group_{rid}", "priority": prio, "enabled": 1,
        "is_group": 1, "parent_id": None, "scope_type": "global",
        "action": "group", "action_map": {}, "exclusive_match": 1,
        "continue_in_group": 0, "exit_group_continue": 0,
        "match_in_service": None,
    }
    base.update(kw)
    return base


def _child(rid, parent_id, prio, *, action="ignore", **kw):
    base: dict[str, Any] = {
        "id": rid, "name": f"child_{rid}", "priority": prio, "enabled": 1,
        "is_group": 0, "parent_id": parent_id, "scope_type": "global",
        "action": action, "action_map": {},
        "continue_in_group": 0, "exit_group_continue": 0,
        "match_in_service": None,
    }
    base.update(kw)
    return base


def _evt(**kw):
    base = {
        "from_address": "alice@cliente.com",
        "to_address": "info@domarc.it",
        "to_domain": "domarc.it",
        "subject": "richiesta supporto",
        "body_text": "ciao, ho un problema sul firewall",
    }
    base.update(kw)
    return base


def _ctx(**kw):
    base = {"sector": None, "in_service": True}
    base.update(kw)
    return base


def _ids_of(items):
    return [r["id"] if isinstance(r["id"], int) else r.get("_source_child_id") for r in items]


def _parity_check(top, children_by_parent, event, ctx):
    """Asserisce che v2 e legacy(flatten(...)) producano gli stessi id."""
    flat = flatten_rules(top, children_by_parent, only_enabled=True)
    legacy_winners = evaluate_legacy(flat, event, ctx)
    v2_outcome = evaluate_v2(top, children_by_parent, event, ctx)
    legacy_ids = [r.get("_source_child_id") or r["id"] for r in legacy_winners]
    v2_ids = [r["id"] for r in v2_outcome.winners]
    assert legacy_ids == v2_ids, (
        f"Divergenza:\n  legacy={legacy_ids}\n  v2={v2_ids}\n"
        f"  flat priorities={[r['priority'] for r in flat]}"
    )


# =========================================================================
# CASE 1 — Sole orfane: parità banale (flatten è no-op).
# =========================================================================

def test_only_orphans_single_match():
    o1 = _orphan(1, 100, match_to_domain="domarc.it", action="auto_reply")
    o2 = _orphan(2, 200, match_to_domain="other.com", action="ignore")
    _parity_check([o1, o2], {}, _evt(), _ctx())


def test_only_orphans_no_match_passes():
    o1 = _orphan(1, 100, match_to_domain="other.com")
    _parity_check([o1], {}, _evt(), _ctx())


def test_orphans_with_continue_after_match():
    o1 = _orphan(1, 100, match_to_domain="domarc.it", continue_after=True)
    o2 = _orphan(2, 200, match_to_domain="domarc.it", continue_after=False)
    _parity_check([o1, o2], {}, _evt(), _ctx())


# =========================================================================
# CASE 2 — Gruppo con un solo figlio
# =========================================================================

def test_group_single_child_match():
    g = _group(10, 50, match_to_domain="domarc.it")
    c = _child(11, 10, 60, action="auto_reply",
               action_map={"template_id": 7})
    _parity_check([g], {10: [c]}, _evt(), _ctx())


def test_group_single_child_no_match_via_parent():
    g = _group(10, 50, match_to_domain="other.com")
    c = _child(11, 10, 60, action="auto_reply")
    _parity_check([g], {10: [c]}, _evt(), _ctx())


def test_group_single_child_no_match_via_child():
    g = _group(10, 50, match_to_domain="domarc.it")
    c = _child(11, 10, 60, action="auto_reply",
               match_subject_regex="(?i)^URGENTE$")
    _parity_check([g], {10: [c]}, _evt(subject="ciao"), _ctx())


# =========================================================================
# CASE 3 — Gruppo con più figli, continue_in_group
# =========================================================================

def test_group_two_children_continue_in_group():
    g = _group(10, 50, match_to_domain="domarc.it")
    c1 = _child(11, 10, 60, action="auto_reply", continue_in_group=1)
    c2 = _child(12, 10, 70, action="create_ticket", continue_in_group=0)
    _parity_check([g], {10: [c1, c2]}, _evt(), _ctx())


def test_group_two_children_first_does_not_match():
    g = _group(10, 50, match_to_domain="domarc.it")
    c1 = _child(11, 10, 60, match_subject_regex="(?i)urgente",
                action="auto_reply", continue_in_group=1)
    c2 = _child(12, 10, 70, action="create_ticket", continue_in_group=0)
    _parity_check([g], {10: [c1, c2]}, _evt(subject="info"), _ctx())


# =========================================================================
# CASE 4 — exclusive_match=True blocca gruppi successivi
# =========================================================================

def test_exclusive_match_true_blocks_next_group():
    g1 = _group(10, 50, match_to_domain="domarc.it", exclusive_match=1)
    c1 = _child(11, 10, 60, action="auto_reply", continue_in_group=0,
                exit_group_continue=0)
    g2 = _group(20, 100, match_to_domain="domarc.it")
    c2 = _child(21, 20, 110, action="create_ticket")
    _parity_check([g1, g2], {10: [c1], 20: [c2]}, _evt(), _ctx())


def test_exclusive_match_false_allows_next_group():
    g1 = _group(10, 50, match_to_domain="domarc.it", exclusive_match=0)
    c1 = _child(11, 10, 60, action="auto_reply", continue_in_group=0,
                exit_group_continue=0)
    g2 = _group(20, 100, match_to_domain="domarc.it")
    c2 = _child(21, 20, 110, action="create_ticket")
    _parity_check([g1, g2], {10: [c1], 20: [c2]}, _evt(), _ctx())


# =========================================================================
# CASE 5 — exit_group_continue=True salta i fratelli ma valuta gruppi successivi
# =========================================================================

def test_exit_group_continue_continues_in_group_and_top_level():
    """Con priority globale unica, ``exit_group_continue=True`` su un figlio
    si comporta come ``continue_in_group=True`` su quel figlio e forza
    l'ultimo figlio del gruppo a propagare ``continue=True`` ai top-level
    successivi (semantica documentata in ``derive_continue_flag``).
    """
    g1 = _group(10, 50, match_to_domain="domarc.it")
    c1 = _child(11, 10, 60, action="auto_reply", exit_group_continue=1)
    c2 = _child(12, 10, 70, action="create_ticket")
    g2 = _group(20, 100, match_to_domain="domarc.it")
    c3 = _child(21, 20, 110, action="flag_only")
    _parity_check([g1, g2], {10: [c1, c2], 20: [c3]}, _evt(), _ctx())


# =========================================================================
# CASE 6 — Tristate match_in_service
# =========================================================================

def test_in_service_constraint_skips_when_out():
    g = _group(10, 50, match_to_domain="domarc.it", match_in_service=0)  # solo fuori orario
    c = _child(11, 10, 60, action="auto_reply")
    _parity_check([g], {10: [c]}, _evt(), _ctx(in_service=True))   # in orario → skip
    _parity_check([g], {10: [c]}, _evt(), _ctx(in_service=False))  # fuori orario → match


def test_in_service_constraint_unidentified_customer():
    g = _group(10, 50, match_to_domain="domarc.it", match_in_service=1)
    c = _child(11, 10, 60, action="auto_reply")
    _parity_check([g], {10: [c]}, _evt(), _ctx(in_service=None))


# =========================================================================
# CASE 7 — Gruppo precede orfana, orfana precede gruppo
# =========================================================================

def test_orphan_before_group_priority():
    o = _orphan(1, 30, match_to_domain="domarc.it", action="ignore")
    g = _group(10, 50, match_to_domain="domarc.it")
    c = _child(11, 10, 60, action="auto_reply")
    _parity_check([o, g], {10: [c]}, _evt(), _ctx())


def test_group_before_orphan_priority():
    g = _group(10, 50, match_to_domain="domarc.it")
    c = _child(11, 10, 60, action="auto_reply")
    o = _orphan(1, 200, match_to_domain="domarc.it", action="ignore")
    _parity_check([g, o], {10: [c]}, _evt(), _ctx())


# =========================================================================
# CASE 8 — Match regex AND padre+figlio
# =========================================================================

def test_regex_combined_parent_child_both_match():
    g = _group(10, 50, match_to_domain="domarc.it",
               match_subject_regex="(?i)supporto")
    c = _child(11, 10, 60, action="auto_reply",
               match_subject_regex="(?i)firewall")
    _parity_check([g], {10: [c]}, _evt(subject="richiesta supporto firewall"), _ctx())
    _parity_check([g], {10: [c]}, _evt(subject="richiesta supporto generico"), _ctx())


def test_regex_combined_parent_only():
    g = _group(10, 50, match_to_domain="domarc.it",
               match_subject_regex="(?i)supporto")
    c = _child(11, 10, 60, action="auto_reply")
    _parity_check([g], {10: [c]}, _evt(subject="richiesta supporto"), _ctx())


# =========================================================================
# CASE 9 — Action_map ereditata viene veicolata correttamente
# =========================================================================

def test_action_map_inheritance_in_flatten():
    g = _group(10, 50, match_to_domain="domarc.it",
               action_map={"keep_original_delivery": True,
                           "also_deliver_to": "ticket@x.com"})
    c = _child(11, 10, 60, action="auto_reply",
               action_map={"template_id": 7})
    flat = flatten_rules([g], {10: [c]})
    assert len(flat) == 1
    am = flat[0]["action_map"]
    assert am["template_id"] == 7
    assert am["keep_original_delivery"] is True
    assert am["also_deliver_to"] == "ticket@x.com"


# =========================================================================
# CASE 10 — Disabilitati e gruppi vuoti
# =========================================================================

def test_disabled_group_skipped():
    g = _group(10, 50, match_to_domain="domarc.it", enabled=0)
    c = _child(11, 10, 60, action="auto_reply")
    _parity_check([g], {10: [c]}, _evt(), _ctx())


def test_disabled_child_skipped():
    g = _group(10, 50, match_to_domain="domarc.it")
    c1 = _child(11, 10, 60, action="auto_reply", enabled=0)
    c2 = _child(12, 10, 70, action="create_ticket")
    _parity_check([g], {10: [c1, c2]}, _evt(), _ctx())


def test_empty_group_skipped():
    g = _group(10, 50, match_to_domain="domarc.it")
    o = _orphan(1, 200, match_to_domain="domarc.it", action="ignore")
    _parity_check([g, o], {10: []}, _evt(), _ctx())


# =========================================================================
# CASE 11 — Chain matrix completa continue_in_group × exit_group_continue
# =========================================================================

@pytest.mark.parametrize("continue_in_group,exit_group_continue,exclusive", [
    (0, 0, 1),   # STOP totale, gruppo esclusivo
    (0, 0, 0),   # STOP totale, gruppo non esclusivo (ultimo figlio attiva continue)
    (1, 0, 1),   # continua nei fratelli
    (0, 1, 1),   # esce dal gruppo, continua top-level
    (1, 1, 1),   # continue_in_group prevale (ignora exit_group_continue per i non-ultimi)
])
def test_truth_table_first_child_matches(continue_in_group, exit_group_continue, exclusive):
    g1 = _group(10, 50, match_to_domain="domarc.it", exclusive_match=exclusive)
    c1 = _child(11, 10, 60, action="auto_reply",
                continue_in_group=continue_in_group,
                exit_group_continue=exit_group_continue)
    c2 = _child(12, 10, 70, action="create_ticket")
    g2 = _group(20, 100, match_to_domain="domarc.it")
    c3 = _child(21, 20, 110, action="flag_only")
    _parity_check([g1, g2], {10: [c1, c2], 20: [c3]}, _evt(), _ctx())


# =========================================================================
# CASE 12 — No match anywhere
# =========================================================================

def test_no_match_anywhere():
    g = _group(10, 50, match_to_domain="other.com")
    c = _child(11, 10, 60)
    o = _orphan(1, 200, match_to_domain="other2.com")
    _parity_check([g, o], {10: [c]}, _evt(), _ctx())


# =========================================================================
# CASE 13 — Stress: molti gruppi e orfane mescolate
# =========================================================================

def test_many_groups_and_orphans():
    items = []
    children: dict[int, list[dict[str, Any]]] = {}

    # 5 gruppi con 2-3 figli ciascuno + 5 orfane intercalate
    for gi in range(5):
        gid = 100 + gi * 100
        prio = 1000 + gi * 100
        g = _group(gid, prio, match_to_domain="domarc.it",
                   exclusive_match=(gi % 2))
        items.append(g)
        n_children = 2 + (gi % 2)
        cs = []
        for ci in range(n_children):
            cid = gid + 1 + ci
            c = _child(cid, gid, prio + 10 + ci * 5,
                       action=("auto_reply" if ci == 0 else "create_ticket"),
                       continue_in_group=(1 if ci < n_children - 1 else 0))
            cs.append(c)
        children[gid] = cs
        # orfana tra i gruppi
        items.append(_orphan(50000 + gi, prio + 80,
                             match_to_domain="domarc.it",
                             action="flag_only",
                             continue_after=(gi % 3 == 0)))

    _parity_check(items, children, _evt(), _ctx())


# =========================================================================
# CASE 14 — Eventi sintetici diversificati
# =========================================================================

@pytest.mark.parametrize("event_overrides", [
    {"to_domain": "domarc.it"},
    {"to_domain": "datia.it"},
    {"to_domain": "unknown.com"},
    {"subject": "URGENTE: server down"},
    {"subject": "ciao Roberto"},
    {"from_address": "noreply@bounce.example.com"},
    {"from_address": "info@cliente-noto.it"},
    {"body_text": "alert: cpu 99%"},
    {"body_text": ""},
])
def test_event_variations(event_overrides):
    g1 = _group(10, 50, match_to_domain="domarc.it",
                match_subject_regex="(?i)(URGENTE|critico|alert)")
    c1 = _child(11, 10, 60, action="create_ticket",
                action_map={"settore": "assistenza", "urgenza": "ALTA"})
    g2 = _group(20, 100, match_to_domain="datia.it")
    c2 = _child(21, 20, 110, action="auto_reply")
    o1 = _orphan(1, 30, match_from_regex="(?i)(noreply|mailer-daemon|bounce)",
                 action="ignore")
    o2 = _orphan(2, 999, action="flag_only", match_to_domain="domarc.it")  # catch-all domain
    _parity_check([o1, g1, g2, o2], {10: [c1], 20: [c2]},
                  _evt(**event_overrides), _ctx())


# =========================================================================
# CASE 15 — Event * Context combos (in_service)
# =========================================================================

@pytest.mark.parametrize("in_service", [True, False, None])
def test_in_service_combinations(in_service):
    g_in = _group(10, 50, match_to_domain="domarc.it", match_in_service=1)
    c_in = _child(11, 10, 60, action="auto_reply")
    g_out = _group(20, 100, match_to_domain="domarc.it", match_in_service=0)
    c_out = _child(21, 20, 110, action="create_ticket")
    _parity_check([g_in, g_out], {10: [c_in], 20: [c_out]},
                  _evt(), _ctx(in_service=in_service))


# Conta eventi sintetici cumulativi: ~50+ tra parametrizzati e fissi.
