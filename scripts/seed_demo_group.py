"""Seed idempotente di un gruppo dimostrativo "Fuori orario contratto".

Usage::

    /opt/domarc-smtp-relay-admin/.venv/bin/python3 scripts/seed_demo_group.py

Crea:
- 1 gruppo padre "Fuori orario contratto" con i match_* condivisi
  (match_to_domain=domarc.it, match_in_service=0, match_contract_active=1) e
  defaults action_map ereditabili (keep_original_delivery, also_deliver_to,
  reply_mode, generate_auth_code, auth_code_ttl_hours).
- 2 figli: auto_reply (template_id=1, continue_in_group=True) +
  create_ticket (settore=assistenza, urgenza=NORMALE).

Idempotente: skippa se esiste già un gruppo con lo stesso ``group_label``
sullo stesso tenant.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from domarc_relay_admin.app import create_app
from domarc_relay_admin.config import load_config

DEMO_LABEL = "Fuori orario contratto"
DEMO_GROUP_PRIORITY = 500
DEMO_CHILD_AUTOREPLY_PRIORITY = 510
DEMO_CHILD_TICKET_PRIORITY = 520


def main() -> int:
    cfg = load_config()
    app = create_app(cfg, init_db=False)
    storage = app.extensions["domarc_storage"]

    # Trova il primo tenant disponibile (di default tenant_id=1)
    tenants = storage.list_tenants()
    if not tenants:
        print("Nessun tenant configurato — aborto seed.", file=sys.stderr)
        return 1
    tid = int(tenants[0]["id"])

    # Idempotenza: cerco un gruppo esistente con lo stesso label
    existing = [
        r for r in storage.list_top_level_items(tenant_id=tid)
        if r.get("is_group") and r.get("group_label") == DEMO_LABEL
    ]
    if existing:
        print(f"Gruppo demo già presente (id={existing[0]['id']}). Skip.")
        return 0

    group_id = storage.upsert_rule({
        "name": f"[GRUPPO] {DEMO_LABEL}",
        "priority": DEMO_GROUP_PRIORITY,
        "enabled": True,
        "is_group": 1,
        "group_label": DEMO_LABEL,
        "scope_type": "global",
        "match_to_domain": "domarc.it",
        "match_in_service": 0,
        "match_contract_active": 1,
        "action": "group",
        "action_map": {
            "keep_original_delivery": True,
            "also_deliver_to": "ticket@domarc.it",
            "reply_mode": "to_sender_only",
            "generate_auth_code": True,
            "auth_code_ttl_hours": 12,
        },
        "exclusive_match": True,
    }, tenant_id=tid, created_by="seed_demo_group")

    storage.upsert_rule({
        "name": "Auto-reply out_of_hours",
        "priority": DEMO_CHILD_AUTOREPLY_PRIORITY,
        "enabled": True,
        "parent_id": group_id,
        "scope_type": "global",
        "action": "auto_reply",
        "action_map": {
            "template_id": 1,
            "reply_subject_prefix": "Re: ",
        },
        "continue_in_group": True,
    }, tenant_id=tid, created_by="seed_demo_group")

    storage.upsert_rule({
        "name": "Crea ticket NORMALE",
        "priority": DEMO_CHILD_TICKET_PRIORITY,
        "enabled": True,
        "parent_id": group_id,
        "scope_type": "global",
        "action": "create_ticket",
        "action_map": {
            "settore": "assistenza",
            "urgenza": "NORMALE",
        },
    }, tenant_id=tid, created_by="seed_demo_group")

    print(f"Seed completato. Gruppo id={group_id} con 2 figli.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
