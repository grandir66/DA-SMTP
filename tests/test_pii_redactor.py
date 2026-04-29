"""Test PII redactor: regex deterministici + signature stripping + dictionary."""
from __future__ import annotations

import pytest

from domarc_relay_admin.ai_assistant.pii_redactor import redact, RedactionResult


def _kinds(result: RedactionResult) -> set[str]:
    return {r.kind for r in result.redactions}


# ============================================ REGEX DETERMINISTICI ===

def test_redact_iban_italiano():
    text = "Bonifico su IBAN IT60X0542811101000000123456 per conferma."
    r = redact(text, use_spacy=False, strip_signature=False)
    assert "iban" in _kinds(r)
    assert "[IBAN]" in r.text
    assert "IT60" not in r.text


def test_redact_codice_fiscale():
    text = "Il CF e' RSSMRA80A01H501Z grazie."
    r = redact(text, use_spacy=False, strip_signature=False)
    assert "cf" in _kinds(r)
    assert "[CF]" in r.text


def test_redact_partita_iva():
    text = "P.IVA 12345678901 — Acme Srl"
    r = redact(text, use_spacy=False, strip_signature=False)
    assert "piva" in _kinds(r)
    assert "12345678901" not in r.text


def test_redact_telefono_internazionale():
    text = "Chiamami al +39 333 1234567 quando puoi."
    r = redact(text, use_spacy=False, strip_signature=False)
    assert "phone" in _kinds(r)
    assert "[TELEFONO]" in r.text


def test_redact_telefono_locale():
    text = "Centralino 02 12345678."
    r = redact(text, use_spacy=False, strip_signature=False)
    assert "phone" in _kinds(r)


def test_redact_email():
    text = "Contattami su mario.rossi@example.com per dettagli."
    r = redact(text, use_spacy=False, strip_signature=False)
    assert "email" in _kinds(r)
    assert "mario.rossi" not in r.text


def test_redact_ipv4():
    text = "Server 192.168.4.42 non risponde."
    r = redact(text, use_spacy=False, strip_signature=False)
    assert "ipv4" in _kinds(r)


def test_redact_url_with_token():
    text = "Vedi https://api.example.com/data?token=abc123secret"
    r = redact(text, use_spacy=False, strip_signature=False)
    assert "url_token" in _kinds(r)


# ============================================ SIGNATURE STRIPPING ===

def test_signature_stripping_cordialmente():
    text = "Buongiorno, vediamo il problema.\n\nCordialmente,\nMario Rossi\nAcme S.p.A."
    r = redact(text, use_spacy=False, strip_signature=True)
    assert "signature" in _kinds(r)
    assert "Mario Rossi" not in r.text
    assert "[FIRMA_RIMOSSA]" in r.text


def test_signature_stripping_distinti_saluti():
    text = "Buongiorno, vediamo.\n\nDistinti saluti\nLuca Bianchi"
    r = redact(text, use_spacy=False, strip_signature=True)
    assert "signature" in _kinds(r)


def test_signature_stripping_dash_marker():
    text = "Salve, info richiesta.\n\n--\nGianni Verdi\nCEO"
    r = redact(text, use_spacy=False, strip_signature=True)
    assert "signature" in _kinds(r)


def test_signature_stripping_disabled():
    text = "Buongiorno.\n\nCordialmente,\nMario Rossi"
    r = redact(text, use_spacy=False, strip_signature=False)
    assert "signature" not in _kinds(r)


# ============================================ DICTIONARY CUSTOM ===

def test_custom_dictionary_matches_case_insensitive():
    text = "Il referente di Acme S.p.A. e' tale Mario Rossi."
    custom = [
        {"kind": "org", "value": "Acme S.p.A.", "replacement": "[ACME]"},
    ]
    r = redact(text, use_spacy=False, custom_dictionary=custom)
    assert "[ACME]" in r.text
    assert "Acme" not in r.text


def test_custom_dictionary_skips_short_values():
    """Valori < 2 char vengono ignorati per evitare falsi positivi."""
    text = "il "
    custom = [{"kind": "other", "value": "i", "replacement": "X"}]
    r = redact(text, use_spacy=False, custom_dictionary=custom)
    assert r.text == "il "  # invariato


# ============================================ EMPTY / EDGE CASES ===

def test_empty_text():
    r = redact("", use_spacy=False)
    assert r.text == ""
    assert r.count == 0


def test_no_pii_no_redactions():
    text = "Buongiorno, una richiesta generica senza dati sensibili."
    r = redact(text, use_spacy=False, strip_signature=False)
    assert r.count == 0


def test_multiple_pii_combined():
    text = (
        "Bonifico IBAN IT60X0542811101000000123456 e P.IVA 12345678901. "
        "Contatto +39 333 1234567 oppure mario@x.com. Server 192.168.4.42."
    )
    r = redact(text, use_spacy=False, strip_signature=False)
    kinds = _kinds(r)
    assert "iban" in kinds
    assert "piva" in kinds
    assert "phone" in kinds
    assert "email" in kinds
    assert "ipv4" in kinds
    assert r.count >= 5


def test_redaction_result_kinds_summary():
    text = "IBAN IT60X0542811101000000123456 e mail mario@x.com"
    r = redact(text, use_spacy=False, strip_signature=False)
    summary = r.kinds_summary()
    assert summary.get("iban") == 1
    assert summary.get("email") == 1
