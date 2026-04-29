"""Validatori del Rule Engine v2.

Distingue **errori bloccanti** (V001-V008, V_PRI_RANGE) da **warning soft**
(W001-W005, W_PRI_GAP). I primi impediscono il salvataggio della regola; i
secondi vengono mostrati in UI come avvisi non bloccanti.

I match_* riconosciuti sono tutti quelli presenti nello schema `rules` post
migration 009 (vedi 001_initial.sqlite.sql + 008/009).
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence

from .action_map_schema import (
    ALL_KNOWN_ACTION_MAP_KEYS,
    CHILD_ONLY_ACTION_MAP,
    PARENT_ACTION_MAP_DEFAULTS,
)

MATCH_FIELDS_TEXT: tuple[str, ...] = (
    "match_from_regex",
    "match_to_regex",
    "match_subject_regex",
    "match_body_regex",
    "match_to_domain",
    "match_from_domain",
    "match_at_hours",
    "match_tag",
)

MATCH_FIELDS_TRISTATE: tuple[str, ...] = (
    "match_in_service",
    "match_contract_active",
    "match_known_customer",
    "match_has_exception_today",
)

MATCH_FIELDS_ALL: tuple[str, ...] = MATCH_FIELDS_TEXT + MATCH_FIELDS_TRISTATE

PRIORITY_MIN = 1
PRIORITY_MAX = 999_999


class ValidationError(ValueError):
    """Errore di validazione bloccante. ``code`` corrisponde a Vxxx."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def _has_any_match(rule: Mapping[str, Any]) -> bool:
    return any(
        rule.get(field) not in (None, "")
        for field in MATCH_FIELDS_ALL
    )


def _matches_compatible(
    field: str,
    parent_value: Any,
    child_value: Any,
) -> bool:
    """Restituisce True se padre e figlio possono coesistere su questo campo.

    Per i campi di dominio (esact match) i due valori, se entrambi presenti,
    devono coincidere. Per i tristate idem. Per regex/text generici è ammesso
    coesistere (verranno combinati in AND lookahead in fase di flatten).
    """
    if parent_value in (None, ""):
        return True
    if child_value in (None, ""):
        return True
    if field in ("match_to_domain", "match_from_domain"):
        return str(parent_value).lower() == str(child_value).lower()
    if field in MATCH_FIELDS_TRISTATE:
        return int(parent_value) == int(child_value)
    return True


