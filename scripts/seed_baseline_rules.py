"""Seed idempotente delle 6 regole di base operative.

Costruisce sul tenant DOMARC (id=1) la scaletta canonica:

    prio   50   • orfana   "Errori critici (ERROR/FAILED/PROBLEMA)"  → ticket ALTA
    prio  200   ▾ gruppo   "Clienti contratto H24"                    → 2 figli (scaffold)
    prio  300   ▾ gruppo   "Clienti contratto EXT (fuori orario)"     → 2 figli
    prio  400   ▾ gruppo   "Clienti contratto STD (fuori orario)"     → 2 figli
    prio  600   • orfana   "Clienti senza contratto in archivio"      → auto_reply (scaffold)
    prio  999   • orfana   "Catch-all — log mail non gestite"         → flag_only

Note di design:

- Per i clienti H24 e per quelli "senza contratto in archivio" oggi non ci
  sono dati nel customer source (0 H24, 0 senza contratto), quindi le regole
  vengono inserite **disabilitate** come scaffold.
- I clienti STD sono troppi (321 domini) per essere elencati in una singola
  regex (`match_from_regex` lato listener ha tetto 500 char). Per il gruppo
  STD usiamo combinazione tristate (`match_known_customer=1`,
  `match_contract_active=1`, `match_in_service=0`) + `scope_ref="STD"`. Lo
  scope_ref è un discriminatore semantico per il profilo, non oggi
  consumato dal listener legacy ma corretto nel modello e pronto per
  un'evoluzione del listener.
- Per il gruppo EXT (2 domini reali) costruiamo una `match_from_regex`
  esplicita con i domini in OR.

Lo script è idempotente: usa le `name` come chiave di lookup. Re-run = no-op
sulle regole già presenti.

Usage::

    /opt/domarc-smtp-relay-admin/.venv/bin/python3 scripts/seed_baseline_rules.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from domarc_relay_admin.app import create_app
from domarc_relay_admin.config import load_config


# Etichette canoniche (idempotenza key)
LABEL_GROUP_STD = "Clienti contratto STD (fuori orario)"
LABEL_GROUP_EXT = "Clienti contratto EXT (fuori orario)"
LABEL_GROUP_H24 = "Clienti contratto H24"
NAME_ORPHAN_NOCONTRACT = "Clienti senza contratto in archivio"
NAME_ORPHAN_ERRORS = "Errori critici (ERROR/FAILED/PROBLEMA)"
NAME_ORPHAN_CATCHALL = "Catch-all — log mail non gestite"

PRIO_ERRORS = 50
PRIO_GROUP_H24 = 200
PRIO_GROUP_EXT = 300
PRIO_GROUP_STD = 400
PRIO_ORPHAN_NOCONTRACT = 600
PRIO_ORPHAN_CATCHALL = 999


def _clean_domains(raw_domains: list[str]) -> list[str]:
    """I domini nel customer source sono talvolta CSV malformati con `;`
    separatori. Li ripulisco e tengo solo entries con format dominio valido."""
    out: set[str] = set()
    for raw in raw_domains or []:
        for piece in str(raw).split(";"):
            p = piece.strip().lower()
            if p and re.match(r"^[a-z0-9.-]+\.[a-z]{2,}$", p):
                out.add(p)
    return sorted(out)


def _build_from_regex(domains: list[str]) -> str | None:
    """Costruisce ``(?i)^.+@(d1|d2|…)$`` se i domini stanno entro 500 char."""
    if not domains:
        return None
    escaped = [re.escape(d) for d in domains]
    pattern = f"(?i)^.+@({'|'.join(escaped)})$"
    if len(pattern) > 500:
        return None
    return pattern


def _find_existing(storage, *, name: str = None, group_label: str = None,
                   tenant_id: int = 1):
    """Lookup per idempotenza."""
    rules = storage.list_rules(tenant_id=tenant_id)
    for r in rules:
        if name and r.get("name") == name:
            return r
        if group_label and r.get("group_label") == group_label:
            return r
    return None


def main() -> int:
    cfg = load_config()
    app = create_app(cfg, init_db=False)
    storage = app.extensions["domarc_storage"]
    cs = app.extensions.get("domarc_customer_source")
    if not cs:
        print("ERRORE: customer source non configurato.", file=sys.stderr)
        return 1

    customers = list(cs.list_customers())

    # Aggrega per profilo (normalizzo "standard" → "STD") + contract_active
    by_profile: dict[str, list] = {"STD": [], "EXT": [], "H24": [], "OTHER": []}
    no_contract: list = []
    for c in customers:
        prof = (c.tipologia_servizio or "").upper()
        if prof in ("STANDARD", "STD"):
            prof = "STD"
        elif prof not in ("EXT", "H24"):
            prof = "OTHER"
        if c.contract_active:
            by_profile[prof].append(c)
        else:
            no_contract.append(c)

    print("Customer source — distribuzione:")
    for k, v in by_profile.items():
        print(f"  {k}: {len(v)} clienti con contratto")
    print(f"  senza contratto: {len(no_contract)} clienti")
    print()

    tid = 1  # tenant DOMARC
    by_label = LABEL_GROUP_STD  # placeholder
    created = 0
    skipped = 0

    # ---------------------------------------------- Errori critici (orfana)
    existing = _find_existing(storage, name=NAME_ORPHAN_ERRORS, tenant_id=tid)
    if existing:
        print(f"⏭  Orfana «{NAME_ORPHAN_ERRORS}» già presente (id={existing['id']}).")
        skipped += 1
    else:
        rid = storage.upsert_rule({
            "name": NAME_ORPHAN_ERRORS,
            "priority": PRIO_ERRORS,
            "enabled": True,
            "scope_type": "global",
            "match_subject_regex": r"(?i)\b(ERROR|FAILED|PROBLEMA|FAILURE)\b",
            "action": "create_ticket",
            "action_map": {
                "settore": "assistenza",
                "urgenza": "ALTA",
                "keep_original_delivery": True,
                "also_deliver_to": "ticket@domarc.it",
            },
            "continue_after_match": False,
        }, tenant_id=tid, created_by="seed_baseline")
        print(f"✓ Creata orfana errori (id={rid}, prio={PRIO_ERRORS}).")
        created += 1

    # ----------------------------------------- Gruppo H24 (scaffold disabled)
    existing = _find_existing(storage, group_label=LABEL_GROUP_H24, tenant_id=tid)
    if existing:
        print(f"⏭  Gruppo H24 già presente (id={existing['id']}).")
        skipped += 1
    else:
        h24_count = len(by_profile["H24"])
        h24_enabled = h24_count > 0
        # Match: clienti H24 noti + contratto attivo. Senza filtro in_service
        # (H24 è sempre in service), perché ogni mail va trattata come ALTA.
        gid = storage.upsert_rule({
            "name": f"[GRUPPO] {LABEL_GROUP_H24}",
            "group_label": LABEL_GROUP_H24,
            "is_group": 1,
            "priority": PRIO_GROUP_H24,
            "enabled": h24_enabled,
            "scope_type": "sector_pack",
            "scope_ref": "H24",
            "match_to_domain": "domarc.it",
            "match_known_customer": 1,
            "match_contract_active": 1,
            "exclusive_match": True,
            "action": "group",
            "action_map": {
                "keep_original_delivery": True,
                "also_deliver_to": "h24@domarc.it",
                "reply_mode": "to_sender_only",
                "generate_auth_code": True,
                "auth_code_ttl_hours": 4,
            },
        }, tenant_id=tid, created_by="seed_baseline")
        storage.upsert_rule({
            "name": "Auto-reply H24 (preso in carico)",
            "parent_id": gid,
            "priority": PRIO_GROUP_H24 + 10,
            "enabled": h24_enabled,
            "action": "auto_reply",
            "action_map": {"template_id": 1, "reply_subject_prefix": "[H24] Re: "},
            "continue_in_group": True,
        }, tenant_id=tid, created_by="seed_baseline")
        storage.upsert_rule({
            "name": "Crea ticket H24 urgenza ALTA",
            "parent_id": gid,
            "priority": PRIO_GROUP_H24 + 20,
            "enabled": h24_enabled,
            "action": "create_ticket",
            "action_map": {"settore": "assistenza_h24", "urgenza": "ALTA"},
        }, tenant_id=tid, created_by="seed_baseline")
        status = "abilitato" if h24_enabled else "DISABILITATO (0 clienti H24)"
        print(f"✓ Creato gruppo H24 (id={gid}, prio={PRIO_GROUP_H24}, {status}).")
        created += 1

    # -------------------------------------------- Gruppo EXT (fuori orario)
    existing = _find_existing(storage, group_label=LABEL_GROUP_EXT, tenant_id=tid)
    if existing:
        print(f"⏭  Gruppo EXT già presente (id={existing['id']}).")
        skipped += 1
    else:
        ext_domains: list[str] = []
        for c in by_profile["EXT"]:
            ext_domains.extend(_clean_domains(c.domains or []))
        ext_domains = sorted(set(ext_domains))
        ext_enabled = len(ext_domains) > 0
        from_regex = _build_from_regex(ext_domains)
        gid = storage.upsert_rule({
            "name": f"[GRUPPO] {LABEL_GROUP_EXT}",
            "group_label": LABEL_GROUP_EXT,
            "is_group": 1,
            "priority": PRIO_GROUP_EXT,
            "enabled": ext_enabled,
            "scope_type": "sector_pack",
            "scope_ref": "EXT",
            "match_to_domain": "domarc.it",
            "match_from_regex": from_regex,
            "match_known_customer": 1,
            "match_contract_active": 1,
            "match_in_service": 0,  # fuori orario esteso (sera/notte/domenica)
            "exclusive_match": True,
            "action": "group",
            "action_map": {
                "keep_original_delivery": True,
                "also_deliver_to": "ticket@domarc.it",
                "reply_mode": "to_sender_only",
                "generate_auth_code": True,
                "auth_code_ttl_hours": 12,
            },
        }, tenant_id=tid, created_by="seed_baseline")
        storage.upsert_rule({
            "name": "Auto-reply EXT (fuori orario)",
            "parent_id": gid,
            "priority": PRIO_GROUP_EXT + 10,
            "enabled": ext_enabled,
            "action": "auto_reply",
            "action_map": {"template_id": 1, "reply_subject_prefix": "[EXT] Re: "},
            "continue_in_group": True,
        }, tenant_id=tid, created_by="seed_baseline")
        storage.upsert_rule({
            "name": "Crea ticket EXT urgenza NORMALE",
            "parent_id": gid,
            "priority": PRIO_GROUP_EXT + 20,
            "enabled": ext_enabled,
            "action": "create_ticket",
            "action_map": {"settore": "assistenza", "urgenza": "NORMALE"},
        }, tenant_id=tid, created_by="seed_baseline")
        regex_info = f"regex {len(ext_domains)} domini" if from_regex else "no regex (>500 char)"
        print(f"✓ Creato gruppo EXT (id={gid}, prio={PRIO_GROUP_EXT}, {regex_info}).")
        created += 1

    # -------------------------------------------- Gruppo STD (fuori orario)
    existing = _find_existing(storage, group_label=LABEL_GROUP_STD, tenant_id=tid)
    if existing:
        print(f"⏭  Gruppo STD già presente (id={existing['id']}).")
        skipped += 1
    else:
        # STD ha 321 domini → match_from_regex troppo lungo. Usiamo solo le
        # tristate + scope_ref. Il listener oggi distingue via in_service
        # (calcolato dallo schedule del cliente STD), il scope_ref è un
        # discriminatore semantico per il profilo.
        gid = storage.upsert_rule({
            "name": f"[GRUPPO] {LABEL_GROUP_STD}",
            "group_label": LABEL_GROUP_STD,
            "is_group": 1,
            "priority": PRIO_GROUP_STD,
            "enabled": True,
            "scope_type": "sector_pack",
            "scope_ref": "STD",
            "match_to_domain": "domarc.it",
            "match_known_customer": 1,
            "match_contract_active": 1,
            "match_in_service": 0,  # fuori orario standard (lun-ven 9-18 → sera/sabato/dom)
            "exclusive_match": True,
            "action": "group",
            "action_map": {
                "keep_original_delivery": True,
                "also_deliver_to": "ticket@domarc.it",
                "reply_mode": "to_sender_only",
                "generate_auth_code": True,
                "auth_code_ttl_hours": 24,
            },
        }, tenant_id=tid, created_by="seed_baseline")
        storage.upsert_rule({
            "name": "Auto-reply STD (fuori orario)",
            "parent_id": gid,
            "priority": PRIO_GROUP_STD + 10,
            "enabled": True,
            "action": "auto_reply",
            "action_map": {"template_id": 1, "reply_subject_prefix": "Re: "},
            "continue_in_group": True,
        }, tenant_id=tid, created_by="seed_baseline")
        storage.upsert_rule({
            "name": "Crea ticket STD urgenza NORMALE",
            "parent_id": gid,
            "priority": PRIO_GROUP_STD + 20,
            "enabled": True,
            "action": "create_ticket",
            "action_map": {"settore": "assistenza", "urgenza": "NORMALE"},
        }, tenant_id=tid, created_by="seed_baseline")
        std_count = len(by_profile["STD"])
        print(f"✓ Creato gruppo STD (id={gid}, prio={PRIO_GROUP_STD}, {std_count} clienti coperti via tristate).")
        created += 1

    # ---------------------------------- Senza contratto ma in archivio (orfana)
    existing = _find_existing(storage, name=NAME_ORPHAN_NOCONTRACT, tenant_id=tid)
    if existing:
        print(f"⏭  Orfana «{NAME_ORPHAN_NOCONTRACT}» già presente (id={existing['id']}).")
        skipped += 1
    else:
        nc_count = len(no_contract)
        nc_enabled = nc_count > 0
        rid = storage.upsert_rule({
            "name": NAME_ORPHAN_NOCONTRACT,
            "priority": PRIO_ORPHAN_NOCONTRACT,
            "enabled": nc_enabled,
            "scope_type": "global",
            "match_to_domain": "domarc.it",
            "match_known_customer": 1,
            "match_contract_active": 0,
            "action": "auto_reply",
            "action_map": {
                "template_id": 1,
                "reply_subject_prefix": "[Senza contratto] Re: ",
                "reply_mode": "to_sender_only",
                "keep_original_delivery": True,
                "also_deliver_to": "commerciale@domarc.it",
            },
            "continue_after_match": False,
        }, tenant_id=tid, created_by="seed_baseline")
        status = "abilitata" if nc_enabled else "DISABILITATA (0 clienti senza contratto)"
        print(f"✓ Creata orfana 'senza contratto' (id={rid}, prio={PRIO_ORPHAN_NOCONTRACT}, {status}).")
        created += 1

    # ---------------------------------------------- Catch-all log (orfana)
    existing = _find_existing(storage, name=NAME_ORPHAN_CATCHALL, tenant_id=tid)
    if existing:
        print(f"⏭  Orfana catch-all già presente (id={existing['id']}).")
        skipped += 1
    else:
        rid = storage.upsert_rule({
            "name": NAME_ORPHAN_CATCHALL,
            "priority": PRIO_ORPHAN_CATCHALL,
            "enabled": True,
            "scope_type": "global",
            "match_to_regex": r".*",
            "action": "flag_only",
            "action_map": {"keep_original_delivery": True},
            "continue_after_match": False,
        }, tenant_id=tid, created_by="seed_baseline")
        print(f"✓ Creata orfana catch-all (id={rid}, prio={PRIO_ORPHAN_CATCHALL}).")
        created += 1

    print()
    print(f"Riepilogo: {created} regole/gruppi creati, {skipped} già esistenti.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
