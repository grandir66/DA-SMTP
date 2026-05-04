"""Gruppi clienti virtuali (sintetici) calcolati a runtime.

Non sono persistiti in `customer_groups`: vengono iniettati nelle viste e
nell'API verso il listener. Il listener applica la logica equivalente in
`_event_dict` aggiungendo questi codici a `customer_groups` quando le
condizioni sono soddisfatte (es. cliente noto + contratto attivo).

Codici riservati (non usabili come `code` di gruppi reali):
- ``all_contract``: cliente noto con contratto attivo.
- ``no_contract``:  cliente noto senza contratto attivo.
"""
from __future__ import annotations

import sqlite3
from typing import Any

VIRTUAL_GROUP_CODES = ("all_contract", "no_contract")

_VIRTUAL_DEFS: tuple[dict[str, Any], ...] = (
    {
        "code": "all_contract",
        "name": "Tutti i clienti con contratto",
        "description": "Cliente riconosciuto e con contratto attivo. Gruppo automatico.",
        "color": "#0b8a3e",
    },
    {
        "code": "no_contract",
        "name": "Tutti i clienti senza contratto",
        "description": "Cliente riconosciuto ma senza contratto attivo. Gruppo automatico.",
        "color": "#b94a48",
    },
)


def _counts_from_cache(db_path: str, tenant_id: int) -> tuple[int, int]:
    """Conta clienti attivi/non attivi dalla `customers_pg_cache`.

    Ritorna (n_with_contract, n_without_contract). Se la tabella non esiste
    (backend non-postgres), ritorna (0, 0).
    """
    try:
        conn = sqlite3.connect(db_path, timeout=2.0)
        try:
            row = conn.execute(
                "SELECT "
                "  SUM(CASE WHEN contract_active = 1 THEN 1 ELSE 0 END), "
                "  SUM(CASE WHEN contract_active = 0 THEN 1 ELSE 0 END) "
                "FROM customers_pg_cache WHERE tenant_id = ?",
                (int(tenant_id),),
            ).fetchone()
            return int(row[0] or 0), int(row[1] or 0)
        finally:
            conn.close()
    except sqlite3.OperationalError:
        return 0, 0


def virtual_groups(db_path: str, tenant_id: int = 1) -> list[dict[str, Any]]:
    """Ritorna i 2 gruppi virtuali con member_count corrente."""
    n_yes, n_no = _counts_from_cache(db_path, tenant_id)
    counts = {"all_contract": n_yes, "no_contract": n_no}
    out: list[dict[str, Any]] = []
    for d in _VIRTUAL_DEFS:
        out.append({
            "id": None,
            "tenant_id": tenant_id,
            "code": d["code"],
            "name": d["name"],
            "description": d["description"],
            "color": d["color"],
            "enabled": 1,
            "member_count": counts.get(d["code"], 0),
            "is_system": True,
        })
    return out


def merge_with_virtuals(real_groups: list[dict[str, Any]],
                          db_path: str,
                          tenant_id: int = 1) -> list[dict[str, Any]]:
    """Antepone i gruppi virtuali alla lista dei gruppi reali."""
    return virtual_groups(db_path, tenant_id) + list(real_groups)


def is_virtual_code(code: str) -> bool:
    return (code or "").strip() in VIRTUAL_GROUP_CODES
