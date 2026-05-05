"""Rule engine locale del relay (porting da modules/ingestion/ingestion_rules.py).

Le regole sono **dati cached** scaricati dal manager via API (non query DB locali).
Lo schema delle regole è quello di `ingestion_rules` del manager, esteso con i campi
specifici SMTP (`match_to_domain`, `match_in_service`).

Tre scope in ordine di valutazione (come nel manager):
    1. mailbox      (ignorato in flusso SMTP — non c'è il concetto di mailbox lato relay)
    2. sector_pack  (scope_ref = settore cliente, opzionale)
    3. global       (scope_ref = NULL)

Per il flusso SMTP saltiamo direttamente a sector_pack/global; la mailbox è IMAP-only.
"""
from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

SCOPE_ORDER = ("sector_pack", "global")

_REGEX_MAX_LEN = 500
_REGEX_HAYSTACK_MAX = 16 * 1024
_REGEX_TIMEOUT_SEC = 0.5

_REDOS_HEURISTICS = [
    re.compile(r"\([^)]*[+*]\)[+*]"),
    re.compile(r"\([^)]*\?\)[+*]\+"),
    re.compile(r"\(\?\:[^)]*[+*]\)[+*]"),
    re.compile(r"\((?:[^|()]+\|){2,}[^|()]+\)[+*]"),
]


def _looks_redos(pattern: str) -> bool:
    if len(pattern) > _REGEX_MAX_LEN:
        return True
    return any(h.search(pattern) for h in _REDOS_HEURISTICS)


def _safe_search(pattern: str, haystack: str, flags: int = re.IGNORECASE,
                 timeout: float = _REGEX_TIMEOUT_SEC) -> re.Match[str] | None:
    if len(haystack) > _REGEX_HAYSTACK_MAX:
        haystack = haystack[:_REGEX_HAYSTACK_MAX]
    box: list[Any] = [None, None]

    def _run() -> None:
        try:
            box[0] = re.search(pattern, haystack, flags)
        except Exception as exc:  # noqa: BLE001
            box[1] = exc

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        logger.warning("Regex timeout (%.2fs) su pattern lungo %d chars", timeout, len(pattern))
        return None
    if box[1] is not None:
        raise box[1]
    return box[0]


@dataclass
class ChainStep:
    scope: str
    rule_id: int
    rule_name: str | None
    priority: int
    matched: bool
    reasons: list[str] = field(default_factory=list)


@dataclass
class RuleOutcome:
    rule: dict[str, Any] | None
    scope: str | None
    chain: list[ChainStep] = field(default_factory=list)


def _row_to_dict(row: Any) -> dict[str, Any]:
    if isinstance(row, dict):
        return dict(row)
    return {k: row[k] for k in row.keys()}


