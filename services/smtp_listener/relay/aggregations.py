"""Logica di aggregazione errori ripetuti.

Per ogni mail entrante, valuta le `aggregations_cache`: se una aggregation matcha,
calcola la fingerprint, aggiorna il counter, decide se aprire ticket.

Concetti:
- `match`: la mail soddisfa i criteri regex della aggregation (AND di from/subject/body)
- `reset_match`: la mail (di solito una notifica di "recovered/online/ok") matcha i
  reset_*_regex → azzera il counter
- `fingerprint`: stringa che identifica "stesso errore" (default `${from}|${subject_normalized}`)
- `subject_normalized`: subject con cifre, IP, UUID, hash sostituiti per ignorare
  parti variabili (timestamp, ticket-id, ecc.)
"""
from __future__ import annotations

import logging
import re
import threading
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

# Pattern di normalizzazione: rimuovono parti variabili dal subject per ottenere fingerprint stabile
_NORM_PATTERNS = [
    (re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b"), "<IP>"),
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I), "<UUID>"),
    (re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2})?(\+\d{2}:?\d{2}|Z)?\b"), "<DATETIME>"),
    (re.compile(r"\b\d{2}/\d{2}/\d{2,4}\b"), "<DATE>"),
    (re.compile(r"\b\d{2}:\d{2}(:\d{2})?\b"), "<TIME>"),
    (re.compile(r"#\d{3,}"), "#<NUM>"),
    (re.compile(r"\[\d+\]"), "[<NUM>]"),
    (re.compile(r"\b\d{4,}\b"), "<NUM>"),  # numeri lunghi (timestamp epoch, port, ecc.)
]


def _normalize_subject(subject: str) -> str:
    """Riduce il subject a una forma stabile sostituendo timestamp/IP/UUID/numeri."""
    if not subject:
        return ""
    s = subject.strip()
    for pattern, repl in _NORM_PATTERNS:
        s = pattern.sub(repl, s)
    s = re.sub(r"\s+", " ", s)
    return s.lower()[:200]


_REGEX_HAYSTACK_MAX = 16 * 1024
_REGEX_TIMEOUT_SEC = 0.5


def _safe_search(pattern: str | None, haystack: str | None) -> re.Match[str] | None:
    """Esecuzione regex con timeout (anti-ReDoS) — stesso pattern di rules._safe_search.
    Un thread daemon viene join()-ato a 0.5s. Se ancora alive → match=None + log.
    """
    if not pattern or haystack is None:
        return None
    text = haystack[:_REGEX_HAYSTACK_MAX] if haystack else ""
    box: list[Any] = [None, None]

    def _run() -> None:
        try:
            box[0] = re.search(pattern, text, re.IGNORECASE)
        except re.error as exc:
            box[1] = exc
        except Exception as exc:  # noqa: BLE001
            box[1] = exc

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(_REGEX_TIMEOUT_SEC)
    if t.is_alive():
        logger.warning("Regex aggregation timeout (%.2fs) pattern=%r",
                        _REGEX_TIMEOUT_SEC, pattern[:80])
        return None
    if isinstance(box[1], re.error):
        logger.warning("Regex aggregation invalida '%s': %s", pattern, box[1])
        return None
    if box[1] is not None:
        logger.warning("Regex aggregation exception '%s': %s", pattern, box[1])
        return None
    return box[0]


def aggregation_matches(agg: Any, parsed: Any) -> bool:
    """True se la mail soddisfa i criteri regex della aggregation (AND di valorizzati).

    Almeno uno tra subject/from/body regex deve essere valorizzato (no catch-all).
    """
    has_any = False
    if agg["match_from_regex"]:
        has_any = True
        if not _safe_search(agg["match_from_regex"], parsed.from_address):
            return False
    if agg["match_subject_regex"]:
        has_any = True
        if not _safe_search(agg["match_subject_regex"], parsed.subject):
            return False
    if agg["match_body_regex"]:
        has_any = True
        if not _safe_search(agg["match_body_regex"], parsed.body_text or ""):
            return False
    return has_any


def is_reset_match(agg: Any, parsed: Any) -> bool:
    """True se la mail matcha i reset_*_regex configurati (azzeramento counter)."""
    has_reset_rule = bool(agg["reset_subject_regex"] or agg["reset_from_regex"])
    if not has_reset_rule:
        return False
    subj_match = True
    from_match = True
    if agg["reset_subject_regex"]:
        subj_match = _safe_search(agg["reset_subject_regex"], parsed.subject) is not None
    if agg["reset_from_regex"]:
        from_match = _safe_search(agg["reset_from_regex"], parsed.from_address) is not None
    return subj_match and from_match


def compute_fingerprint(template: str, parsed: Any, subject_match: re.Match[str] | None = None) -> str:
    """Calcola la fingerprint sostituendo le variabili nel template.

    Variabili supportate:
    - ${from}              indirizzo mittente (lowercase)
    - ${from_domain}       solo dominio
    - ${subject}           oggetto raw (lowercase, troncato 120)
    - ${subject_normalized} oggetto normalizzato (preferito per stabilità)
    - ${capture:N}         gruppo N catturato dalla match_subject_regex (1-based)
    """
    out = template
    out = out.replace("${from}", (parsed.from_address or "").lower())
    out = out.replace("${from_domain}", (parsed.from_domain or "").lower())
    out = out.replace("${subject}", (parsed.subject or "").lower()[:120])
    out = out.replace("${subject_normalized}", _normalize_subject(parsed.subject or ""))
    # ${capture:N}: sostituisci con il gruppo cattura della match_subject_regex
    def _replace_capture(match: re.Match[str]) -> str:
        try:
            idx = int(match.group(1))
            if subject_match is not None:
                try:
                    return (subject_match.group(idx) or "").strip()
                except IndexError:
                    return ""
        except (ValueError, AttributeError):
            pass
        return ""
    out = re.sub(r"\$\{capture:(\d+)\}", _replace_capture, out)
    # Truncate per stare sotto VARCHAR(512)
    return out[:500]


def is_outside_window(first_seen_iso: str, window_hours: int) -> bool:
    """True se la prima occurrence è oltre la finestra → counter va resettato a 1 con nuovo first_seen."""
    if not first_seen_iso:
        return False
    try:
        # ISO with TZ
        first = datetime.fromisoformat(first_seen_iso)
        if first.tzinfo is None:
            first = first.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return False
    now = datetime.now(timezone.utc)
    elapsed_hours = (now - first).total_seconds() / 3600.0
    return elapsed_hours > window_hours


def should_open_ticket(
    *,
    new_count: int,
    threshold: int,
    consecutive_only: bool,
    was_reset: bool,
    was_window_expired: bool,
    ticket_already_opened: bool,
) -> bool:
    """Decide se la occurrence corrente deve generare un ticket.

    Logica:
    - Se era reset → no (counter azzerato, niente ticket finché non si raggiunge di nuovo soglia)
    - Se ticket già aperto → no (evita duplicati)
    - Se la window è appena scaduta → counter ricomincia da 1, no ticket
    - Altrimenti soglia raggiunta → sì
    """
    if was_reset:
        return False
    if ticket_already_opened:
        return False
    if was_window_expired:
        return False
    return new_count >= threshold
