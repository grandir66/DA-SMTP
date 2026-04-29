"""Configurazione comune per i test del Rule Engine v2."""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture
def tmp_db_path(tmp_path: Path) -> str:
    return str(tmp_path / "test_admin.db")


@pytest.fixture
def storage(tmp_db_path: str):
    from domarc_relay_admin.storage.sqlite_impl import SqliteStorage

    s = SqliteStorage(tmp_db_path)
    return s


@pytest.fixture
def tenant_id(storage) -> int:
    """Crea un tenant di test e ritorna il suo id."""
    with storage._connect() as conn:  # type: ignore[attr-defined]
        conn.execute(
            "INSERT INTO tenants (codice, ragione_sociale, contract_active, enabled) "
            "VALUES (?, ?, 1, 1)",
            ("TEST", "Tenant di test"),
        )
        row = conn.execute("SELECT id FROM tenants WHERE codice = ?", ("TEST",)).fetchone()
        conn.commit()
        return int(row[0])
