"""Import dati dal Stormshield Manager (PostgreSQL) al SQLite del standalone.

Operazione one-shot: copia rules / templates / service_hours / auth_codes /
aggregations dalle tabelle `smtp_relay_*` del Postgres del manager alle tabelle
omonime del SQLite locale del standalone.

Idempotente: ON CONFLICT DO NOTHING su tutte le INSERT (per `code`, per
`(tenant_id, name)` ecc.). Eseguendolo più volte non duplica.

Usage:
    python -m domarc_relay_admin.import_from_manager \\
        --pg-host 192.168.4.41 --pg-db stormshield --pg-user stormshield \\
        --pg-password stormshield123 [--tenant-id 1]
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Any

from .config import load_config
from .storage import get_storage

logger = logging.getLogger(__name__)


def import_data(*, pg_host: str, pg_port: int, pg_db: str,
                pg_user: str, pg_password: str,
                tenant_id_filter: int | None = None,
                dry_run: bool = False) -> dict[str, int]:
    """Esegue l'import. Ritorna dict con conteggi per tabella."""
    try:
        import psycopg2
        import psycopg2.extras
    except ImportError:
        print("ERRORE: psycopg2 richiesto. Installa con:\n"
              "  pip install psycopg2-binary", file=sys.stderr)
        return {"error": "psycopg2_missing"}

    cfg = load_config()
    storage = get_storage(cfg)

    pg = psycopg2.connect(
        host=pg_host, port=pg_port, dbname=pg_db,
        user=pg_user, password=pg_password,
    )
    counts: dict[str, int] = {}

    try:
        # 1. Tenants — copia anche se esistono nel SQLite (poteva essere solo DOMARC default)
        with pg.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            where = ""
            params: list[Any] = []
            if tenant_id_filter is not None:
                where = "WHERE id = %s"
                params.append(tenant_id_filter)
            cur.execute(f"SELECT * FROM smtp_relay_tenants {where}", params)
            n = 0
            for r in cur.fetchall():
                if dry_run:
                    n += 1
                    continue
                # Skip se esiste già (per codice)
                existing = storage.get_tenant_by_codice(r["codice"]) if hasattr(storage, "get_tenant_by_codice") else None
                if existing:
                    continue
                # Force id = manager id per preservare riferimenti
                try:
                    storage.upsert_tenant({
                        "id": r["id"],
                        "codice": r["codice"],
                        "ragione_sociale": r["ragione_sociale"],
                        "description": r.get("description"),
                        "contract_active": bool(r["contract_active"]),
                        "enabled": bool(r["enabled"]),
                        "created_by": r.get("created_by") or "import",
                    })
                    n += 1
                except Exception as exc:  # noqa: BLE001
                    logger.warning("import tenant %s: %s", r["codice"], exc)
            counts["tenants"] = n

        # 2. Rules
        with pg.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            where = "WHERE 1=1"
            params = []
            if tenant_id_filter is not None:
                where += " AND tenant_id = %s"
                params.append(tenant_id_filter)
            cur.execute(f"SELECT * FROM smtp_relay_rules {where}", params)
            n = 0
            for r in cur.fetchall():
                if dry_run:
                    n += 1
                    continue
                am = r.get("action_map")
                if am and not isinstance(am, dict):
                    try:
                        am = json.loads(am)
                    except (TypeError, ValueError):
                        am = {}
                try:
                    storage.upsert_rule({
                        "name": r["name"],
                        "scope_type": r["scope_type"],
                        "scope_ref": r["scope_ref"],
                        "priority": r["priority"],
                        "enabled": bool(r["enabled"]),
                        "match_from_regex": r.get("match_from_regex"),
                        "match_to_regex": r.get("match_to_regex"),
                        "match_subject_regex": r.get("match_subject_regex"),
                        "match_body_regex": r.get("match_body_regex"),
                        "match_to_domain": r.get("match_to_domain"),
                        "match_at_hours": r.get("match_at_hours"),
                        "match_in_service": r.get("match_in_service"),
                        "match_contract_active": r.get("match_contract_active"),
                        "match_tag": r.get("match_tag"),
                        "action": r["action"],
                        "action_map": am,
                        "severity": r.get("severity"),
                        "continue_after_match": bool(r.get("continue_after_match")),
                    }, tenant_id=r["tenant_id"], created_by=r.get("created_by") or "import")
                    n += 1
                except Exception as exc:  # noqa: BLE001
                    logger.warning("import rule %s: %s", r["name"], exc)
            counts["rules"] = n

        # 3. Reply templates
        with pg.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            where = "WHERE 1=1"
            params = []
            if tenant_id_filter is not None:
                where += " AND tenant_id = %s"
                params.append(tenant_id_filter)
            cur.execute(f"SELECT * FROM smtp_relay_reply_templates {where}", params)
            n = 0
            for r in cur.fetchall():
                if dry_run:
                    n += 1
                    continue
                atts = r.get("attachment_paths")
                if atts and not isinstance(atts, list):
                    try:
                        atts = json.loads(atts) if isinstance(atts, str) else list(atts)
                    except (TypeError, ValueError):
                        atts = []
                try:
                    storage.upsert_template({
                        "name": r["name"],
                        "description": r.get("description"),
                        "subject_tmpl": r["subject_tmpl"],
                        "body_html_tmpl": r["body_html_tmpl"],
                        "body_text_tmpl": r.get("body_text_tmpl"),
                        "reply_from_name": r.get("reply_from_name"),
                        "reply_from_email": r.get("reply_from_email"),
                        "attachment_paths": atts or None,
                        "enabled": bool(r.get("enabled", True)),
                        "updated_by": r.get("updated_by") or "import",
                    }, tenant_id=r["tenant_id"])
                    n += 1
                except Exception as exc:  # noqa: BLE001
                    logger.warning("import template %s: %s", r["name"], exc)
            counts["templates"] = n

        # 4. Service hours
        with pg.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT to_regclass('public.smtp_relay_service_hours') AS t"
            )
            if cur.fetchone()["t"]:
                where = "WHERE 1=1"
                params = []
                if tenant_id_filter is not None:
                    where += " AND tenant_id = %s"
                    params.append(tenant_id_filter)
                cur.execute(f"SELECT * FROM smtp_relay_service_hours {where}", params)
                n = 0
                for r in cur.fetchall():
                    if dry_run:
                        n += 1
                        continue
                    sched = r.get("schedule") or {}
                    hols = r.get("holidays") or []
                    excs = r.get("schedule_exceptions") or []
                    try:
                        storage.upsert_service_hours({
                            "codice_cliente": r["codice_cliente"],
                            "profile": r.get("profile") or "custom",
                            "profile_id": r.get("profile_id"),
                            "timezone": r.get("timezone") or "Europe/Rome",
                            "schedule": sched if isinstance(sched, dict) else json.loads(sched),
                            "holidays": hols if isinstance(hols, list) else json.loads(hols),
                            "schedule_exceptions": excs if isinstance(excs, list) else json.loads(excs),
                            "ah_key": r.get("ah_key"),
                            "notes": r.get("notes"),
                            "enabled": bool(r.get("enabled", True)),
                            "updated_by": r.get("updated_by") or "import",
                        }, tenant_id=r["tenant_id"])
                        n += 1
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("import service_hours %s: %s", r["codice_cliente"], exc)
                counts["service_hours"] = n
            else:
                counts["service_hours"] = 0

        # 5. Auth codes (solo non-scaduti)
        with pg.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT to_regclass('public.smtp_relay_authorization_codes') AS t"
            )
            if cur.fetchone()["t"]:
                where = "WHERE valid_until > NOW()"
                params = []
                if tenant_id_filter is not None:
                    where += " AND tenant_id = %s"
                    params.append(tenant_id_filter)
                cur.execute(f"SELECT * FROM smtp_relay_authorization_codes {where}", params)
                n = 0
                # SQLite issue_auth_code genera un nuovo codice; per import preserviamo
                # il codice originale. Inseriamo direttamente via SQL.
                from .storage.sqlite_impl import SqliteStorage
                if isinstance(storage, SqliteStorage):
                    for r in cur.fetchall():
                        if dry_run:
                            n += 1
                            continue
                        try:
                            with storage.transaction() as conn:
                                conn.execute(
                                    """INSERT OR IGNORE INTO authorization_codes
                                           (tenant_id, code, codice_cliente, rule_id,
                                            generated_at, valid_until, used_at, used_by, note)
                                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                                    (r["tenant_id"], r["code"], r.get("codice_cliente"),
                                     None, str(r["generated_at"]), str(r["valid_until"]),
                                     str(r["used_at"]) if r.get("used_at") else None,
                                     r.get("used_by"), r.get("note")),
                                )
                                if conn.total_changes > 0:
                                    n += 1
                        except Exception as exc:  # noqa: BLE001
                            logger.warning("import auth_code %s: %s", r.get("code"), exc)
                counts["auth_codes"] = n
            else:
                counts["auth_codes"] = 0

        # 6. Profili orari built-in (4 canonici STD/EXT/H24/NO).
        # I 4 profili sono già seedati dalla migration 003 con orari Domarc canonici.
        # Se il manager espone uno schema esteso (`schedule_template` etc., presente
        # sul PG `solution`, non sul PG `stormshield` di default), riallinea gli orari.
        with pg.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT to_regclass('public.customer_availability_types') AS t")
            row = cur.fetchone()
            if True:
                if row and row["t"]:
                    # Verifica se lo schema esteso è disponibile (colonna `schedule_template`)
                    cur.execute(
                        """SELECT column_name FROM information_schema.columns
                            WHERE table_name='customer_availability_types'
                              AND column_name='schedule_template'"""
                    )
                    has_extended = cur.fetchone() is not None
                    if not has_extended:
                        logger.info(
                            "customer_availability_types non espone schedule_template "
                            "(schema parziale su questo PG): skip import profili — "
                            "i 4 canonici sono già seedati dalla migration 003."
                        )
                        counts["profiles"] = 0
                        row = {"t": None}  # forza skip del blocco sotto
                if row and row["t"]:
                    cur.execute(
                        """SELECT code, description, hours_description, details,
                                  schedule_template, exclude_holidays,
                                  requires_authorization_always, authorize_outside_hours
                             FROM customer_availability_types
                            WHERE COALESCE(active, TRUE) = TRUE
                            ORDER BY display_order ASC""",
                    )
                    n = 0
                    for r in cur.fetchall():
                        if dry_run:
                            n += 1
                            continue
                        sched = r.get("schedule_template")
                        if isinstance(sched, str):
                            try:
                                sched = json.loads(sched)
                            except (TypeError, ValueError):
                                sched = {}
                        # Normalizza chiavi: PG usa 0..6 (lun..dom), il SQLite mon..sun
                        if isinstance(sched, dict) and any(k.isdigit() for k in sched.keys() if isinstance(k, str)):
                            day_map = {"0":"mon","1":"tue","2":"wed","3":"thu","4":"fri","5":"sat","6":"sun"}
                            sched = {day_map.get(k, k): v for k, v in sched.items()}
                        try:
                            storage.upsert_profile({
                                "code": r["code"],
                                "name": r["description"] or r["code"],
                                "description": r["hours_description"],
                                "details": r["details"],
                                "schedule": sched or {},
                                "holidays": [],
                                "holidays_auto": True,
                                "is_builtin": True,
                                "enabled": True,
                                "exclude_holidays": bool(r["exclude_holidays"]) if r["exclude_holidays"] is not None else True,
                                "requires_authorization_always": bool(r["requires_authorization_always"]) if r["requires_authorization_always"] is not None else False,
                                "authorize_outside_hours": bool(r["authorize_outside_hours"]) if r["authorize_outside_hours"] is not None else True,
                                "updated_by": "import_manager",
                            }, tenant_id=None)
                            n += 1
                        except Exception as exc:  # noqa: BLE001
                            logger.warning("import profile %s: %s", r.get("code"), exc)
                    counts["profiles"] = n
                else:
                    counts["profiles"] = 0

        # 7. Routes (smarthost / forward / redirect per alias)
        with pg.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT to_regclass('public.smtp_relay_routes') AS t")
            if cur.fetchone()["t"]:
                where = "WHERE 1=1"
                params = []
                if tenant_id_filter is not None:
                    where += " AND tenant_id = %s"
                    params.append(tenant_id_filter)
                cur.execute(f"SELECT * FROM smtp_relay_routes {where}", params)
                n = 0
                for r in cur.fetchall():
                    if dry_run:
                        n += 1
                        continue
                    try:
                        storage.upsert_route({
                            "local_part": r.get("local_part"),
                            "domain": r.get("domain"),
                            "codice_cliente": r.get("codice_cliente"),
                            "forward_target": r.get("forward_target"),
                            "forward_port": r.get("forward_port") or 25,
                            "forward_tls": r.get("forward_tls") or "opportunistic",
                            "redirect_target": r.get("redirect_target"),
                            "enabled": bool(r.get("enabled", True)),
                            "apply_rules": bool(r.get("apply_rules", True)),
                            "notes": r.get("notes"),
                        }, tenant_id=r["tenant_id"])
                        n += 1
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("import route %s@%s: %s",
                                       r.get("local_part"), r.get("domain"), exc)
                counts["routes"] = n
            else:
                counts["routes"] = 0

        # 8. Domain routing (smarthost per dominio)
        with pg.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT to_regclass('public.smtp_relay_domain_routing') AS t")
            if cur.fetchone()["t"]:
                where = "WHERE 1=1"
                params = []
                if tenant_id_filter is not None:
                    where += " AND tenant_id = %s"
                    params.append(tenant_id_filter)
                cur.execute(f"SELECT * FROM smtp_relay_domain_routing {where}", params)
                n = 0
                for r in cur.fetchall():
                    if dry_run:
                        n += 1
                        continue
                    try:
                        # Manager PG usa 'smarthost' (singolo); standalone SQLite usa 'smarthost_host'
                        smarthost_host = r.get("smarthost") or r.get("smarthost_host")
                        storage.upsert_domain_routing({
                            "domain": r["domain"],
                            "smarthost_host": smarthost_host,
                            "smarthost_port": r.get("smarthost_port") or 25,
                            "smarthost_tls": r.get("smarthost_tls") or "opportunistic",
                            "apply_rules": bool(r.get("apply_rules", True)),
                            "enabled": bool(r.get("enabled", True)),
                            "notes": r.get("notes"),
                        }, tenant_id=r["tenant_id"])
                        n += 1
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("import domain_routing %s: %s", r.get("domain"), exc)
                counts["domain_routing"] = n
            else:
                counts["domain_routing"] = 0

        # 9. Addresses from / to
        for kind, tbl in (("from", "smtp_relay_addresses_from"),
                          ("to",   "smtp_relay_addresses_to")):
            with pg.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(f"SELECT to_regclass('public.{tbl}') AS t")
                if not cur.fetchone()["t"]:
                    counts[f"addresses_{kind}"] = 0
                    continue
                cur.execute(f"SELECT * FROM {tbl}")
                n = 0
                from .storage.sqlite_impl import SqliteStorage
                for r in cur.fetchall():
                    if dry_run:
                        n += 1
                        continue
                    try:
                        if isinstance(storage, SqliteStorage):
                            with storage.transaction() as conn:
                                if kind == "from":
                                    conn.execute(
                                        """INSERT OR IGNORE INTO addresses_from
                                              (tenant_id, email_address, local_part, domain,
                                               codice_cliente, codcli_source,
                                               seen_count, first_seen_at, last_seen_at, created_by)
                                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                                        (r.get("tenant_id") or 1,
                                         r["email_address"], r.get("local_part"), r.get("domain"),
                                         r.get("codice_cliente"), r.get("codcli_source") or "auto",
                                         r.get("seen_count") or 1,
                                         str(r["first_seen_at"]) if r.get("first_seen_at") else None,
                                         str(r["last_seen_at"]) if r.get("last_seen_at") else None,
                                         r.get("created_by") or "import"),
                                    )
                                else:
                                    conn.execute(
                                        """INSERT OR IGNORE INTO addresses_to
                                              (tenant_id, email_address, local_part, domain,
                                               codice_cliente,
                                               seen_count, first_seen_at, last_seen_at)
                                           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                                        (r.get("tenant_id") or 1,
                                         r["email_address"], r.get("local_part"), r.get("domain"),
                                         r.get("codice_cliente"),
                                         r.get("seen_count") or 1,
                                         str(r["first_seen_at"]) if r.get("first_seen_at") else None,
                                         str(r["last_seen_at"]) if r.get("last_seen_at") else None),
                                    )
                                if conn.total_changes > 0:
                                    n += 1
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("import address_%s %s: %s", kind, r.get("email_address"), exc)
                counts[f"addresses_{kind}"] = n

        # 10. Error aggregations
        with pg.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT to_regclass('public.smtp_relay_error_aggregations') AS t"
            )
            if cur.fetchone()["t"]:
                where = "WHERE 1=1"
                params = []
                if tenant_id_filter is not None:
                    where += " AND tenant_id = %s"
                    params.append(tenant_id_filter)
                cur.execute(f"SELECT * FROM smtp_relay_error_aggregations {where}", params)
                n = 0
                for r in cur.fetchall():
                    if dry_run:
                        n += 1
                        continue
                    try:
                        storage.upsert_aggregation({
                            "name": r["name"],
                            "description": r.get("description"),
                            "match_from_regex": r.get("match_from_regex"),
                            "match_subject_regex": r.get("match_subject_regex"),
                            "match_body_regex": r.get("match_body_regex"),
                            "fingerprint_template": r.get("fingerprint_template"),
                            "threshold": r["threshold"],
                            "consecutive_only": bool(r.get("consecutive_only")),
                            "window_hours": r["window_hours"],
                            "reset_subject_regex": r.get("reset_subject_regex"),
                            "reset_from_regex": r.get("reset_from_regex"),
                            "ticket_settore": r.get("ticket_settore"),
                            "ticket_urgenza": r.get("ticket_urgenza"),
                            "ticket_codice_cliente": r.get("ticket_codice_cliente"),
                            "enabled": bool(r["enabled"]),
                            "priority": r.get("priority", 100),
                            "created_by": r.get("created_by") or "import",
                        }, tenant_id=r["tenant_id"])
                        n += 1
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("import aggregation %s: %s", r.get("name"), exc)
                counts["aggregations"] = n
            else:
                counts["aggregations"] = 0
    finally:
        pg.close()

    return counts


def main():
    parser = argparse.ArgumentParser(description="Import dati dal Stormshield Manager Postgres → SQLite standalone")
    parser.add_argument("--pg-host", default="localhost")
    parser.add_argument("--pg-port", type=int, default=5432)
    parser.add_argument("--pg-db", default="stormshield")
    parser.add_argument("--pg-user", default="stormshield")
    parser.add_argument("--pg-password", required=True)
    parser.add_argument("--tenant-id", type=int, default=None,
                        help="Importa solo un tenant_id specifico (default: tutti)")
    parser.add_argument("--dry-run", action="store_true", help="Non scrive, solo conta")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    print(f"Import da PostgreSQL {args.pg_host}:{args.pg_port}/{args.pg_db} → SQLite locale")
    if args.dry_run:
        print("(dry-run mode — niente scritture)")

    counts = import_data(
        pg_host=args.pg_host, pg_port=args.pg_port, pg_db=args.pg_db,
        pg_user=args.pg_user, pg_password=args.pg_password,
        tenant_id_filter=args.tenant_id,
        dry_run=args.dry_run,
    )
    print("\nRisultati:")
    for k, v in counts.items():
        print(f"  {k:20s} {v}")


if __name__ == "__main__":
    main()
