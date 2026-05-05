"""Provider postgres — modalita' parametrica + modalita' legacy.

Modalita' parametrica:
    config = {host, port, user, password, dbname, sslmode?}
    query  = SQL SELECT che ritorna 1 riga per cliente con colonne arbitrarie
    fetch() yield ogni riga come dict; il mapper traduce in canonico.

Modalita' legacy (sentinel `_use_legacy_pgconfig=true` in config):
    Riusa PgConfig.from_settings(storage) del vecchio postgres_source.py
    ed esegue le 5 query discovery defensive (clienti, customer_settings,
    client_domains, customer_aliases, smtp_relay_service_hours), aggregando
    in record canonici 1-per-cliente. Nessun mapping necessario
    (mapping_json deve avere `_legacy: true`).
"""
from __future__ import annotations

import logging
from typing import Any, Iterator

from .base import CustomerSyncProvider, FetchedRecord, ProviderConnectionError

logger = logging.getLogger(__name__)


def _import_psycopg2():
    try:
        import psycopg2
        import psycopg2.extras
        return psycopg2
    except ImportError as exc:
        raise ProviderConnectionError(
            "psycopg2 non installato (richiesto per provider postgres)"
        ) from exc


class PostgresProvider(CustomerSyncProvider):

    def __init__(self, *, config: dict[str, Any],
                 query: str | None, storage=None) -> None:
        self._config = config or {}
        self._query = query
        self._storage = storage  # serve per modalita' legacy

    # ---- public API ---------------------------------------------------------

    def is_legacy_mode(self) -> bool:
        return bool(self._config.get("_use_legacy_pgconfig"))

    def fetch(self) -> Iterator[FetchedRecord]:
        if self.is_legacy_mode():
            yield from self._fetch_legacy()
            return
        if not self._query:
            raise ValueError("PostgresProvider parametrico richiede query SQL")
        psycopg2 = _import_psycopg2()
        with self._connect_parametric(psycopg2) as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(self._query)
                for row in cur:
                    yield dict(row)

    def test_connection(self) -> dict[str, Any]:
        if self.is_legacy_mode():
            return self._test_legacy()
        psycopg2 = _import_psycopg2()
        try:
            with self._connect_parametric(psycopg2) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
                    cur.fetchone()
                    if self._query:
                        cur.execute(f"SELECT COUNT(*) FROM ({self._query}) AS _q")
                        sample_count = cur.fetchone()[0]
                    else:
                        sample_count = None
            return {"ok": True, "message": "Connessione OK",
                    "sample_count": sample_count}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": "Connessione fallita",
                    "error": str(exc)[:500]}

    def describe_schema(self) -> list[str]:
        if self.is_legacy_mode():
            return [
                "codcli", "ragione_sociale", "domains", "aliases",
                "contract_active", "tipologia_servizio", "contract_type",
                "contract_expiry", "timezone", "service_hours_json",
            ]
        if not self._query:
            return []
        psycopg2 = _import_psycopg2()
        try:
            with self._connect_parametric(psycopg2) as conn:
                with conn.cursor() as cur:
                    # Tecnica: eseguo la query con LIMIT 0 per leggere solo i nomi
                    cur.execute(f"SELECT * FROM ({self._query}) AS _q LIMIT 0")
                    return [d.name for d in cur.description]
        except Exception as exc:  # noqa: BLE001
            logger.warning("describe_schema postgres fallito: %s", exc)
            return []

    # ---- modalita' parametrica ----------------------------------------------

    def _connect_parametric(self, psycopg2):
        cfg = self._config
        return psycopg2.connect(
            host=cfg.get("host", "localhost"),
            port=int(cfg.get("port", 5432)),
            user=cfg.get("user", "postgres"),
            password=cfg.get("password", ""),
            dbname=cfg.get("dbname") or cfg.get("database", "postgres"),
            sslmode=cfg.get("sslmode") or "prefer",
            connect_timeout=10,
        )

    # ---- modalita' legacy: replica logica di postgres_source._do_sync -------

    def _legacy_pg_config(self):
        """Ricostruisce PgConfig esistente (env + UI Integrations)."""
        from ..customer_sources.postgres_source import PgConfig
        if self._storage is not None:
            return PgConfig.from_settings(self._storage)
        return PgConfig.from_env()

    def _legacy_connect(self, psycopg2, database: str):
        cfg = self._legacy_pg_config()
        # Per DB solution usa credenziali dedicate se presenti (caso 4.41/4.42)
        if database == cfg.solution_db and cfg.solution_user:
            user = cfg.solution_user
            password = cfg.solution_password or cfg.password
        else:
            user = cfg.user
            password = cfg.password
        return psycopg2.connect(
            host=cfg.host, port=cfg.port, database=database,
            user=user, password=password,
            sslmode=cfg.sslmode or "prefer",
            connect_timeout=10,
        )

    def _test_legacy(self) -> dict[str, Any]:
        psycopg2 = _import_psycopg2()
        cfg = self._legacy_pg_config()
        try:
            with self._legacy_connect(psycopg2, cfg.solution_db) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT COUNT(*) FROM clienti WHERE COALESCE(aescluso, FALSE)=FALSE")
                    n_solution = cur.fetchone()[0]
            with self._legacy_connect(psycopg2, cfg.stormshield_db) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT COUNT(*) FROM client_domains")
                    n_storm = cur.fetchone()[0]
            return {"ok": True,
                    "message": f"OK: {n_solution} clienti su solution, "
                               f"{n_storm} domini su stormshield",
                    "sample_count": n_solution}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": "Connessione legacy fallita",
                    "error": str(exc)[:500]}

    def _fetch_legacy(self) -> Iterator[FetchedRecord]:
        """Replica esatta della logica di postgres_source._do_sync, ma yield
        record canonici invece di scrivere direttamente.
        """
        psycopg2 = _import_psycopg2()
        rs_map = self._legacy_load_ragioni_sociali(psycopg2)
        settings_map = self._legacy_load_customer_settings(psycopg2)
        domains_map = self._legacy_load_client_domains(psycopg2)
        aliases_map = self._legacy_load_customer_aliases(psycopg2)
        sh_map = self._legacy_load_service_hours(psycopg2)

        codcli_set = set(rs_map.keys()) | set(settings_map.keys())
        for codcli, settings in list(settings_map.items()):
            if settings.get("exclude_from_list", False):
                codcli_set.discard(codcli)

        for codcli in codcli_set:
            settings = settings_map.get(codcli) or {
                "is_active": True,
                "exclude_from_list": False,
                "contract_expiry_date": None,
                "timezone": "Europe/Rome",
                "availability_type": None,
                "contract_type": None,
            }
            avail = settings.get("availability_type") or {}
            contract = settings.get("contract_type") or {}
            yield {
                "codcli": codcli,
                "ragione_sociale": rs_map.get(codcli, ""),
                "domains": domains_map.get(codcli, []),
                "aliases": aliases_map.get(codcli, []),
                "contract_active": bool(settings.get("is_active", True)),
                "tipologia_servizio": (avail.get("code") if avail else None) or "standard",
                "contract_type": (contract.get("code") if contract else None),
                "contract_expiry": settings.get("contract_expiry_date"),
                "timezone": settings.get("timezone", "Europe/Rome"),
                "service_hours_json": sh_map.get(codcli, {}),
            }

    # ----- query legacy difensive (estratte da postgres_source.py) -----------

    def _legacy_load_customer_settings(self, psycopg2) -> dict[str, dict[str, Any]]:
        cfg = self._legacy_pg_config()
        out: dict[str, dict[str, Any]] = {}
        try:
            with self._legacy_connect(psycopg2, cfg.solution_db) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT to_regclass('public.customer_settings') AS t")
                    if not cur.fetchone()[0]:
                        return {}
                    cur.execute(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name = 'customer_settings'"
                    )
                    cs_cols = {r[0] for r in cur.fetchall()}
                    cur.execute("SELECT to_regclass('public.customer_contract_types') AS t")
                    has_contract_types = bool(cur.fetchone()[0])
                    cur.execute(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name = 'customer_availability_types'"
                    )
                    av_cols = {r[0] for r in cur.fetchall()}

                    cs_part = ["cs.codcli", "cs.is_active"]
                    cs_part.append("cs.contract_expiry_date" if "contract_expiry_date" in cs_cols else "NULL")
                    cs_part.append("cs.timezone" if "timezone" in cs_cols else "NULL")
                    cs_part.append("COALESCE(cs.exclude_from_list, FALSE)" if "exclude_from_list" in cs_cols else "FALSE")
                    av_part = ["av.code"] if "code" in av_cols else ["NULL::text"]
                    av_part.append("av.description" if "description" in av_cols else "NULL::text")
                    if has_contract_types and "contract_type_id" in cs_cols:
                        contract_part = ["ct.code", "ct.description"]
                        contract_join = "LEFT JOIN customer_contract_types ct ON ct.id = cs.contract_type_id"
                    else:
                        contract_part = ["NULL::text", "NULL::text"]
                        contract_join = ""

                    sql = (
                        f"SELECT {', '.join(cs_part + av_part + contract_part)} "
                        f"FROM customer_settings cs "
                        f"LEFT JOIN customer_availability_types av ON av.id = cs.availability_type_id "
                        f"{contract_join}"
                    )
                    cur.execute(sql)
                    for r in cur.fetchall():
                        cc = (r[0] or "").strip().upper()
                        if not cc:
                            continue
                        expiry = r[2].isoformat() if r[2] and hasattr(r[2], "isoformat") else r[2]
                        out[cc] = {
                            "is_active": bool(r[1]),
                            "contract_expiry_date": expiry,
                            "timezone": r[3] or "Europe/Rome",
                            "exclude_from_list": bool(r[4]),
                            "availability_type": (
                                {"code": r[5], "description": r[6]} if r[5] else None
                            ),
                            "contract_type": (
                                {"code": r[7], "description": r[8]} if r[7] else None
                            ),
                        }
        except Exception as exc:  # noqa: BLE001
            logger.warning("legacy customer_settings load fallito: %s", exc)
        return out

    def _legacy_load_client_domains(self, psycopg2) -> dict[str, list[str]]:
        cfg = self._legacy_pg_config()
        out: dict[str, list[str]] = {}
        try:
            with self._legacy_connect(psycopg2, cfg.stormshield_db) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT to_regclass('public.client_domains') AS t")
                    if not cur.fetchone()[0]:
                        return {}
                    cur.execute(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name = 'client_domains'"
                    )
                    cols = {r[0] for r in cur.fetchall()}
                    where = ["dominio IS NOT NULL"]
                    if "excluded" in cols:
                        where.append("COALESCE(excluded, FALSE) = FALSE")
                    sql = ("SELECT codice_cliente, dominio FROM client_domains "
                           f"WHERE {' AND '.join(where)}")
                    cur.execute(sql)
                    for codcli, dominio in cur.fetchall():
                        cc = (codcli or "").strip().upper()
                        d = (dominio or "").strip().lower()
                        if cc and d:
                            out.setdefault(cc, []).append(d)
        except Exception as exc:  # noqa: BLE001
            logger.warning("legacy client_domains load fallito: %s", exc)
        return out

    def _legacy_load_customer_aliases(self, psycopg2) -> dict[str, list[str]]:
        cfg = self._legacy_pg_config()
        out: dict[str, list[str]] = {}
        try:
            with self._legacy_connect(psycopg2, cfg.solution_db) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT to_regclass('public.customer_aliases') AS t")
                    if not cur.fetchone()[0]:
                        return {}
                    cur.execute(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name = 'customer_aliases'"
                    )
                    cols = {r[0] for r in cur.fetchall()}
                    cc_col = "codcli" if "codcli" in cols else (
                              "codice_cliente" if "codice_cliente" in cols else None)
                    al_col = "alias_name" if "alias_name" in cols else (
                              "alias" if "alias" in cols else None)
                    if cc_col is None or al_col is None:
                        logger.info("legacy customer_aliases skip: schema inatteso")
                        return {}
                    where_clauses = [f"{al_col} IS NOT NULL"]
                    if "is_searchable" in cols:
                        where_clauses.append("COALESCE(is_searchable, TRUE) = TRUE")
                    elif "enabled" in cols:
                        where_clauses.append("COALESCE(enabled, TRUE) = TRUE")
                    sql = (f"SELECT {cc_col}, {al_col} FROM customer_aliases "
                           f"WHERE {' AND '.join(where_clauses)}")
                    cur.execute(sql)
                    for codcli, alias in cur.fetchall():
                        cc = (codcli or "").strip().upper()
                        a = (alias or "").strip().lower()
                        if cc and a:
                            out.setdefault(cc, []).append(a)
        except Exception as exc:  # noqa: BLE001
            logger.info("legacy customer_aliases load skip: %s", exc)
        return out

    def _legacy_load_ragioni_sociali(self, psycopg2) -> dict[str, str]:
        cfg = self._legacy_pg_config()
        out: dict[str, str] = {}
        try:
            with self._legacy_connect(psycopg2, cfg.solution_db) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT to_regclass('public.clienti') AS t")
                    if not cur.fetchone()[0]:
                        return {}
                    cur.execute(
                        "SELECT aconto, ragione_sociale FROM clienti "
                        "WHERE aconto IS NOT NULL AND ragione_sociale IS NOT NULL "
                        "AND COALESCE(aescluso, FALSE) = FALSE"
                    )
                    for codcli, rs in cur.fetchall():
                        cc = (codcli or "").strip().upper()
                        if cc:
                            out[cc] = (rs or "").strip()
        except Exception as exc:  # noqa: BLE001
            logger.warning("legacy ragioni_sociali load fallito: %s", exc)
        return out

    def _legacy_load_service_hours(self, psycopg2) -> dict[str, dict[str, Any]]:
        cfg = self._legacy_pg_config()
        out: dict[str, dict[str, Any]] = {}
        try:
            with self._legacy_connect(psycopg2, cfg.stormshield_db) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT to_regclass('public.smtp_relay_service_hours') AS t")
                    if cur.fetchone()[0]:
                        cur.execute(
                            "SELECT codice_cliente, profile, timezone, schedule, holidays "
                            "FROM smtp_relay_service_hours WHERE enabled = TRUE"
                        )
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
                    cur.execute("SELECT to_regclass('public.customer_service_hours') AS t")
                    if not cur.fetchone()[0]:
                        return {}
                    cur.execute(
                        "SELECT 1 FROM information_schema.columns "
                        "WHERE table_name='customer_service_hours' AND column_name='holidays'"
                    )
                    has_holidays = cur.fetchone() is not None
                    if has_holidays:
                        cur.execute("SELECT codice_cliente, profile, timezone, schedule, holidays "
                                    "FROM customer_service_hours")
                    else:
                        cur.execute("SELECT codice_cliente, profile, timezone, schedule, NULL "
                                    "FROM customer_service_hours")
                    for codcli, profile, tz, sched, hols in cur.fetchall():
                        cc = (codcli or "").strip().upper()
                        if cc:
                            out[cc] = {
                                "profile": profile,
                                "timezone": tz or "Europe/Rome",
                                "schedule": sched, "holidays": hols or [],
                            }
        except Exception as exc:  # noqa: BLE001
            logger.warning("legacy service_hours load fallito: %s", exc)
        return out
