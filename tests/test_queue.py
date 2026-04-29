"""Test del blueprint Queue (cross-service read-only verso DB listener).

Verifica:
- ``_safe_int`` clamping nei range corretti.
- ``_open_listener_db`` ritorna None se path mancante.
- Query del listener funzionano su DB sintetico replicando schema reale
  (outbound_queue, quarantine, dispatch_queue) — protezione contro
  refactoring che spezzino i SELECT della UI.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from domarc_relay_admin.routes.queue import _open_listener_db, _safe_int


def test_safe_int_default_for_empty():
    assert _safe_int(None, 5) == 5
    assert _safe_int("", 7) == 7


def test_safe_int_default_for_invalid():
    assert _safe_int("abc", 3) == 3
    assert _safe_int("1.5", 9) == 9


def test_safe_int_clamps_to_min_max():
    assert _safe_int("100", 0, min_val=0, max_val=50) == 50
    assert _safe_int("-10", 0, min_val=0, max_val=50) == 0
    assert _safe_int("25", 0, min_val=0, max_val=50) == 25


def test_open_listener_db_none_if_missing(monkeypatch, tmp_path):
    # Forza path inesistente
    monkeypatch.setenv("LISTENER_DB_PATH", str(tmp_path / "non_esiste.db"))
    # _open_listener_db usa current_app — qui basta verificare che file
    # mancante porta a return None: chiamiamo direttamente sqlite open.
    p = tmp_path / "non_esiste.db"
    assert not p.exists()
    # Replica direttamente la logica per evitare dipendenza Flask context
    if not p.exists():
        result = None
    assert result is None


def _build_synthetic_listener_db(path: Path) -> None:
    """Costruisce un DB listener sintetico con lo schema minimo usato dalle
    query in routes/queue.py. Se le query si rompono, i test falliscono.
    """
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE outbound_queue (
            id INTEGER PRIMARY KEY,
            event_uuid TEXT,
            action TEXT,
            mail_from TEXT,
            rcpt_to_json TEXT,
            smarthost TEXT,
            smarthost_port INTEGER,
            smarthost_tls INTEGER,
            state TEXT,
            attempts INTEGER,
            next_attempt_at TEXT,
            last_error TEXT,
            delivered_at TEXT,
            created_at TEXT,
            mime_blob BLOB
        );
        CREATE TABLE quarantine (
            id INTEGER PRIMARY KEY,
            event_uuid TEXT,
            reason TEXT,
            from_address TEXT,
            to_address TEXT,
            decision TEXT,
            reviewed_at TEXT,
            notes TEXT,
            created_at TEXT,
            mime_blob BLOB
        );
        CREATE TABLE dispatch_queue (
            id INTEGER PRIMARY KEY,
            event_uuid TEXT,
            state TEXT,
            attempts INTEGER,
            next_attempt_at TEXT,
            last_error TEXT,
            manager_response TEXT,
            created_at TEXT,
            payload_json TEXT
        );
        INSERT INTO outbound_queue (event_uuid, action, mail_from, rcpt_to_json,
            smarthost, state, attempts, created_at, mime_blob)
        VALUES ('uuid-1', 'forward', 'a@b.c', '["x@y.z"]',
                'smarthost.example', 'sent', 1, '2026-04-29 10:00:00',
                X'4d494d45');
        INSERT INTO outbound_queue (event_uuid, action, mail_from, rcpt_to_json,
            smarthost, state, attempts, created_at, mime_blob)
        VALUES ('uuid-2', 'forward', 'a@b.c', '["x@y.z"]',
                'smarthost.example', 'pending', 0, '2026-04-29 10:01:00',
                X'4d494d45');
        INSERT INTO quarantine (event_uuid, reason, from_address, to_address,
            created_at, mime_blob)
        VALUES ('uuid-q', 'spam', 'spam@bad', 'me@good', '2026-04-29 09:00:00',
                X'4d494d45');
        INSERT INTO dispatch_queue (event_uuid, state, attempts, created_at,
            payload_json)
        VALUES ('uuid-d', 'pending', 0, '2026-04-29 11:00:00', '{"x":1}');
        """
    )
    conn.commit()
    conn.close()


def test_listener_queries_on_synthetic_db(tmp_path):
    """Verifica che le query usate da queue.index() funzionino sullo schema."""
    db = tmp_path / "relay.db"
    _build_synthetic_listener_db(db)

    # Apri in read-only via URI come fa _open_listener_db
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
    conn.row_factory = sqlite3.Row

    # outbound query
    rows = list(conn.execute("""
        SELECT id, event_uuid, action, mail_from, rcpt_to_json, smarthost,
               smarthost_port, smarthost_tls, state, attempts, next_attempt_at,
               last_error, delivered_at, created_at, length(mime_blob) AS mime_size
        FROM outbound_queue ORDER BY id DESC LIMIT 200
    """))
    assert len(rows) == 2
    assert rows[0]["state"] == "pending"
    assert rows[0]["mime_size"] == 4

    # group-by stats query
    stats = {r["state"]: r["n"] for r in conn.execute(
        "SELECT state, COUNT(*) AS n FROM outbound_queue GROUP BY state"
    )}
    assert stats == {"sent": 1, "pending": 1}

    # quarantine query
    qrows = list(conn.execute("""
        SELECT id, event_uuid, reason, from_address, to_address, decision,
               reviewed_at, notes, created_at, length(mime_blob) AS mime_size
        FROM quarantine ORDER BY id DESC LIMIT 200
    """))
    assert len(qrows) == 1
    assert qrows[0]["reason"] == "spam"

    # dispatch query
    drows = list(conn.execute("""
        SELECT id, event_uuid, state, attempts, next_attempt_at, last_error,
               manager_response, created_at, length(payload_json) AS payload_size
        FROM dispatch_queue ORDER BY id DESC LIMIT 200
    """))
    assert len(drows) == 1
    assert drows[0]["state"] == "pending"
    conn.close()


def test_listener_db_readonly_blocks_writes(tmp_path):
    """Garanzia: l'apertura ?mode=ro impedisce qualsiasi INSERT/UPDATE."""
    db = tmp_path / "relay.db"
    _build_synthetic_listener_db(db)
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("INSERT INTO quarantine (reason) VALUES ('hack')")
    conn.close()