class RuleEngine:
    """Valuta regole SMTP su un evento + contesto cliente (sincrono, in-process)."""

    def __init__(self, rules: list[dict[str, Any]] | None = None):
        self._rules = [self._normalize(r) for r in (rules or [])]

    @staticmethod
    def _normalize(r: Any) -> dict[str, Any]:
        d = _row_to_dict(r)
        if "action_map" not in d and "action_map_json" in d:
            import json
            try:
                d["action_map"] = json.loads(d["action_map_json"] or "{}")
            except (TypeError, ValueError):
                d["action_map"] = {}
        d.setdefault("action_map", {})
        d.setdefault("scope_type", "global")
        d.setdefault("priority", 100)
        d.setdefault("continue_after_match", False)
        return d

    def evaluate(self, event: dict[str, Any], context: dict[str, Any],
                   *, exclude_rule_ids: set[int] | None = None,
                   active_rule_set_ids: set[int] | None = None) -> RuleOutcome:
        """Valuta le regole.

        ``exclude_rule_ids`` (Fix B 2026-05-05): salta le regole con id presente
        nel set. Usato dal pipeline per re-evaluare dopo un falso positivo H24
        (codice estratto via regex larga ma non trovato in DB).

        ``active_rule_set_ids`` (M029): se valorizzato, considera solo le regole
        appartenenti a uno di questi rule_set. Tipicamente: il set "globali"
        (sempre attivo) + il set associato al profilo orario del cliente
        (standard/esteso/h24/nessuno). Se None: nessun filtro (compat
        pre-M029).
        """
        chain: list[ChainStep] = []
        winning: dict[str, Any] | None = None
        winning_scope: str | None = None
        exclude = set(exclude_rule_ids or ())
        active_sets = set(active_rule_set_ids) if active_rule_set_ids is not None else None

        grouped: dict[str, list[dict[str, Any]]] = {s: [] for s in SCOPE_ORDER}
        for rule in self._rules:
            # M029: filtra per rule_set attivo. Le regole con rule_set_id=NULL
            # (legacy o non ancora migrate) sono sempre incluse per safety.
            if active_sets is not None:
                rsid = rule.get("rule_set_id")
                if rsid is not None and int(rsid) not in active_sets:
                    continue
            stype = rule.get("scope_type", "global")
            if stype in grouped:
                grouped[stype].append(rule)
        for lst in grouped.values():
            lst.sort(key=lambda r: r.get("priority", 999999))

        for scope in SCOPE_ORDER:
            if winning is not None and not winning.get("continue_after_match"):
                break
            for rule in grouped[scope]:
                if int(rule.get("id", 0)) in exclude:
                    continue
                if not self._scope_matches(rule, scope, context):
                    continue

                inservice_skip = self._service_constraint_skip(rule, context)
                if inservice_skip:
                    chain.append(
                        ChainStep(
                            scope=scope,
                            rule_id=int(rule["id"]),
                            rule_name=rule.get("name"),
                            priority=int(rule.get("priority", 100)),
                            matched=False,
                            reasons=[f"skip (orario): {inservice_skip}"],
                        )
                    )
                    continue

                match_result = self._rule_matches(rule, event)
                chain.append(
                    ChainStep(
                        scope=scope,
                        rule_id=int(rule["id"]),
                        rule_name=rule.get("name"),
                        priority=int(rule.get("priority", 100)),
                        matched=match_result["matched"],
                        reasons=match_result["reasons"],
                    )
                )
                if match_result["matched"]:
                    winning = rule
                    winning_scope = scope
                    if not rule.get("continue_after_match"):
                        return RuleOutcome(rule=winning, scope=winning_scope, chain=chain)

        return RuleOutcome(rule=winning, scope=winning_scope, chain=chain)

    @staticmethod
    def _scope_matches(rule: dict[str, Any], scope: str, context: dict[str, Any]) -> bool:
        if rule.get("scope_type") != scope:
            return False
        if scope == "global":
            return True
        scope_ref = rule.get("scope_ref")
        if not scope_ref:
            return False
        ctx_sector = context.get("sector")
        return str(ctx_sector) == str(scope_ref)

    @staticmethod
    def _service_constraint_skip(rule: dict[str, Any], context: dict[str, Any]) -> str | None:
        constraint = rule.get("match_in_service")
        if constraint is None:
            return None
        if isinstance(constraint, int):
            constraint = bool(constraint)
        in_service = context.get("in_service")
        if in_service is None:
            return "vincolo orario ma cliente non identificato/configurato"
        if constraint and not in_service:
            return "regola richiede in_service=true ma cliente fuori orario"
        if not constraint and in_service:
            return "regola richiede in_service=false ma cliente in orario"
        return None

    @staticmethod
    def _rule_matches(rule: dict[str, Any], event: dict[str, Any]) -> dict[str, Any]:
        reasons: list[str] = []

        regex_tests = [
            ("match_from_regex", event.get("from_address") or ""),
            ("match_to_regex", event.get("to_address") or ""),
            ("match_subject_regex", event.get("subject") or ""),
            ("match_body_regex", event.get("body_text") or ""),
        ]
        for fld, haystack in regex_tests:
            pattern = rule.get(fld)
            if not pattern:
                continue
            try:
                m = _safe_search(pattern, haystack)
            except re.error as exc:
                reasons.append(f"{fld}: REGEX INVALIDA ({exc})")
                return {"matched": False, "reasons": reasons}
            if m:
                reasons.append(f"{fld}: match")
            else:
                reasons.append(f"{fld}: no match")
                return {"matched": False, "reasons": reasons}

        to_domain = rule.get("match_to_domain")
        if to_domain:
            event_to_domain = (event.get("to_domain") or "").lower()
            if event_to_domain == to_domain.lower():
                reasons.append(f"match_to_domain '{to_domain}': match")
            else:
                reasons.append(f"match_to_domain '{to_domain}': no (got '{event_to_domain}')")
                return {"matched": False, "reasons": reasons}

        from_domain = rule.get("match_from_domain")
        if from_domain:
            from_addr = (event.get("from_address") or "").lower()
            event_from_domain = from_addr.rsplit("@", 1)[-1] if "@" in from_addr else ""
            if event_from_domain == from_domain.lower():
                reasons.append(f"match_from_domain '{from_domain}': match")
            else:
                reasons.append(f"match_from_domain '{from_domain}': no (got '{event_from_domain}')")
                return {"matched": False, "reasons": reasons}

        constraint = rule.get("match_in_service")
        if constraint is not None:
            reasons.append(f"match_in_service={bool(constraint)}: ok")

        # Tristate matchers su contesto cliente
        for fld, ctx_key, label in (
            ("match_contract_active", "contract_active", "match_contract_active"),
            ("match_known_customer", "known_customer", "match_known_customer"),
            ("match_has_exception_today", "has_exception_today", "match_has_exception_today"),
        ):
            want = rule.get(fld)
            if want is None:
                continue
            want_b = bool(want)
            got = event.get(ctx_key)
            if got is None:
                reasons.append(f"{label}={want_b}: skip (contesto non disponibile)")
                return {"matched": False, "reasons": reasons}
            if bool(got) != want_b:
                reasons.append(f"{label}={want_b}: no (got {bool(got)})")
                return {"matched": False, "reasons": reasons}
            reasons.append(f"{label}={want_b}: ok")

        # Match tag: equality string (case-insensitive) sul campo `tag` dell'evento.
        # Origine tag: header X-Domarc-Tag, classificazione IA precedente, o estensione
        # custom. Se la regola ha match_tag valorizzato e l'evento NON ha quel tag, no match.
        rule_tag = (rule.get("match_tag") or "").strip()
        if rule_tag:
            event_tag = (event.get("tag") or "").strip()
            if event_tag.lower() != rule_tag.lower():
                reasons.append(f"match_tag '{rule_tag}': no (got '{event_tag or '∅'}')")
                return {"matched": False, "reasons": reasons}
            reasons.append(f"match_tag '{rule_tag}': match")

        # Match recipient group: la regola scatta se uno dei destinatari
        # (To primario o gli altri in to_addresses) è membro del gruppo.
        # Migration 027. Alternativa esclusiva a match_to_regex.
        to_group_id = rule.get("match_to_group_id")
        if to_group_id:
            recipient_groups = event.get("recipient_groups") or {}
            target_email = (event.get("to_address") or "").lower()
            also_check = [a.lower() for a in (event.get("to_addresses") or [])]
            ids_for_email: set[int] = set()
            for em in [target_email, *also_check]:
                if not em:
                    continue
                ids_for_email.update(int(x) for x in recipient_groups.get(em, []))
            if int(to_group_id) not in ids_for_email:
                reasons.append(
                    f"match_to_group_id={to_group_id}: nessun destinatario nel gruppo"
                )
                return {"matched": False, "reasons": reasons}
            reasons.append(f"match_to_group_id={to_group_id}: match")

        # Match customer groups (CSV "top,sanita" → cliente deve appartenere ad
        # almeno uno dei gruppi listati). Se il cliente non è risolto (codcli=None)
        # o non ha gruppi, la regola NON matcha.
        groups_csv = rule.get("match_customer_groups")
        if groups_csv:
            wanted = {g.strip() for g in str(groups_csv).split(",") if g.strip()}
            if not wanted:
                pass  # CSV malformato, ignora
            else:
                cust_groups = set(event.get("customer_groups") or [])
                if not cust_groups:
                    reasons.append(
                        f"match_customer_groups {sorted(wanted)}: no (cliente "
                        f"non in alcun gruppo)"
                    )
                    return {"matched": False, "reasons": reasons}
                inter = wanted & cust_groups
                if not inter:
                    reasons.append(
                        f"match_customer_groups {sorted(wanted)}: no (cliente "
                        f"in {sorted(cust_groups)})"
                    )
                    return {"matched": False, "reasons": reasons}
                reasons.append(
                    f"match_customer_groups: match via {sorted(inter)}"
                )

        if not reasons:
            reasons.append("catch-all (nessun criterio valorizzato)")
        return {"matched": True, "reasons": reasons}

    def validate_regex_fields(self, rule: dict[str, Any]) -> list[str]:
        errors: list[str] = []
        for fld in ("match_from_regex", "match_subject_regex", "match_body_regex"):
            value = rule.get(fld)
            if not value:
                continue
            if len(value) > _REGEX_MAX_LEN:
                errors.append(f"{fld}: regex troppo lunga (max {_REGEX_MAX_LEN})")
                continue
            try:
                re.compile(value)
            except re.error as exc:
                errors.append(f"{fld}: regex invalida ({exc})")
                continue
            if _looks_redos(value):
                errors.append(f"{fld}: regex sospetta di backtracking esponenziale")
        return errors
