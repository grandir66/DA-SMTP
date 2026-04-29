"""Rule Engine v2 — gerarchia padre/figlio (1 livello).

Modulo introdotto con la migration 010. Contiene:

- ``action_map_schema``: whitelist delle chiavi action_map ereditabili dal padre
  vs chiavi figlio-only.
- ``inheritance``: merge dell'action_map effettiva (padre+figlio).
- ``validators``: vincoli hard (V001-V008, V_PRI_RANGE) e warning soft.
- ``flatten``: traduzione gerarchia → regole flat compatibili col listener.
- ``evaluator``: simulatore v2 per UI di test e suite di parità.
- ``legacy_evaluator``: replica della logica del listener (priority ASC + AND
  match + continue_after_match), usata SOLO nei test di parità.

Il listener vero (`/opt/stormshield-smtp-relay/relay/rules.py`) NON viene
modificato: riceve regole flat tramite l'endpoint
``/api/v1/relay/rules/active``, transparente alla gerarchia.
"""

from .action_map_schema import (
    CHILD_ONLY_ACTION_MAP,
    CHILD_OVERRIDABLE,
    PARENT_ACTION_MAP_DEFAULTS,
)
from .inheritance import deep_merge_action_map

__all__ = [
    "PARENT_ACTION_MAP_DEFAULTS",
    "CHILD_ONLY_ACTION_MAP",
    "CHILD_OVERRIDABLE",
    "deep_merge_action_map",
]
