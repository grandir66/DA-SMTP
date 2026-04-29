"""Replica della logica di valutazione del listener legacy.

Replica fedelmente :class:`relay.rules.RuleEngine` di
``/opt/stormshield-smtp-relay/relay/rules.py`` (riassunto):

- Le regole sono organizzate per ``scope_type`` (sector_pack, global). Lo
  scope ``mailbox`` esiste nel manager ma non è considerato dal listener SMTP.
- Per ogni scope, le regole abilitate sono ordinate per ``priority`` ASC.
- Ogni regola viene valutata con AND tra: scope, vincolo orario
  ``match_in_service``, regex su ``match_from_regex``/``match_to_regex``/
  ``match_subject_regex``/``match_body_regex``, e ``match_to_domain``
  (uguaglianza case-insensitive).
- Alla prima regola che matcha, se ``continue_after_match`` è False ci si
  ferma. Altrimenti si continua con la regola successiva (anche tra scope
  diversi: ``sector_pack`` → ``global``).
- I match_* aggiunti dalle migration 008/009 (``match_from_domain``,
  ``match_contract_active``, ``match_known_customer``,
  ``match_has_exception_today``, ``match_at_hours``, ``match_tag``) NON sono
  letti dal listener (saranno applicati in versioni future del listener); il
  legacy_evaluator li ignora come fa il listener.

Questo modulo è usato SOLO nei test di parità (``tests/test_rule_engine_parity.py``)
per garantire che il flatten del Rule Engine v2 produce regole flat che
darebbero lo stesso risultato del listener attuale.
"""
from __future__ import annotations

import re
from typing import Any, Mapping, Sequence

SCOPE_ORDER: tuple[str, ...] = ("sector_pack", "global")


def _safe_search(pattern: str, haystack: str) -> bool:
    """Versione semplificata di ``relay._safe_search`` (no thread timeout, no
    lunghezza max — il test usa input controllati). Restituisce True se match.
    """
    try:
        return bool(re.search(pattern, haystack or "", re.IGNORECASE))
    except re.error:
        return False


def _scope_matches(rule: Mapping[str, Any], scope: str, context: Mapping[str, Any]) -> bool:
    if rule.get("scope_type", "global") != scope:
        return False
    if scope == "global":
        return True
    scope_ref = rule.get("scope_ref")
    if not scope_ref:
        return False
    return str(context.get("sector")) == str(scope_ref)


def _service_constraint_skip(rule: Mapping[str, Any], context: Mapping[str, Any]) -> bool:
    """True = skip (regola non si applica per vincolo orario)."""
    constraint = rule.get("match_in_service")
    if constraint is None:
        return False
    in_service = context.get("in_service")
    if in_service is None:
        return True  # cliente non identificato → skip
    constraint_b = bool(int(constraint))
    return constraint_b != bool(in_service)


def _rule_matches(rule: Mapping[str, Any], event: Mapping[str, Any]) -> bool:
    regex_tests = (
        ("match_from_regex", event.get("from_address") or ""),
        ("match_to_regex", event.get("to_address") or ""),
        ("match_subject_regex", event.get("subject") or ""),
        ("match_body_regex", event.get("body_text") or ""),
    )
    for fld, haystack in regex_tests:
        pattern = rule.get(fld)
        if not pattern:
            continue
        if not _safe_search(pattern, haystack):
            return False

    to_domain = rule.get("match_to_domain")
    if to_domain:
        event_to_domain = (event.get("to_domain") or "").lower()
        if event_to_domain != str(to_domain).lower():
            return False
    return True


def evaluate_legacy(
    flat_rules: Sequence[Mapping[str, Any]],
    event: Mapping[str, Any],
    context: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Valuta una lista di regole flat come farebbe il listener.

    Returns:
        Lista delle regole "vincenti" in ordine di esecuzione. Lista vuota se
        nessuna ha matchato.
    """
    grouped: dict[str, list[Mapping[str, Any]]] = {s: [] for s in SCOPE_ORDER}
    for rule in flat_rules:
        if not rule.get("enabled", 1):
            continue
        stype = rule.get("scope_type", "global")
        if stype in grouped:
            grouped[stype].append(rule)
    for lst in grouped.values():
        lst.sort(key=lambda r: int(r.get("priority", 999999) or 999999))

    winners: list[dict[str, Any]] = []
    last_continue = False
    for scope in SCOPE_ORDER:
        if winners and not last_continue:
            break
        for rule in grouped[scope]:
            if not _scope_matches(rule, scope, context):
                continue
            if _service_constraint_skip(rule, context):
                continue
            if not _rule_matches(rule, event):
                continue
            winners.append(dict(rule))
            last_continue = bool(rule.get("continue_after_match"))
            if not last_continue:
                break
    return winners
