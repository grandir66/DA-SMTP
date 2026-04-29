"""Test del module_manager (gestione moduli Python installabili da UI).

Coperture:
- ``MODULE_CATALOG`` ha campi obbligatori per ogni entry.
- ``list_modules_status()`` ritorna 1 dict per entry con flag installed.
- ``_is_installed()`` riconosce stdlib + segnala assenti.
- ``install_module(code)`` con codice fuori whitelist fallisce safe.
- ``uninstall_module(code)`` idem (no subprocess invocato).
"""
from __future__ import annotations

import pytest

from domarc_relay_admin import module_manager as mm


def test_module_catalog_required_fields():
    assert len(mm.MODULE_CATALOG) > 0
    required = {"code", "package", "import_check", "description"}
    for entry in mm.MODULE_CATALOG:
        missing = required - entry.keys()
        assert not missing, f"entry {entry.get('code')} manca: {missing}"
        assert isinstance(entry["code"], str) and entry["code"]
        assert isinstance(entry["package"], str)


def test_module_catalog_unique_codes():
    codes = [e["code"] for e in mm.MODULE_CATALOG]
    assert len(codes) == len(set(codes)), "code duplicati nel catalogo"


def test_is_installed_true_for_stdlib():
    """`json` è sempre presente — verifica che _is_installed lo riconosca."""
    installed, version = mm._is_installed("json")
    assert installed is True
    assert version is not None


def test_is_installed_false_for_missing():
    installed, version = mm._is_installed("modulo_che_non_esiste_2026")
    assert installed is False
    assert version is None


def test_list_modules_status_matches_catalog_size():
    status = mm.list_modules_status()
    assert len(status) == len(mm.MODULE_CATALOG)
    for s in status:
        assert "installed" in s
        assert "depends_satisfied" in s
        assert isinstance(s["installed"], bool)


def test_install_module_unknown_code_fails(monkeypatch):
    """Installazione di codice fuori whitelist NON deve invocare subprocess."""
    called = {"flag": False}

    def fake_run(*args, **kw):
        called["flag"] = True
        raise AssertionError("subprocess non deve essere invocato")

    monkeypatch.setattr(mm.subprocess, "run", fake_run)
    res = mm.install_module(
        "evil_package; rm -rf /",
        storage=_DummyStorage(),
        actor="test",
    )
    assert res["ok"] is False
    assert "non in whitelist" in res["error"]
    assert called["flag"] is False


def test_uninstall_module_unknown_code_fails(monkeypatch):
    called = {"flag": False}

    def fake_run(*args, **kw):
        called["flag"] = True

    monkeypatch.setattr(mm.subprocess, "run", fake_run)
    res = mm.uninstall_module(
        "../../bin/sh",
        storage=_DummyStorage(),
        actor="test",
    )
    assert res["ok"] is False
    assert called["flag"] is False


class _DummyStorage:
    """Storage minimale per test_install_module_unknown_code_fails.

    NB: il test sulla whitelist verifica fallisce PRIMA di chiamare il
    DAO, quindi questi metodi non vengono invocati. Implementati come
    no-op safety net.
    """

    def insert_module_install_log(self, **kwargs) -> int:
        return 1

    def update_module_install_log(self, log_id, **kwargs) -> None:
        pass
