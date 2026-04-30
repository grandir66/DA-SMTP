"""Customer source backend `postgres`: legge dalla cache locale popolata
da un sync periodico verso i DB del gestionale (`stormshield` + `solution`).

Architettura:
  ┌──────────────┐  sync ogni X min   ┌───────────────────┐
  │ PG stormshield│ ──────────────────▶│ admin.db          │
  │ PG solution   │                    │ customers_pg_cache│
  └──────────────┘                    └────────┬──────────┘
                                                │ runtime
                                                ▼
                                       list_customers()
                                       get_by_codcli()

Vantaggi:
  - Zero dipendenza da PG durante il match (anche se PG va giù il relay
    continua con l'ultimo snapshot).
  - Independent dal manager Stormshield (svincolo completo).
  - Audit del sync in `customers_pg_sync_log`.

Configurazione: secrets.env
  GESTIONALE_PG_HOST=192.168.4.1
  GESTIONALE_PG_PORT=5432
  GESTIONALE_PG_USER=stormshield
  GESTIONALE_PG_PASSWORD=...
  GESTIONALE_PG_STORMSHIELD_DB=stormshield
  GESTIONALE_PG_SOLUTION_DB=solution
  GESTIONALE_PG_SYNC_INTERVAL_SEC=300
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .base import Customer, CustomerSource

logger = logging.getLogger(__name__)


@dataclass
class PgConfig:
    host: str = "localhost"
    port: int = 5432
    # Credenziali default (per DB stormshield)
    user: str = "stormshield"
    password: str = ""
    stormshield_db: str = "stormshield"
    # Credenziali separate per DB solution (clienti) — fallback alle default se vuote
    solution_db: str = "solution"
    solution_user: str = ""
    solution_password: str = ""
    sslmode: str = "prefer"
    sync_interval_sec: int = 300

    @classmethod
    def from_env(cls) -> "PgConfig":
        return cls(
            host=os.environ.get("GESTIONALE_PG_HOST", "localhost"),
            port=int(os.environ.get("GESTIONALE_PG_PORT", "5432")),
            user=os.environ.get("GESTIONALE_PG_USER", "stormshield"),
            password=os.environ.get("GESTIONALE_PG_PASSWORD", ""),
            stormshield_db=os.environ.get("GESTIONALE_PG_STORMSHIELD_DB", "stormshield"),
            solution_db=os.environ.get("GESTIONALE_PG_SOLUTION_DB", "solution"),
            solution_user=os.environ.get("GESTIONALE_PG_SOLUTION_USER", ""),
            solution_password=os.environ.get("GESTIONALE_PG_SOLUTION_PASSWORD", ""),
            sslmode=os.environ.get("GESTIONALE_PG_SSLMODE", "prefer"),
            sync_interval_sec=int(os.environ.get("GESTIONALE_PG_SYNC_INTERVAL_SEC", "300")),
        )

    @classmethod
    def from_settings(cls, storage) -> "PgConfig":
        """Carica config dal DB settings (UI integrations) con fallback env."""
        env = cls.from_env()
        def _get(k: str, default: str) -> str:
            v = storage.get_setting(k)
            return v if v is not None else default
        try:
            return cls(
                host=_get("customer_source.pg.host", env.host),
                port=int(_get("customer_source.pg.port", str(env.port))),
                user=_get("customer_source.pg.user", env.user),
                password=_get("customer_source.pg.password", env.password),
                stormshield_db=_get("customer_source.pg.stormshield_db", env.stormshield_db),
                solution_db=_get("customer_source.pg.solution_db", env.solution_db),
                solution_user=_get("customer_source.pg.solution_user", env.solution_user),
                solution_password=_get("customer_source.pg.solution_password", env.solution_password),
                sslmode=_get("customer_source.pg.sslmode", env.sslmode),
                sync_interval_sec=int(_get("customer_source.pg.sync_interval_sec", str(env.sync_interval_sec))),
            )
        except Exception:  # noqa: BLE001
            return env


class PostgresCustomerSource(CustomerSource):
    """Customer source backed by local cache, populated by periodic PG sync."""

    def __init__(self, app_config, storage=None) -> None:
        # Se ho lo storage, leggo prima da settings (UI integrations);
        # altrimenti fallback env.
        if storage is not None:
            self._cfg = PgConfig.from_settings(storage)
        else:
            self._cfg = PgConfig.from_env()
        self._db_path = app_config.db_path
        self._storage = storage
        self._sync_thread: threading.Thread | None = None
        self._sync_stop = threading.Event()
        self._lock = threading.Lock()

    # ============================================================ Pubblica

    def list_customers(self) -> list[Customer]:
        rows = self._fetch_cache()
        return [self._row_to_customer(r) for r in rows]

    def get_by_codcli(self, codcli: str) -> Customer | None:
        rows = self._fetch_cache(codcli=codcli)
        if not rows:
            return None
        return self._row_to_customer(rows[0])

    def health(self) -> dict[str, Any]:
        """Stato del customer source (per UI / API health)."""
        import sqlite3
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        try:
            r_count = conn.execute("SELECT COUNT(*) FROM customers_pg_cache").fetchone()[0]
            r_last_sync = conn.execute(
                "SELECT MAX(last_synced_at) FROM customers_pg_cache"
            ).fetchone()[0]
            r_log = conn.execute(
                "SELECT * FROM customers_pg_sync_log ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
            last_log = dict(r_log) if r_log else None
        finally:
            conn.close()

        # Stato age dell'ultimo sync
        age_seconds = None
        if r_last_sync:
            try:
                last_dt = datetime.fromisoformat(r_last_sync.replace("Z", "+00:00"))
                if last_dt.tzinfo is None:
                    last_dt = last_dt.replace(tzinfo=timezone.utc)
                age_seconds = int((datetime.now(timezone.utc) - last_dt).total_seconds())
            except (ValueError, TypeError):
                pass

        return {
            "backend": "postgres",
            "host": self._cfg.host,
            "port": self._cfg.port,
            "stormshield_db": self._cfg.stormshield_db,
            "solution_db": self._cfg.solution_db,
            "cache_count": r_count,
            "last_sync": r_last_sync,
            "last_sync_age_seconds": age_seconds,
            "last_sync_log": last_log,
            "sync_interval_sec": self._cfg.sync_interval_sec,
            "sync_thread_alive": (self._sync_thread is not None
                                    and self._sync_thread.is_alive()),
            "stale_warning": (age_seconds is not None
                              and age_seconds > self._cfg.sync_interval_sec * 3),
        }

    # ============================================================ Sync

    def start_sync_thread(self) -> None:
        """Avvia il thread di sync periodico in background. Idempotente."""
        with self._lock:
            if self._sync_thread is not None and self._sync_thread.is_alive():
                return
            self._sync_stop.clear()
            self._sync_thread = threading.Thread(
                target=self._sync_loop, name="pg-customer-sync", daemon=True,
            )
            self._sync_thread.start()
            logger.info("PG customer sync thread avviato (interval=%ds)",
                         self._cfg.sync_interval_sec)

    def stop_sync_thread(self) -> None:
        self._sync_stop.set()

    def sync_now(self, triggered_by: str = "manual") -> dict[str, Any]:
        """Esegue un sync sincrono (bloccante). Ritorna report."""
        return self._do_sync(triggered_by=triggered_by)

    def _sync_loop(self) -> None:
        """Loop infinito che chiama _do_sync ogni N secondi."""
        # Primo sync subito allo startup
        try:
            self._do_sync(triggered_by="startup")
        except Exception as exc:  # noqa: BLE001
            logger.exception("Sync iniziale PG fallito: %s", exc)
        while not self._sync_stop.wait(timeout=self._cfg.sync_interval_sec):
            try:
                self._do_sync(triggered_by="scheduled")
            except Exception as exc:  # noqa: BLE001
                logger.exception("Sync periodico PG fallito: %s", exc)

    def _do_sync(self, triggered_by: str) -> dict[str, Any]:
        """Esegue un sync completo da PG → admin.db. Idempotente."""
        import sqlite3
        try:
            import psycopg2
            import psycopg2.extras
        except ImportError:
            logger.error("psycopg2 non installato — impossibile fare sync PG")
            return {"success": False, "error": "psycopg2 non installato"}

        started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        t0 = time.monotonic()
        report: dict[str, Any] = {
            "success": False, "rows_synced": 0, "rows_removed": 0,
            "error": None, "started_at": started_at, "triggered_by": triggered_by,
        }

        sqlite_conn = sqlite3.connect(self._db_path, timeout=10.0)
        try:
            # Apre log entry
            cur = sqlite_conn.execute(
                """INSERT INTO customers_pg_sync_log
                       (started_at, triggered_by, success)
                   VALUES (?, ?, 0)""",
                (started_at, triggered_by),
            )
            log_id = cur.lastrowid
            sqlite_conn.commit()

            # === Carico tutto da PG ===
            # Base: TUTTI i clienti dalla tabella `clienti` (DB solution).
            # Se `customer_settings` è vuota (dev), usiamo defaults (is_active=True).
            rs_map = self._load_ragioni_sociali(psycopg2)
            settings_map = self._load_customer_settings(psycopg2)  # may be empty
            domains_map = self._load_client_domains(psycopg2)
            aliases_map = self._load_customer_aliases(psycopg2)
            sh_map = self._load_service_hours(psycopg2)

            # === Costruisco i record ===
            now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
            # Universo: union di clienti (rs_map) + clienti con settings (settings_map).
            # Se settings ha exclude_from_list/is_active=FALSE → escludo dal cache.
            new_codcli_set = set(rs_map.keys()) | set(settings_map.keys())
            # Filtra clienti esplicitamente disabilitati in customer_settings
            for codcli, settings in list(settings_map.items()):
                if not settings.get("is_active", True):
                    new_codcli_set.discard(codcli)

            with sqlite_conn:
                for codcli in new_codcli_set:
                    # Settings: fallback sicuro se cliente non ha riga in customer_settings
                    settings = settings_map.get(codcli) or {
                        "is_active": True,
                        "contract_expiry_date": None,
                        "contract_notes": None,
                        "timezone": "Europe/Rome",
                        "availability_type": None,
                        "contract_type": None,
                    }
                    domains = domains_map.get(codcli, [])
                    aliases = aliases_map.get(codcli, [])
                    rs = rs_map.get(codcli, "")
                    sh = sh_map.get(codcli, {})

                    avail = settings.get("availability_type") or {}
                    contract = settings.get("contract_type") or {}

                    raw = {
                        "codcli": codcli,
                        "ragione_sociale": rs,
                        "domains": domains,
                        "aliases": aliases,
                        "contract_active": settings.get("is_active", True),
                        "contract_expiry_date": settings.get("contract_expiry_date"),
                        "tipologia_servizio": avail.get("code") or "standard",
                        "availability_type": avail,
                        "contract_type": contract,
                        "service_hours": sh,
                        "timezone": settings.get("timezone", "Europe/Rome"),
                    }

                    sqlite_conn.execute(
                        """INSERT INTO customers_pg_cache
                               (codcli, ragione_sociale, domains_json, aliases_json,
                                contract_active, tipologia_servizio, service_hours_json,
                                contract_expiry, timezone, raw_json, last_synced_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                           ON CONFLICT(codcli) DO UPDATE SET
                               ragione_sociale = excluded.ragione_sociale,
                               domains_json = excluded.domains_json,
                               aliases_json = excluded.aliases_json,
                               contract_active = excluded.contract_active,
                               tipologia_servizio = excluded.tipologia_servizio,
                               service_hours_json = excluded.service_hours_json,
                               contract_expiry = excluded.contract_expiry,
                               timezone = excluded.timezone,
                               raw_json = excluded.raw_json,
                               last_synced_at = excluded.last_synced_at""",
                        (
                            codcli, rs,
                            json.dumps(domains, ensure_ascii=False),
                            json.dumps(aliases, ensure_ascii=False),
                            1 if settings.get("is_active", True) else 0,
                            avail.get("code") or "standard",
                            json.dumps(sh, ensure_ascii=False) if sh else None,
                            settings.get("contract_expiry_date"),
                            settings.get("timezone", "Europe/Rome"),
                            json.dumps(raw, ensure_ascii=False),
                            now_iso,
                        ),
                    )
                    report["rows_synced"] += 1

                # Cleanup: rimuovi codcli che non sono più in PG
                placeholders = ",".join("?" * len(new_codcli_set)) if new_codcli_set else "''"
                if new_codcli_set:
                    cur = sqlite_conn.execute(
                        f"DELETE FROM customers_pg_cache WHERE codcli NOT IN ({placeholders})",
                        list(new_codcli_set),
                    )
                else:
                    cur = sqlite_conn.execute("DELETE FROM customers_pg_cache")
                report["rows_removed"] = cur.rowcount or 0

                # Aggiorna log entry
                duration_ms = int((time.monotonic() - t0) * 1000)
                sqlite_conn.execute(
                    """UPDATE customers_pg_sync_log SET
                            finished_at = ?, duration_ms = ?,
                            rows_synced = ?, rows_removed = ?, success = 1
                        WHERE id = ?""",
                    (datetime.now(timezone.utc).isoformat(timespec="seconds"),
                     duration_ms, report["rows_synced"], report["rows_removed"], log_id),
                )

            report["success"] = True
            report["duration_ms"] = duration_ms
            logger.info("PG sync OK: %d clienti aggiornati, %d rimossi (%dms)",
                         report["rows_synced"], report["rows_removed"], duration_ms)
        except Exception as exc:  # noqa: BLE001
            report["error"] = str(exc)
            logger.exception("PG sync fallito: %s", exc)
            try:
                sqlite_conn.execute(
                    """UPDATE customers_pg_sync_log SET
                            finished_at = ?, success = 0, error_message = ?
                        WHERE id = ?""",
                    (datetime.now(timezone.utc).isoformat(timespec="seconds"),
                     str(exc)[:1000], log_id),
                )
                sqlite_conn.commit()
            except Exception:  # noqa: BLE001
                pass
        finally:
            sqlite_conn.close()
        return report

    # ============================================================ PG queries

    def _pg_connect(self, psycopg2, database: str):
        # Per DB solution usa credenziali dedicate (se valorizzate); fallback
        # alle default. Necessario perché su molti setup `clienti` è di
        # proprietà di `solution_user` e non leggibile da `stormshield`.
        if database == self._cfg.solution_db and self._cfg.solution_user:
            user = self._cfg.solution_user
            password = self._cfg.solution_password or self._cfg.password
        else:
            user = self._cfg.user
            password = self._cfg.password
        return psycopg2.connect(
            host=self._cfg.host, port=self._cfg.port, database=database,
            user=user, password=password,
            sslmode=self._cfg.sslmode or "prefer",
            connect_timeout=10,
        )

    def _load_customer_settings(self, psycopg2) -> dict[str, dict[str, Any]]:
        """Solo clienti is_active=TRUE AND NOT exclude_from_list.

        Schema introspettivo: usa solo le colonne che esistono davvero nel DB
        (varianti su customer_settings: con/senza contract_*, con/senza timezone).
        """
        out: dict[str, dict[str, Any]] = {}
        try:
            with self._pg_connect(psycopg2, self._cfg.stormshield_db) as conn:
                with conn.cursor() as cur:
                    cur.execute("""SELECT to_regclass('public.customer_settings') AS t""")
                    if not cur.fetchone()[0]:
                        return {}
                    # Discovery delle colonne disponibili
                    cur.execute("""
                        SELECT column_name FROM information_schema.columns
                         WHERE table_name = 'customer_settings'
                    """)
                    cs_cols = {r[0] for r in cur.fetchall()}
                    cur.execute("SELECT to_regclass('public.customer_contract_types') AS t")
                    has_contract_types = bool(cur.fetchone()[0])

                    # Discovery anche su customer_availability_types
                    cur.execute("""
                        SELECT column_name FROM information_schema.columns
                         WHERE table_name = 'customer_availability_types'
                    """)
                    av_cols = {r[0] for r in cur.fetchall()}

                    # Costruzione SELECT difensiva
                    cs_part = ["cs.codcli", "cs.is_active"]
                    cs_part.append("cs.contract_expiry_date" if "contract_expiry_date" in cs_cols else "NULL")
                    cs_part.append("cs.timezone" if "timezone" in cs_cols else "NULL")
                    av_part = ["av.code"] if "code" in av_cols else ["NULL::text"]
                    av_part.append("av.description" if "description" in av_cols else "NULL::text")
                    contract_part = []
                    if has_contract_types and "contract_type_id" in cs_cols:
                        contract_part = ["ct.code", "ct.description"]
                        contract_join = "LEFT JOIN customer_contract_types ct ON ct.id = cs.contract_type_id"
                    else:
                        contract_part = ["NULL::text", "NULL::text"]
                        contract_join = ""

                    sql = f"""
                        SELECT {', '.join(cs_part + av_part + contract_part)}
                          FROM customer_settings cs
                          LEFT JOIN customer_availability_types av ON av.id = cs.availability_type_id
                          {contract_join}
                         WHERE cs.is_active = TRUE
                           AND COALESCE(cs.exclude_from_list, FALSE) = FALSE
                    """
                    cur.execute(sql)
                    for r in cur.fetchall():
                        cc = (r[0] or "").strip().upper()
                        if not cc:
                            continue
                        expiry = r[2].isoformat() if r[2] and hasattr(r[2], "isoformat") else r[2]
                        out[cc] = {
                            "is_active": bool(r[1]),
                            "contract_expiry_date": expiry,
                            "contract_notes": None,
                            "timezone": r[3] or "Europe/Rome",
                            "availability_type": (
                                {"code": r[4], "description": r[5]} if r[4] else None
                            ),
                            "contract_type": (
                                {"code": r[6], "description": r[7]} if r[6] else None
                            ),
                        }
        except Exception as exc:  # noqa: BLE001
            logger.warning("customer_settings load fallito: %s", exc)
        return out

    def _load_client_domains(self, psycopg2) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        try:
            with self._pg_connect(psycopg2, self._cfg.stormshield_db) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT to_regclass('public.client_domains') AS t")
                    if not cur.fetchone()[0]:
                        return {}
                    cur.execute("""
                        SELECT codice_cliente, dominio FROM client_domains
                         WHERE COALESCE(excluded, FALSE) = FALSE AND dominio IS NOT NULL
                    """)
                    for codcli, dominio in cur.fetchall():
                        cc = (codcli or "").strip().upper()
                        d = (dominio or "").strip().lower()
                        if cc and d:
                            out.setdefault(cc, []).append(d)
        except Exception as exc:  # noqa: BLE001
            logger.warning("client_domains load fallito: %s", exc)
        return out

    def _load_customer_aliases(self, psycopg2) -> dict[str, list[str]]:
        """Alias degli account specifici (es. assistenza@cliente.it)."""
        out: dict[str, list[str]] = {}
        try:
            with self._pg_connect(psycopg2, self._cfg.stormshield_db) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT to_regclass('public.customer_aliases') AS t")
                    if not cur.fetchone()[0]:
                        return {}
                    cur.execute("""
                        SELECT codice_cliente, alias FROM customer_aliases
                         WHERE COALESCE(enabled, TRUE) = TRUE AND alias IS NOT NULL
                    """)
                    for codcli, alias in cur.fetchall():
                        cc = (codcli or "").strip().upper()
                        a = (alias or "").strip().lower()
                        if cc and a:
                            out.setdefault(cc, []).append(a)
        except Exception as exc:  # noqa: BLE001
            logger.info("customer_aliases load skip: %s", exc)
        return out

    def _load_ragioni_sociali(self, psycopg2) -> dict[str, str]:
        out: dict[str, str] = {}
        try:
            with self._pg_connect(psycopg2, self._cfg.solution_db) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT to_regclass('public.clienti') AS t")
                    if not cur.fetchone()[0]:
                        return {}
                    cur.execute("""
                        SELECT aconto, ragione_sociale FROM clienti
                         WHERE aconto IS NOT NULL AND ragione_sociale IS NOT NULL
                           AND COALESCE(aescluso, FALSE) = FALSE
                    """)
                    for codcli, rs in cur.fetchall():
                        cc = (codcli or "").strip().upper()
                        if cc:
                            out[cc] = (rs or "").strip()
        except Exception as exc:  # noqa: BLE001
            logger.warning("ragioni_sociali load fallito: %s", exc)
        return out

    def _load_service_hours(self, psycopg2) -> dict[str, dict[str, Any]]:
        """smtp_relay_service_hours (preferito) con fallback a customer_service_hours."""
        out: dict[str, dict[str, Any]] = {}
        try:
            with self._pg_connect(psycopg2, self._cfg.stormshield_db) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT to_regclass('public.smtp_relay_service_hours') AS t")
                    if cur.fetchone()[0]:
                        cur.execute("""
                            SELECT codice_cliente, profile, timezone, schedule, holidays
                              FROM smtp_relay_service_hours
                             WHERE enabled = TRUE
                        """)
                        for codcli, profile, tz, sched, hols in cur.fetchall():
                            cc = (codcli or "").strip().upper()
                            if cc:
                                out[cc] = {
                                    "profile": profile,
                                    "timezone": tz or "Europe/Rome",
                                    "schedule": sched, "holidays": hols or [],
                                }
                    if out:
                        return out
                    # Fallback IMAP table (senza holidays nella stessa tabella —
                    # `customer_service_hours` non ha sempre la colonna).
                    cur.execute("SELECT to_regclass('public.customer_service_hours') AS t")
                    if not cur.fetchone()[0]:
                        return {}
                    # Verifica presenza colonna holidays
                    cur.execute("""
                        SELECT 1 FROM information_schema.columns
                         WHERE table_name='customer_service_hours' AND column_name='holidays'
                    """)
                    has_holidays = cur.fetchone() is not None
                    if has_holidays:
                        cur.execute("""SELECT codice_cliente, profile, timezone, schedule, holidays
                                         FROM customer_service_hours""")
                    else:
                        cur.execute("""SELECT codice_cliente, profile, timezone, schedule, NULL
                                         FROM customer_service_hours""")
                    for codcli, profile, tz, sched, hols in cur.fetchall():
                        cc = (codcli or "").strip().upper()
                        if cc:
                            out[cc] = {
                                "profile": profile,
                                "timezone": tz or "Europe/Rome",
                                "schedule": sched, "holidays": hols or [],
                            }
        except Exception as exc:  # noqa: BLE001
            logger.warning("service_hours load fallito: %s", exc)
        return out

    # ============================================================ Helpers

    def _fetch_cache(self, codcli: str | None = None) -> list[dict]:
        import sqlite3
        conn = sqlite3.connect(self._db_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        try:
            if codcli:
                rows = conn.execute(
                    "SELECT * FROM customers_pg_cache WHERE codcli = ?",
                    (codcli.strip().upper(),)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM customers_pg_cache ORDER BY ragione_sociale"
                ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def _row_to_customer(self, r: dict) -> Customer:
        try:
            domains = json.loads(r.get("domains_json") or "[]")
        except (TypeError, ValueError):
            domains = []
        try:
            aliases = json.loads(r.get("aliases_json") or "[]")
        except (TypeError, ValueError):
            aliases = []
        try:
            sh = json.loads(r.get("service_hours_json") or "{}") if r.get("service_hours_json") else {}
        except (TypeError, ValueError):
            sh = {}
        return Customer(
            codice_cliente=r["codcli"],
            ragione_sociale=r.get("ragione_sociale") or "",
            domains=domains,
            aliases=aliases,
            is_active=bool(r.get("contract_active", 1)),
            tipologia_servizio=r.get("tipologia_servizio") or "standard",
            holidays=(sh.get("holidays") or []),
            schedule=(sh.get("schedule") or None),
            contract_expiry_date=r.get("contract_expiry"),
            contract_type=None,
            profile_description=None,
        )
