"""Engine v2 — valutazione gerarchica padre/figlio.

Usato lato admin per:

- **Simulazione UI** (``/rules/simulate``): mostrare passo-passo il path di
  valutazione padre→figlio con match per match e action_map ereditata.
- **Test di parità**: verificare che ``evaluate_v2(...) == evaluate_legacy(flatten(...))``
  per ogni evento sintetico (gate di rilascio Fase 2).

Il listener vero non chiama mai questo modulo.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from .flatten import _merge_match_field, derive_continue_flag
from .inheritance import deep_merge_action_map
from .legacy_evaluator import _rule_matches, _scope_matches, _service_constraint_skip
from .validators import MATCH_FIELDS_TEXT, MATCH_FIELDS_TRISTATE


@dataclass
class StepTrace:
    kind: str  # "group", "child", "orphan"
    rule_id: int
    rule_name: str | None
    priority: int
    matched: bool
    parent_id: int | None = None
    reasons: list[str] = field(default_factory=list)
    effective_action_map: dict[str, Any] = field(default_factory=dict)


@dataclass
class V2Outcome:
    winners: list[dict[str, Any]]
    chain: list[StepTrace]


def _materialize_child_for_match(group: Mapping[str, Any],
                                 child: Mapping[str, Any]) -> dict[str, Any]:
    """Costruisce la "vista flat" del figlio mergiando match_* col padre.

    Identica al record che ``flatten_rules`` emetterebbe per quel figlio,
    senza l'override di ``continue_after_match`` (qui gestiamo il flusso
    direttamente).
    """
    materialized: dict[str, Any] = dict(child)
    for field_name in MATCH_FIELDS_TEXT + MATCH_FIELDS_TRISTATE:
        materialized[field_name] = _merge_match_field(
            field_name, group.get(field_name), child.get(field_name)
        )
    materialized["action_map"] = deep_merge_action_map(
        group.get("action_map") or {},
        child.get("action_map") or {},
    )
    if not materialized.get("scope_type") or materialized.get("scope_type") == "global":
        if group.get("scope_type") and group.get("scope_type") != "global":
            materialized["scope_type"] = group["scope_type"]
            materialized["scope_ref"] = group.get("scope_ref")
    return materialized


def evaluate_v2(
    top_level: Sequence[Mapping[str, Any]],
    children_by_parent: Mapping[int, Sequence[Mapping[str, Any]]],
    event: Mapping[str, Any],
    context: Mapping[str, Any],
) -> V2Outcome:
    """Valuta direttamente sul modello gerarchico (NO flatten preliminare)."""
    chain: list[StepTrace] = []
    winners: list[dict[str, Any]] = []
    # ``next_continue`` rappresenta il flag ``continue_after_match`` dell'ultima
    # regola matchata (mimica del listener legacy). Se False → STOP. Se True →
    # si valuta il prossimo top-level. Si resetta implicitamente a ogni nuovo
    # top-level che matcha.
    next_continue = True

    sorted_top = sorted(
        (t for t in top_level if t.get("enabled")),
        key=lambda r: (int(r.get("priority") or 999999), int(r.get("id") or 0)),
    )

    for item in sorted_top:
        if winners and not next_continue:
            break

        if not item.get("is_group"):
            # Orfana: valutazione diretta
            if not _scope_matches(item, item.get("scope_type", "global"), context):
                continue
            if _service_constraint_skip(item, context):
                chain.append(StepTrace(
                    kind="orphan", rule_id=int(item["id"]), rule_name=item.get("name"),
                    priority=int(item.get("priority", 100)), matched=False,
                    reasons=["skip vincolo orario"],
                ))
                continue
            matched = _rule_matches(item, event)
            chain.append(StepTrace(
                kind="orphan", rule_id=int(item["id"]), rule_name=item.get("name"),
                priority=int(item.get("priority", 100)), matched=matched,
                reasons=[],
            ))
            if matched:
                winners.append(dict(item))
                next_continue = bool(item.get("continue_after_match"))
            continue

        # Gruppo: padre deve matchare a sua volta (AND di tutti i suoi match_*)
        group_matches = _rule_matches(item, event) and not _service_constraint_skip(item, context)
        chain.append(StepTrace(
            kind="group", rule_id=int(item["id"]),
            rule_name=item.get("group_label") or item.get("name"),
            priority=int(item.get("priority", 100)), matched=group_matches,
            reasons=[],
        ))
        if not group_matches:
            continue

        children = children_by_parent.get(item["id"], [])
        children = [c for c in children if c.get("enabled")]
        sorted_children = sorted(
            children, key=lambda c: (int(c.get("priority") or 999999), int(c.get("id") or 0))
        )
        n = len(sorted_children)
        any_sibling_exits = any(c.get("exit_group_continue") for c in sorted_children)
        any_child_matched = False
        last_child_matched_idx = -1
        last_child_cont = False
        for idx, raw_child in enumerate(sorted_children):
            mat = _materialize_child_for_match(item, raw_child)
            if _service_constraint_skip(mat, context):
                chain.append(StepTrace(
                    kind="child", rule_id=int(raw_child["id"]),
                    rule_name=raw_child.get("name"), parent_id=int(item["id"]),
                    priority=int(raw_child.get("priority", 100)), matched=False,
                    reasons=["skip vincolo orario"],
                ))
                continue
            child_matched = _rule_matches(mat, event)
            chain.append(StepTrace(
                kind="child", rule_id=int(raw_child["id"]),
                rule_name=raw_child.get("name"), parent_id=int(item["id"]),
                priority=int(raw_child.get("priority", 100)), matched=child_matched,
                reasons=[],
                effective_action_map=mat.get("action_map", {}),
            ))
            if not child_matched:
                continue
            any_child_matched = True
            last_child_matched_idx = idx
            winners.append(mat)
            last_child_cont = derive_continue_flag(
                item, raw_child, idx == n - 1, any_sibling_exits=any_sibling_exits,
            )
            if last_child_cont:
                # continua nei fratelli successivi (continue_in_group o exit_group_continue
                # o ultimo figlio con exclusive_match=False / sibling-exit)
                continue
            break

        # Dopo aver finito di processare il gruppo: il flag verso il prossimo
        # top-level dipende dall'ultimo figlio matchato. Se è effettivamente
        # l'ultimo figlio del gruppo e ``last_child_cont`` è True, propaghiamo;
        # altrimenti STOP. Se nessun figlio ha matchato, il gruppo non altera
        # ``next_continue`` (continua come da contesto precedente — questo è
        # consistente col listener: il padre matcha solo se almeno un figlio
        # matcha, lato flat).
        if any_child_matched:
            next_continue = bool(
                last_child_matched_idx == n - 1 and last_child_cont
            )

    return V2Outcome(winners=winners, chain=chain)