def validate_rule(
    rule: Mapping[str, Any],
    *,
    parent: Mapping[str, Any] | None = None,
    siblings: Sequence[Mapping[str, Any]] | None = None,
    next_top_level_priority: int | None = None,
) -> tuple[list[ValidationError], list[str]]:
    """Valida una singola regola in fase di salvataggio.

    Args:
        rule: dict con i campi della regola in input. Deve contenere ``is_group``,
            ``parent_id``, ``priority`` e i match_*.
        parent: il record padre (gruppo) se ``rule.parent_id`` è settato.
        siblings: figli già esistenti dello stesso gruppo (per check duplicati
            priority); escludere il record stesso se è un update.
        next_top_level_priority: priority del prossimo top-level (gruppo o
            orfana) successivo al padre, per V_PRI_RANGE. None se il padre è
            l'ultimo blocco.

    Returns:
        ``(errors, warnings)``. Caller deve sollevare se ``errors`` non vuoto.
    """
    errors: list[ValidationError] = []
    warnings: list[str] = []

    is_group = bool(rule.get("is_group"))
    parent_id = rule.get("parent_id")
    priority = int(rule.get("priority") or 0)

    # V001 — gruppo non può avere padre
    if is_group and parent_id not in (None, 0):
        errors.append(ValidationError("V001", "Un gruppo non può avere un padre (max 1 livello)."))

    # V002 — il padre referenziato deve essere un gruppo
    if parent_id and parent is not None and not parent.get("is_group"):
        errors.append(ValidationError("V002", "Il padre referenziato non è un gruppo."))

    # V008 — anche se il padre è un gruppo, il figlio non può a sua volta essere gruppo
    if is_group and parent_id:
        errors.append(ValidationError("V008", "Un gruppo non può essere figlio di un altro gruppo."))

    # V003 — i gruppi non eseguono azioni proprie
    if is_group:
        action = (rule.get("action") or "").strip()
        if action and action.lower() not in ("none", "group", "noop"):
            errors.append(ValidationError(
                "V003",
                f"I gruppi non eseguono azioni dirette (action='{action}'); usare action_map "
                "solo per i defaults ereditabili.",
            ))
        action_map = rule.get("action_map") or {}
        if isinstance(action_map, Mapping):
            invalid_keys = [k for k in action_map.keys() if k in CHILD_ONLY_ACTION_MAP]
            if invalid_keys:
                errors.append(ValidationError(
                    "V003",
                    f"Chiavi action_map figlio-only non consentite sul gruppo: {sorted(invalid_keys)}.",
                ))

    # V004 — gruppo senza filtri = catch-all gerarchico vietato
    if is_group and not _has_any_match(rule):
        errors.append(ValidationError(
            "V004",
            "Un gruppo deve avere almeno un match_* (catch-all gerarchico vietato).",
        ))

    # V005 — riferimento circolare
    rid = rule.get("id")
    if rid is not None and parent_id == rid:
        errors.append(ValidationError("V005", "Riferimento circolare: parent_id == id."))

    # V006 — match incompatibili padre/figlio
    if parent_id and parent is not None and not is_group:
        for field in MATCH_FIELDS_ALL:
            if not _matches_compatible(field, parent.get(field), rule.get(field)):
                errors.append(ValidationError(
                    "V006",
                    f"Match figlio incompatibile con il padre sul campo {field} "
                    f"(padre={parent.get(field)!r}, figlio={rule.get(field)!r}).",
                ))

    # V007 — priority fuori range globale
    if not (PRIORITY_MIN <= priority <= PRIORITY_MAX):
        errors.append(ValidationError(
            "V007",
            f"Priority {priority} fuori range globale {PRIORITY_MIN}..{PRIORITY_MAX}.",
        ))

    # V_PRI_RANGE — figlio: priority strettamente maggiore del padre e minore
    # del prossimo top-level (gruppo/orfana successiva).
    if parent_id and parent is not None and not is_group:
        parent_priority = int(parent.get("priority") or 0)
        if priority <= parent_priority:
            errors.append(ValidationError(
                "V_PRI_RANGE",
                f"Priority del figlio ({priority}) deve essere maggiore di quella del padre "
                f"({parent_priority}).",
            ))
        if next_top_level_priority is not None and priority >= next_top_level_priority:
            errors.append(ValidationError(
                "V_PRI_RANGE",
                f"Priority del figlio ({priority}) deve essere minore di quella del prossimo "
                f"top-level ({next_top_level_priority}). Riassegna le priorità del blocco.",
            ))

    # ============================== Warning soft ==============================

    if is_group and not (rule.get("group_label") or rule.get("name")):
        warnings.append("W: Il gruppo non ha né nome né group_label, sarà difficile identificarlo in UI.")

    if parent_id and not is_group and not _has_any_match(rule):
        warnings.append(
            "W002: Il figlio non ha match_* propri: erediterà solo dal padre. "
            "Se è intenzionale, ignora; altrimenti aggiungi un match per affinare."
        )

    if parent_id and parent is not None and not is_group:
        for field in MATCH_FIELDS_ALL:
            pv = parent.get(field)
            cv = rule.get(field)
            if pv not in (None, "") and cv not in (None, "") and pv == cv:
                warnings.append(
                    f"W004: Match ridondante sul campo {field}: figlio ripete il valore del "
                    f"padre ({pv!r}). Rimuovilo dal figlio per evitare duplicazione."
                )

    # W_PRI_GAP — suggerimento di lasciare gap >= 10 tra figli per inserimenti futuri
    if siblings is not None and not is_group and parent_id:
        nearest = min(
            (abs(int(s.get("priority") or 0) - priority) for s in siblings if s.get("id") != rid),
            default=None,
        )
        if nearest is not None and nearest < 5:
            warnings.append(
                f"W_PRI_GAP: Distanza minima dalla priority dei fratelli è {nearest}. "
                "Si consiglia un gap di almeno 10 per consentire inserimenti futuri senza "
                "rinumerare il blocco."
            )

    # W: chiavi action_map sconosciute (non bloccante)
    am = rule.get("action_map") or {}
    if isinstance(am, Mapping):
        unknown = [k for k in am.keys() if k not in ALL_KNOWN_ACTION_MAP_KEYS]
        if unknown:
            warnings.append(
                f"W: Chiavi action_map non riconosciute (verranno ignorate): {sorted(unknown)}."
            )

    # W005 — gruppo non-exclusive con ultimo figlio "STOP"
    # Nota: W005 vero richiede contesto a livello di gruppo; lo gestisce
    # validate_group_consistency() (vedi sotto).

    return errors, warnings


def validate_group_consistency(
    group: Mapping[str, Any],
    children: Sequence[Mapping[str, Any]],
) -> list[str]:
    """Warning a livello di gruppo (W001, W005). Eseguito dopo update di un
    gruppo o di uno dei suoi figli per dare un colpo d'occhio coerente."""
    warnings: list[str] = []
    if not children:
        warnings.append("W001: Gruppo senza figli, nessun effetto a runtime.")
        return warnings

    if not group.get("exclusive_match"):
        last = sorted(children, key=lambda c: int(c.get("priority") or 0))[-1]
        if not last.get("continue_in_group") and not last.get("exit_group_continue"):
            warnings.append(
                "W005: Gruppo con exclusive_match=False ma l'ultimo figlio ha STOP totale "
                "(continue_in_group=False e exit_group_continue=False). I gruppi successivi "
                "non saranno valutati. Se l'intento è bloccare, imposta exclusive_match=True; "
                "altrimenti abilita exit_group_continue sull'ultimo figlio."
            )

    return warnings


def raise_if_errors(errors: Sequence[ValidationError]) -> None:
    """Helper per il route layer."""
    if errors:
        raise errors[0]
