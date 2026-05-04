"""H24 — estrazione codice autorizzazione dal subject mail.

Liberale in estrazione, conservativi in validazione (DB).
- Priorità: token con prefisso AUTH- o H24- (caso d'uso esplicito).
- Fallback: token alfanumerici uppercase con almeno una lettera E una cifra,
  per evitare di matchare parole gergali (URGENTE, SERVER, CRITICO).

Regex tunabile via setting `h24.subject_extract_regex` se serve un pattern
custom; il default qui è safe per non bloccare il flusso se il setting è
vuoto o malformato.
"""
from __future__ import annotations

import re

# Token maiuscolo: inizia/finisce con [A-Z0-9], può contenere trattini interni,
# lunghezza 6-40 caratteri. Cattura sia "AUTH-ABC123" sia "DOMARC-ACME-H24"
# sia codici puri tipo "ABC123XY".
_DEFAULT_TOKEN_RE = re.compile(
    r"\b[A-Z0-9][A-Z0-9-]{4,38}[A-Z0-9]\b",
    re.IGNORECASE,
)

# Prefissi a priorità alta: se trovati, ritornati immediatamente.
PRIORITY_PREFIXES = ("AUTH-", "H24-")


def extract_auth_code(subject: str | None,
                       custom_regex: str | None = None) -> str | None:
    """Estrae il primo codice autorizzazione plausibile dal subject.

    Args:
        subject: il subject della mail (può essere None/vuoto).
        custom_regex: opzionale, regex custom da `h24.subject_extract_regex`.
            Se vuoto/None usa il default. Se invalido, fallback al default.

    Returns:
        Il codice estratto (uppercase, senza spazi) oppure None se non
        trovato un candidato plausibile.

    Strategia:
        1. Compila la regex (custom o default), in caso di errore usa default.
        2. Trova tutti i token che matchano nel subject.
        3. Se uno ha prefisso AUTH- o H24- → ritorna quello (più alta confidenza).
        4. Altrimenti ritorna il primo token che ha sia lettere che cifre
           (filtra parole tipo URGENTE, SERVER).
        5. Se nessuno → None.

    Esempi:
        >>> extract_auth_code("[OptiWize] - DEV01 | AUTH-ABC234 - urgente")
        'AUTH-ABC234'
        >>> extract_auth_code("DOMARC-ACME-H24 - server giù")
        'DOMARC-ACME-H24'
        >>> extract_auth_code("URGENTE assistenza richiesta")  # no cifre
        >>> extract_auth_code("Codice K7M9PX")
        'K7M9PX'
        >>> extract_auth_code(None)
    """
    if not subject:
        return None
    pattern: re.Pattern[str]
    if custom_regex:
        try:
            pattern = re.compile(custom_regex, re.IGNORECASE)
        except re.error:
            pattern = _DEFAULT_TOKEN_RE
    else:
        pattern = _DEFAULT_TOKEN_RE

    candidates = [m.group(0).upper() for m in pattern.finditer(subject)]
    if not candidates:
        return None

    # 1) Prima passa: priorità a prefissi noti.
    for c in candidates:
        for p in PRIORITY_PREFIXES:
            if c.startswith(p):
                return c

    # 2) Seconda passa: token con trattini multi-segmento (≥2 trattini),
    #    tipici dei codici permanenti strutturati tipo DOMARC-ACME-H24.
    #    Almeno 2 trattini per evitare di pescare "RICHIESTA-URGENTE".
    for c in candidates:
        if c.count("-") >= 2:
            return c

    # 3) Terza passa: token misti lettere+cifre (filtra parole comuni
    #    tutto-maiuscolo come URGENTE, SERVER).
    for c in candidates:
        if any(ch.isdigit() for ch in c) and any(ch.isalpha() for ch in c):
            return c

    return None


# ============================================================================
# Test rapidi (eseguibili come script: python3 -m domarc_relay_admin.h24_code_extractor)
# ============================================================================

if __name__ == "__main__":
    cases = [
        # (subject, expected)
        (None, None),
        ("", None),
        ("nessun codice qui", None),
        ("URGENTE assistenza", None),  # parola maiuscola senza cifre
        ("[OptiWize] - DEV01 | AUTH-ABC234 - aiuto", "AUTH-ABC234"),
        ("DOMARC-ACME-H24 - server giu'", "DOMARC-ACME-H24"),
        ("Codice K7M9PX nel subject", "K7M9PX"),
        ("auth-xyz123 lowercase", "AUTH-XYZ123"),  # case-insensitive
        ("H24-ABCDEF12 codice permanente", "H24-ABCDEF12"),
        # Priorità prefisso vince anche se token più lungo viene prima
        ("MEGA1234567 e poi AUTH-PR1234", "AUTH-PR1234"),
        # Edge: solo lettere maiuscole senza cifre
        ("RICHIESTA URGENTE SERVER GIU", None),
        # Edge: caratteri speciali extra
        ("Re: AUTH-ZZZ789 (FWD)", "AUTH-ZZZ789"),
        # Codice corto < 6 caratteri non matcha
        ("AB12 troppo corto", None),
        # Codice esattamente 6 alfanumerici → match
        ("AB12CD nel mezzo", "AB12CD"),
        # Codice permanente tutte lettere con multi-trattini (struttura)
        ("DOMARC-ACME-H24 server giu", "DOMARC-ACME-H24"),
        ("DOMARC-TEST-PERM aiuto", "DOMARC-TEST-PERM"),
        # Singolo trattino senza prefisso noto → fallback su lettere+cifre
        ("BIG-WORD ma anche AB12CD", "AB12CD"),
        # Singolo trattino con cifre → match (fallback passa 3)
        ("CODE-12345 nel testo", "CODE-12345"),
        # Multi-trattino tipo "MULTI-PAROLA-MAIUSC" senza cifre → match (passa 2)
        ("RICHIESTA URGENTE-AGGRAVATA-CRITICO", "URGENTE-AGGRAVATA-CRITICO"),
    ]
    failed = 0
    for subj, expected in cases:
        got = extract_auth_code(subj)
        ok = got == expected
        if not ok:
            failed += 1
        status = "PASS" if ok else "FAIL"
        print(f"  [{status}] {subj!r:60s} -> {got!r:25s} (expected {expected!r})")
    print(f"\n{len(cases) - failed}/{len(cases)} test PASS, {failed} FAIL")
