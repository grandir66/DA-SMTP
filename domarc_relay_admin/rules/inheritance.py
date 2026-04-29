"""Merge action_map padre+figlio per il Rule Engine v2."""
from __future__ import annotations

from typing import Any, Mapping

from .action_map_schema import PARENT_ACTION_MAP_DEFAULTS


def deep_merge_action_map(
    parent_defaults: Mapping[str, Any] | None,
    child_action_map: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Combina i defaults ereditabili del padre con l'action_map del figlio.

    Regole:
    - Il padre fornisce SOLO le chiavi della whitelist
      :data:`PARENT_ACTION_MAP_DEFAULTS`. Eventuali chiavi extra nel
      ``parent_defaults`` (artefatti legacy) vengono ignorate silenziosamente.
    - Il figlio sovrascrive qualsiasi chiave (incluse quelle ereditate).
    - I valori ``None`` lato figlio NON sono override (significano "non
      specificato"). Per resettare un default ereditato, il figlio deve
      passare un valore neutro esplicito.
    - Le liste/CSV non vengono concatenate: ``also_deliver_to`` figlio sostituisce
      completamente quella del padre.
    """
    effective: dict[str, Any] = {}

    if parent_defaults:
        for key, value in parent_defaults.items():
            if key in PARENT_ACTION_MAP_DEFAULTS and value is not None:
                effective[key] = value

    if child_action_map:
        for key, value in child_action_map.items():
            if value is not None:
                effective[key] = value

    return effective


def split_inherited_keys(
    parent_defaults: Mapping[str, Any] | None,
    effective_action_map: Mapping[str, Any],
) -> tuple[set[str], set[str]]:
    """Restituisce ``(inherited_keys, own_keys)`` per l'audit log.

    ``inherited_keys`` = chiavi presenti nel risultato che provengono dai
    defaults del padre senza override del figlio.
    """
    if not parent_defaults:
        return set(), set(effective_action_map.keys())
    inherited: set[str] = set()
    own: set[str] = set()
    for key, value in effective_action_map.items():
        parent_value = parent_defaults.get(key) if parent_defaults else None
        if (
            key in PARENT_ACTION_MAP_DEFAULTS
            and parent_value is not None
            and parent_value == value
        ):
            inherited.add(key)
        else:
            own.add(key)
    return inherited, own
