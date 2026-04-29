"""Whitelist applicativa delle chiavi action_map.

Il modello padre/figlio prevede che i gruppi possano fornire SOLO defaults
ereditabili (side-effect globali, parametri auto_reply riusabili). Le chiavi
strettamente legate a un'azione specifica restano sui figli.
"""
from __future__ import annotations

# Side-effect globali e defaults ereditabili dal padre verso i figli.
# Un gruppo può impostare queste chiavi nel proprio action_map (defaults),
# ogni figlio le eredita salvo override esplicito.
PARENT_ACTION_MAP_DEFAULTS: frozenset[str] = frozenset({
    # delivery
    "keep_original_delivery",
    "also_deliver_to",
    "apply_rules",
    # auto_reply defaults
    "reply_mode",
    "reply_subject_prefix",
    "reply_quote_original",
    "reply_attach_original",
    "reply_to",
    "generate_auth_code",
    "auth_code_ttl_hours",
})

# Chiavi che non hanno senso a livello padre (sono sempre azione-specifiche).
# Il validatore V003 le rifiuta sui gruppi.
CHILD_ONLY_ACTION_MAP: frozenset[str] = frozenset({
    # auto_reply
    "template_id",
    # create_ticket
    "settore",
    "urgenza",
    "addetto_gestione",
    # forward
    "forward_target",
    "forward_port",
    "forward_tls",
    # redirect
    "redirect_to",
    "reason",
})

# Chiavi che il figlio può sovrascrivere rispetto ai defaults del padre.
# Coincide con la whitelist padre: tutto ciò che il padre fornisce, il figlio
# può ridichiarare per scavalcarlo.
CHILD_OVERRIDABLE: frozenset[str] = PARENT_ACTION_MAP_DEFAULTS

# Tutte le chiavi action_map riconosciute dal sistema. Usata per warning su
# chiavi sconosciute (W non bloccante).
ALL_KNOWN_ACTION_MAP_KEYS: frozenset[str] = (
    PARENT_ACTION_MAP_DEFAULTS | CHILD_ONLY_ACTION_MAP
)
