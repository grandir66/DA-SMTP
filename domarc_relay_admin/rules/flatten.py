"""Flatten della gerarchia padre/figlio in regole flat per il listener legacy.

Il listener (`/opt/stormshield-smtp-relay/relay/rules.py`) NON conosce gruppi.
Lavora su un elenco lineare di regole con `priority`, `match_*`, `action`,
`action_map`, `continue_after_match`.

Con la scelta "priority globale unica" del progetto Domarc, il flatten è
semplice: ogni figlio diventa una regola flat che mantiene la propria
priority intatta, con i match_* combinati col padre (AND) e l'action_map
mergiata coi defaults ereditati.
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence

from .inheritance import deep_merge_action_map
from .validators import MATCH_FIELDS_TEXT, MATCH_FIELDS_TRISTATE


def combine_and(parent_regex: str | None, child_regex: str | None) -> str | None:
    """Combina due regex in AND tramite due lookahead. Vuoti → None."""
    p = (parent_regex or "").strip()
    c = (child_regex or "").strip()
    if not p and not c:
        return None
    if not p:
        return c
    if not c:
        return p
    return f"(?=.*{p})(?=.*{c}).*"


def combine_and_domain(parent: str | None, child: str | None) -> str | None:
    """Per un confronto esatto (lower-case) padre/figlio devono coincidere se
    entrambi presenti — V006 lo garantisce in fase di validazione, qui ci
    limitiamo a restituire il valore."""
    p = (parent or "").strip().lower() or None
    c = (child or "").strip().lower() or None
    if p and c and p != c:
        raise ValueError(f"Match domain incompatibile: padre={p!r}, figlio={c!r}")
    return c or p


def tristate_and(parent: Any, child: Any) -> Any:
    """Tristate: NULL=any, 1=true, 0=false. AND. Padre+figlio incompatibili
    sono bloccati a monte da V006 (validatore)."""
    if parent in (None, ""):
        return child if child not in ("",) else None
    if child in (None, ""):
        return parent
    if int(parent) != int(child):
        raise ValueError(f"Tristate incompatibile: padre={parent}, figlio={child}")
    return int(parent)


def derive_continue_flag(
    group: Mapping[str, Any],
    child: Mapping[str, Any],
    is_last_child: bool,
    *,
    any_sibling_exits: bool = False,
) -> bool:
    """Mappa la semantica gerarchica sul flag flat ``continue_after_match``.

    Con priority globale unica, ``exit_group_continue`` non può "saltare i
    fratelli successivi" — il listener legacy valuterà comunque i flat con
    priority intermedie. Per coerenza tra ``evaluate_v2`` e ``evaluate_legacy``,
    la semantica adottata è:

    - ``continue_in_group=True`` → ``continue_after_match=True`` (continua nei
      fratelli; il flusso top-level si bloccherà comunque sull'ultimo figlio
      se ``exclusive_match=True``).
    - ``exit_group_continue=True`` su un figlio → si comporta come
      ``continue_in_group=True`` su questo figlio E forza
      ``continue_after_match=True`` anche sull'ultimo figlio del gruppo (così
      il flusso top-level prosegue oltre il blocco).
    - ``continue_in_group=False, exit_group_continue=False`` → ``False`` (STOP).

    Edge case ``exclusive_match=False`` sul gruppo: l'ultimo figlio forza
    ``continue=True`` per non bloccare i gruppi successivi.
    """
    if child.get("continue_in_group") or child.get("exit_group_continue"):
        return True
    if is_last_child and not group.get("exclusive_match", 1):
        return True
    if is_last_child and any_sibling_exits:
        return True
    return False


def _merge_match_field(field: str, parent_value: Any, child_value: Any) -> Any:
    """Combina un singolo campo match_* tra padre e figlio."""
    if field in MATCH_FIELDS_TRISTATE:
        return tristate_and(parent_value, child_value)
    if field in ("match_to_domain", "match_from_domain"):
        return combine_and_domain(parent_value, child_value)
    if field in ("match_from_regex", "match_to_regex",
                 "match_subject_regex", "match_body_regex"):
        return combine_and(parent_value, child_value)
    # match_at_hours, match_tag: il figlio sovrascrive se presente, altrimenti eredita
    return child_value if child_value not in (None, "") else parent_value


def _build_flat_from_child(group: Mapping[str, Any], child: Mapping[str, Any],
                          *, is_last_child: bool,
                          any_sibling_exits: bool = False) -> dict[str, Any]:
    """Costruisce un record flat per un figlio di gruppo.

    Mantiene tutti i campi che il listener si aspetta + metadata `_source_*`
    opzionali (ignorati dal listener legacy)."""
    flat: dict[str, Any] = dict(child)  # parte dai valori del figlio
    flat.pop("parent_id", None)
    flat.pop("is_group", None)
    flat.pop("group_label", None)
    flat.pop("exclusive_match", None)
    flat.pop("continue_in_group", None)
    flat.pop("exit_group_continue", None)

    # Match merged
    for field in MATCH_FIELDS_TEXT + MATCH_FIELDS_TRISTATE:
        flat[field] = _merge_match_field(field, group.get(field), child.get(field))

    # Action_map merged
    flat["action_map"] = deep_merge_action_map(
        group.get("action_map") or {},
        child.get("action_map") or {},
    )

    # continue_after_match derivato
    flat["continue_after_match"] = derive_continue_flag(
        group, child, is_last_child, any_sibling_exits=any_sibling_exits,
    )

    # Scope: il figlio eredita lo scope del padre se non lo dichiara
    if not flat.get("scope_type") or flat.get("scope_type") == "global":
        if group.get("scope_type") and group.get("scope_type") != "global":
            flat["scope_type"] = group["scope_type"]
            flat["scope_ref"] = group.get("scope_ref")

    # Metadata (ignorati dal listener legacy, utili per audit)
    flat["_source_group_id"] = group.get("id")
    flat["_source_child_id"] = child.get("id")
    return flat


def flatten_rules(
    top_level: Sequence[Mapping[str, Any]],
    children_by_parent: Mapping[int, Sequence[Mapping[str, Any]]],
    *,
    only_enabled: bool = True,
) -> list[dict[str, Any]]:
    """Appiattisce gerarchia in regole flat compatibili col listener.

    Args:
        top_level: orfane + gruppi, ordinate per priority ASC.
        children_by_parent: dict ``{group_id: [child, child, ...]}``,
            con figli ordinati per priority ASC. I gruppi senza figli vengono
            scartati silenziosamente (W001 a parte).
        only_enabled: se True, scarta record con ``enabled=0`` sia top-level
            che figli.

    Returns:
        Lista di dict flat ordinata per priority globale ASC.
    """
    flat: list[dict[str, Any]] = []
    for item in top_level:
        if only_enabled and not item.get("enabled"):
            continue
        if item.get("is_group"):
            children = children_by_parent.get(item["id"], [])
            if only_enabled:
                children = [c for c in children if c.get("enabled")]
            if not children:
                continue
            n = len(children)
            any_sibling_exits = any(c.get("exit_group_continue") for c in children)
            for idx, child in enumerate(children):
                flat.append(_build_flat_from_child(
                    item, child, is_last_child=(idx == n - 1),
                    any_sibling_exits=any_sibling_exits,
                ))
        else:
            # Orfana: invariata, drop dei campi gerarchici a None
            orphan = dict(item)
            orphan.pop("parent_id", None)
            orphan.pop("is_group", None)
            orphan.pop("group_label", None)
            orphan.pop("exclusive_match", None)
            orphan.pop("continue_in_group", None)
            orphan.pop("exit_group_continue", None)
            flat.append(orphan)

    flat.sort(key=lambda r: (int(r.get("priority") or 999999), int(r.get("id") or 0) if isinstance(r.get("id"), int) else 0))
    return flat
