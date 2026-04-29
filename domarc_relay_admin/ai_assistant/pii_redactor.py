"""PII redactor — pipeline GDPR-friendly per il pre-processing prima dell'IA.

Tre stadi:

1. **Regex deterministici** per pattern fissi: IBAN, CF, P.IVA, telefono,
   email, URL con token, indirizzi IP.
2. **NER spaCy** italiano (``it_core_news_sm``) per entità di persona
   (PER), organizzazione (ORG), luogo (LOC). Caricato lazy alla prima
   chiamata.
3. **Dizionario custom** (``ai_pii_dictionary``) per pattern specifici
   appresi nel tempo (nomi ricorrenti, prodotti aziendali interni).

Restituisce ``RedactionResult`` con il testo redacted + lista delle
sostituzioni effettuate (per audit, conteggio in ``ai_decisions.pii_redactions_count``).

Il modello spaCy va scaricato a setup con::

    /opt/domarc-smtp-relay-admin/.venv/bin/python -m spacy download it_core_news_sm

Se il modello non è disponibile, lo step NER viene saltato e si registra un
warning. La redazione regex resta sempre attiva (zero dipendenze esterne).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..storage.base import Storage

logger = logging.getLogger(__name__)

# =============================================================
# Regex deterministici (italiano + universali)
# =============================================================

_PATTERNS: list[tuple[str, str, re.Pattern]] = [
    # IBAN italiano (IT + 25 caratteri alphanumerici)
    ("iban", "[IBAN]", re.compile(r"\bIT\d{2}[A-Z]\d{10}[A-Z0-9]{12}\b", re.IGNORECASE)),
    # Codice fiscale italiano (16 caratteri canonici)
    ("cf", "[CF]", re.compile(r"\b[A-Z]{6}\d{2}[A-Z]\d{2}[A-Z]\d{3}[A-Z]\b", re.IGNORECASE)),
    # Partita IVA italiana (11 cifre, opzionale prefix IT)
    ("piva", "[PIVA]", re.compile(r"\b(?:IT)?\s?\d{11}\b")),
    # Telefono italiano (con prefisso internazionale o numero locale)
    ("phone", "[TELEFONO]", re.compile(
        r"\b(?:\+39\s?)?(?:0\d{1,3}[\s./-]?\d{5,8}|3\d{2}[\s./-]?\d{6,7})\b"
    )),
    # Email (qualsiasi)
    ("email", "[EMAIL]", re.compile(
        r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"
    )),
    # URL con token o credenziali sensibili (basic, evita di trasmettere segreti)
    ("url_token", "[URL_TOKEN]", re.compile(
        r"https?://[^\s]*(?:token|api_key|apikey|password|secret)=[^\s&]*",
        re.IGNORECASE,
    )),
    # IPv4
    ("ipv4", "[IP]", re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")),
]

# Marcatori di firma (italiano + inglese): rimuoviamo da questi marker fino a fine testo
# Pattern specifici (parole chiave) + il marker speciale "--" su riga propria
# (convenzione storica delle firme email).
_SIGNATURE_MARKERS = re.compile(
    r"(?im)^\s*(?:"
    r"cordialmente|distinti\s+saluti|cordiali\s+saluti|grazie\s+e\s+saluti|"
    r"un\s+cordiale\s+saluto|kind\s+regards|best\s+regards|sincerely|"
    r"inviato\s+da|sent\s+from"
    r")\b.*",
)
_SIGNATURE_DASH_MARKER = re.compile(r"(?m)^--\s*$")


@dataclass
class Redaction:
    kind: str           # "iban", "cf", "person", "org", ecc.
    original: str       # valore originale (NON salvato in audit, solo per debug locale)
    replacement: str    # valore sostitutivo


@dataclass
class RedactionResult:
    text: str                                       # testo redacted
    redactions: list[Redaction] = field(default_factory=list)
    spacy_used: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.redactions)

    def kinds_summary(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for r in self.redactions:
            out[r.kind] = out.get(r.kind, 0) + 1
        return out


# =============================================================
# spaCy NER (lazy load)
# =============================================================

_nlp_cache: object | None = None
_nlp_loaded: bool = False


def _load_spacy():
    global _nlp_cache, _nlp_loaded
    if _nlp_loaded:
        return _nlp_cache
    _nlp_loaded = True
    try:
        import spacy  # type: ignore[import-not-found]
        _nlp_cache = spacy.load("it_core_news_sm",
                                disable=["parser", "lemmatizer", "tagger"])
        logger.info("PII redactor: modello spaCy 'it_core_news_sm' caricato")
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "PII redactor: spaCy non disponibile (%s). Step NER saltato. "
            "Per attivarlo: pip install spacy && python -m spacy download it_core_news_sm",
            exc,
        )
        _nlp_cache = None
    return _nlp_cache


# =============================================================
# Pipeline pubblica
# =============================================================

def redact(
    text: str,
    *,
    use_spacy: bool = True,
    custom_dictionary: list[dict] | None = None,
    strip_signature: bool = True,
) -> RedactionResult:
    """Applica i 3 stadi di redazione e restituisce il risultato.

    Args:
        text: testo da redarre (subject + body concatenati o separati).
        use_spacy: se False, salta lo step NER (più veloce).
        custom_dictionary: lista di dict ``{kind, value, replacement}`` da
            ``ai_pii_dictionary``. Se None, dizionario non applicato.
        strip_signature: se True, rimuove tutto da ``Cordialmente`` in poi.
    """
    if not text:
        return RedactionResult(text="")

    redactions: list[Redaction] = []
    notes: list[str] = []
    spacy_used = False

    # Stadio 0: rimozione firma — cerca il primo dei due marker (parole chiave o `--`)
    if strip_signature:
        m_kw = _SIGNATURE_MARKERS.search(text)
        m_dash = _SIGNATURE_DASH_MARKER.search(text)
        candidates = [c for c in (m_kw, m_dash) if c is not None]
        m = min(candidates, key=lambda x: x.start()) if candidates else None
        if m:
            sig_start = m.start()
            removed = text[sig_start:]
            redactions.append(Redaction(kind="signature", original=removed[:80],
                                        replacement="[FIRMA_RIMOSSA]"))
            text = text[:sig_start].rstrip() + "\n[FIRMA_RIMOSSA]"

    # Stadio 1: pattern regex deterministici
    for kind, replacement, pattern in _PATTERNS:
        def _sub(match: re.Match) -> str:
            redactions.append(Redaction(kind=kind, original=match.group(0),
                                        replacement=replacement))
            return replacement
        text = pattern.sub(_sub, text)

    # Stadio 2: spaCy NER (PER, ORG, LOC)
    if use_spacy:
        nlp = _load_spacy()
        if nlp is not None:
            spacy_used = True
            try:
                doc = nlp(text)
                # Sostituisci dall'ultima entità alla prima (per non shiftare gli indici)
                ents = sorted(
                    [e for e in doc.ents if e.label_ in ("PER", "ORG", "LOC")],
                    key=lambda e: e.start_char, reverse=True,
                )
                kind_counts: dict[str, int] = {}
                for ent in ents:
                    label = ent.label_
                    kind_counts[label] = kind_counts.get(label, 0) + 1
                    replacement = f"[{label}_{kind_counts[label]}]"
                    original = ent.text
                    redactions.append(Redaction(kind=label.lower(),
                                                original=original,
                                                replacement=replacement))
                    text = text[:ent.start_char] + replacement + text[ent.end_char:]
            except Exception as exc:  # noqa: BLE001
                notes.append(f"spaCy NER fallito: {exc}")

    # Stadio 3: dizionario custom (case-insensitive)
    if custom_dictionary:
        for entry in custom_dictionary:
            value = (entry.get("value") or "").strip()
            replacement = entry.get("replacement") or "[REDACTED]"
            kind = entry.get("kind") or "custom"
            if not value or len(value) < 2:
                continue
            # I word-boundary `\b` non funzionano se il valore inizia/finisce con
            # caratteri non-word (es. "Acme S.p.A." finisce con `.`). Adattiamo:
            # `\b` solo dove i char ai bordi sono word chars; altrimenti lookahead
            # soft (?=\W|$) / lookbehind (?<=\W|^).
            left = r"\b" if value[0].isalnum() or value[0] == "_" else r"(?<=\W|^)"
            right = r"\b" if value[-1].isalnum() or value[-1] == "_" else r"(?=\W|$)"
            pattern = re.compile(left + re.escape(value) + right, re.IGNORECASE)
            def _sub_dict(match: re.Match,
                          _kind=kind, _repl=replacement) -> str:
                redactions.append(Redaction(kind=_kind,
                                            original=match.group(0),
                                            replacement=_repl))
                return _repl
            text = pattern.sub(_sub_dict, text)

    return RedactionResult(text=text, redactions=redactions,
                           spacy_used=spacy_used, notes=notes)


def redact_event(event: dict, *, storage: "Storage | None" = None,
                 tenant_id: int = 1) -> tuple[dict, RedactionResult]:
    """Wrapper convenience: redarre subject + body di un evento mail.

    Restituisce ``(event_redacted, RedactionResult)`` dove ``event_redacted``
    è un dict con stesse chiavi ma `subject`/`body_text` sostituiti.
    """
    custom_dict: list[dict] | None = None
    if storage is not None:
        try:
            custom_dict = storage.list_ai_pii_dictionary(tenant_id=tenant_id)
        except (AttributeError, NotImplementedError):
            custom_dict = None
    combined = "\n\n".join(filter(None, [
        event.get("subject") or "",
        event.get("body_text") or "",
    ]))
    result = redact(combined, custom_dictionary=custom_dict)
    redacted = dict(event)
    # Splitta back: il primo blocco è subject (1 riga max ~200 char), il resto body
    parts = result.text.split("\n\n", 1)
    redacted["subject"] = parts[0] if parts else ""
    redacted["body_text"] = parts[1] if len(parts) > 1 else ""
    # Email mittente: redacted (mostriamo solo il dominio)
    if event.get("from_address"):
        domain = event["from_address"].rpartition("@")[2]
        redacted["from_address"] = f"[FROM_USER]@{domain}" if domain else "[FROM]"
    return redacted, result
