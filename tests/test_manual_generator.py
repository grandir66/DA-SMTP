"""Test del generatore manuale automatico (manual_generator).

Verifica funzioni pure che non richiedono Flask app context:
- ``_list_migrations()`` parsa correttamente i file SQL on-disk.
- ``_supported_actions()`` ritorna catalogo non vuoto.
- ``_validators_summary()`` ritorna catalogo non vuoto.
- ``read_manual()`` con path assente ritorna placeholder.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from domarc_relay_admin import manual_generator as mg


def test_list_migrations_returns_sorted():
    migs = mg._list_migrations()
    assert len(migs) > 0, "deve esserci almeno una migration"
    versions = [m["version"] for m in migs]
    assert versions == sorted(versions), "ordinate per version asc"


def test_list_migrations_each_has_keys():
    migs = mg._list_migrations()
    for m in migs:
        assert "version" in m and isinstance(m["version"], int)
        assert "filename" in m and m["filename"].endswith(".sqlite.sql")
        assert "description" in m
        assert "tables" in m and isinstance(m["tables"], list)


def test_supported_actions_non_empty():
    actions = mg._supported_actions()
    assert len(actions) > 0
    codes = {a["code"] for a in actions}
    # Action storicamente presenti — se cambia il catalogo va aggiornato il manual
    assert "ignore" in codes or "drop" in codes or "ai_classify" in codes


def test_validators_summary_non_empty():
    vals = mg._validators_summary()
    assert len(vals) > 0
    ids = {v.get("id", "") for v in vals}
    # V001..V008 + W001+ devono comparire
    assert "V001" in ids
    assert any(i.startswith("W") for i in ids)


def test_read_manual_placeholder_when_missing(monkeypatch, tmp_path):
    fake_path = tmp_path / "nope.md"
    monkeypatch.setattr(mg, "MANUAL_PATH", fake_path)
    text = mg.read_manual()
    assert "Il manuale non è stato ancora generato" in text


def test_read_manual_returns_content_when_present(monkeypatch, tmp_path):
    fake_path = tmp_path / "m.md"
    fake_path.write_text("# Test\n\nContenuto di prova.\n", encoding="utf-8")
    monkeypatch.setattr(mg, "MANUAL_PATH", fake_path)
    assert "Contenuto di prova" in mg.read_manual()


def test_read_version_returns_string():
    v = mg._read_version()
    assert isinstance(v, str)
    assert len(v) > 0
