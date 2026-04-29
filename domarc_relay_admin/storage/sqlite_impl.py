"""Implementazione SQLite di `Storage`.

WAL mode + foreign_keys ON. Uso `sqlite3.Row` per accesso dict-like.
Migration runner: legge i file `migrations/00X_*.sqlite.sql` e tiene traccia in
tabella `_migrations` (versione applicata).

NOTA: questo è il backend default per la v1.0 (target PMI/MSP small).
PostgreSQL è opt-in per chi vuole HA (vedi `postgres_impl.py`).
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .base import Storage

logger = logging.getLogger(__name__)


class SqliteStorage(Storage):
    def __init__(self, db_path: str):
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._path), check_same_thread=False, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    def _init_db(self) -> None:
        # Tabella interna versioning
        with self._connect() as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS _migrations (
                       version INTEGER PRIMARY KEY,
                       applied_at TEXT NOT NULL DEFAULT (datetime('now')),
                       checksum TEXT
                   )"""
            )
        applied = self.apply_migrations()
        if applied:
            logger.info("SqliteStorage: applicate %d migrazioni", applied)
        logger.info("SqliteStorage init: %s (schema v%d)", self._path, self.schema_version())

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    # =================================================== MIGRATION RUNNER ==

    def apply_migrations(self) -> int:
        """Applica migration files non ancora applicati. Ritorna numero applicate."""
        migrations_dir = Path(__file__).parent.parent / "migrations"
        if not migrations_dir.exists():
            return 0
        # File: 001_initial.sqlite.sql, 002_*.sqlite.sql, ...
        files = sorted(migrations_dir.glob("*.sqlite.sql"))
        applied_count = 0
        with self._connect() as conn:
            cur = conn.execute("SELECT version FROM _migrations")
            already = {int(r[0]) for r in cur.fetchall()}
        for f in files:
            try:
                ver = int(f.name.split("_", 1)[0])
            except ValueError:
                logger.warning("Migration file con nome invalido: %s", f.name)
                continue
            if ver in already:
                continue
            sql = f.read_text(encoding="utf-8")
            with self._connect() as conn:
                conn.executescript(sql)
                conn.execute("INSERT INTO _migrations (version) VALUES (?)", (ver,))
                conn.commit()
            logger.info("Migration %d (%s) applicata", ver, f.name)
            applied_count += 1
        return applied_count

    def schema_version(self) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT MAX(version) FROM _migrations").fetchone()
            return int(row[0] or 0)

    # =========================================================== HEALTH ===

    def health(self) -> dict[str, Any]:
        with self._connect() as conn:
            try:
                v = self.schema_version()
                row = conn.execute(
                    "SELECT MAX(applied_at) FROM _migrations"
                ).fetchone()
                tenants = conn.execute("SELECT COUNT(*) FROM tenants").fetchone()[0]
                events = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
                return {
                    "ok": True,
                    "backend": "sqlite",
                    "path": str(self._path),
                    "schema_version": v,
                    "last_migration_at": str(row[0]) if row and row[0] else None,
                    "tenants_count": int(tenants or 0),
                    "events_count": int(events or 0),
                }
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "backend": "sqlite", "error": str(exc)}

    # =========================================================== TENANTS ===

    def list_tenants(self, *, only_enabled: bool | None = None,
                     search: str | None = None) -> list[dict[str, Any]]:
        where: list[str] = []
        params: list[Any] = []
        if only_enabled is True:
            where.append("enabled = 1")
        elif only_enabled is False:
            where.append("enabled = 0")
        if search:
            where.append("(LOWER(codice) LIKE ? OR LOWER(ragione_sociale) LIKE ?)")
            like = f"%{search.lower()}%"
            params.extend([like, like])
        where_sql = "WHERE " + " AND ".join(where) if where else ""
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM tenants {where_sql} ORDER BY ragione_sociale", params
            ).fetchall()
            return [dict(r) for r in rows]

    def get_tenant(self, tenant_id: int) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM tenants WHERE id = ?", (tenant_id,)
            ).fetchone()
            return dict(row) if row else None

    def get_tenant_by_codice(self, codice: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM tenants WHERE codice = ?", ((codice or "").strip().upper(),)
            ).fetchone()
            return dict(row) if row else None

    def upsert_tenant(self, data: dict[str, Any]) -> int:
        codice = (data.get("codice") or "").strip().upper()
        if not codice:
            raise ValueError("codice obbligatorio")
        rs = (data.get("ragione_sociale") or "").strip()
        if not rs:
            raise ValueError("ragione_sociale obbligatoria")
        tid = data.get("id")
        with self.transaction() as conn:
            if tid:
                conn.execute(
                    """UPDATE tenants SET
                          codice = ?, ragione_sociale = ?, description = ?,
                          contract_active = ?, enabled = ?, customer_source_config = ?,
                          updated_at = datetime('now')
                       WHERE id = ?""",
                    (codice, rs, data.get("description"),
                     1 if data.get("contract_active", True) else 0,
                     1 if data.get("enabled", True) else 0,
                     json.dumps(data["customer_source_config"]) if data.get("customer_source_config") else None,
                     int(tid)),
                )
                return int(tid)
            cur = conn.execute(
                """INSERT INTO tenants (codice, ragione_sociale, description,
                                        contract_active, enabled, customer_source_config, created_by)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (codice, rs, data.get("description"),
                 1 if data.get("contract_active", True) else 0,
                 1 if data.get("enabled", True) else 0,
                 json.dumps(data["customer_source_config"]) if data.get("customer_source_config") else None,
                 (data.get("created_by") or "ui")[:100]),
            )
            return int(cur.lastrowid or 0)

    def delete_tenant(self, tenant_id: int) -> None:
        if int(tenant_id) == 1:
            raise ValueError("Tenant default DOMARC (id=1) non eliminabile")
        with self.transaction() as conn:
            for tbl in ("rules", "reply_templates", "events"):
                row = conn.execute(
                    f"SELECT COUNT(*) FROM {tbl} WHERE tenant_id = ?", (tenant_id,)
                ).fetchone()
                if row[0] > 0:
                    raise ValueError(f"Tenant non eliminabile: {row[0]} record in {tbl}")
            conn.execute("DELETE FROM tenants WHERE id = ?", (tenant_id,))

    # =========================================================== USERS ====

    def list_users(self, *, tenant_id: int | None = None) -> list[dict[str, Any]]:
        """Lista utenti.

        Se `tenant_id` valorizzato: filtra solo utenti di quel tenant + i superadmin
        (che hanno tenant_id NULL e sono visibili a chiunque amministri un tenant
        per chiarezza). Senza filtro: tutti.
        """
        with self._connect() as conn:
            if tenant_id is not None:
                rows = conn.execute(
                    "SELECT id, username, role, full_name, email, enabled, "
                    "tenant_id, created_at, last_login_at "
                    "FROM users WHERE tenant_id = ? OR tenant_id IS NULL ORDER BY username",
                    (int(tenant_id),),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT id, username, role, full_name, email, enabled, "
                    "tenant_id, created_at, last_login_at "
                    "FROM users ORDER BY username"
                ).fetchall()
            return [dict(r) for r in rows]

    def get_user(self, user_id: int) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE id = ?", (int(user_id),)).fetchone()
            return dict(row) if row else None

    def get_user_by_username(self, username: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE username = ? AND enabled = 1",
                (username,),
            ).fetchone()
            return dict(row) if row else None

    def upsert_user(self, data: dict[str, Any]) -> int:
        username = (data.get("username") or "").strip()
        if not username:
            raise ValueError("username obbligatorio")
        role = (data.get("role") or "readonly").strip()
        if role not in ("superadmin", "admin", "tech", "readonly"):
            raise ValueError(f"role non valido: {role}")
        # Validazione tenant_id: superadmin → NULL; altri → obbligatorio
        tenant_id = data.get("tenant_id")
        if role == "superadmin":
            tenant_id = None
        else:
            if not tenant_id:
                raise ValueError(f"tenant_id obbligatorio per ruolo {role}")
            try:
                tenant_id = int(tenant_id)
            except (TypeError, ValueError):
                raise ValueError("tenant_id deve essere intero")
        # Hash password se passata in plain
        password_hash = data.get("password_hash")
        if not password_hash and data.get("password"):
            import bcrypt
            password_hash = bcrypt.hashpw(
                str(data["password"]).encode("utf-8"), bcrypt.gensalt()
            ).decode("utf-8")
        uid = data.get("id")
        if not uid and not password_hash:
            raise ValueError("password obbligatoria per nuovo utente")
        with self.transaction() as conn:
            if uid:
                # Update: aggiorna password solo se fornita nuova
                if password_hash:
                    conn.execute(
                        """UPDATE users SET username = ?, password_hash = ?, role = ?,
                                  full_name = ?, email = ?, enabled = ?, tenant_id = ?
                           WHERE id = ?""",
                        (username, password_hash, role,
                         data.get("full_name"), data.get("email"),
                         1 if data.get("enabled", True) else 0, tenant_id, int(uid)),
                    )
                else:
                    conn.execute(
                        """UPDATE users SET username = ?, role = ?,
                                  full_name = ?, email = ?, enabled = ?, tenant_id = ?
                           WHERE id = ?""",
                        (username, role,
                         data.get("full_name"), data.get("email"),
                         1 if data.get("enabled", True) else 0, tenant_id, int(uid)),
                    )
                return int(uid)
            cur = conn.execute(
                """INSERT INTO users (username, password_hash, role, full_name, email, enabled, tenant_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (username, password_hash, role,
                 data.get("full_name"), data.get("email"),
                 1 if data.get("enabled", True) else 0, tenant_id),
            )
            return int(cur.lastrowid or 0)

    def delete_user(self, user_id: int) -> None:
        with self.transaction() as conn:
            conn.execute("DELETE FROM users WHERE id = ?", (user_id,))

    def log_login(self, *, username: str, ip: str | None, ua: str | None,
                  outcome: str) -> None:
        with self.transaction() as conn:
            conn.execute(
                """INSERT INTO auth_audit (username, ip_address, user_agent, outcome)
                   VALUES (?, ?, ?, ?)""",
                (username, ip, ua, outcome),
            )

    # =========================================================== RULES ====
    # Implementazione minimale per boot (port completo dal manager attuale in fase successiva)

    def list_rules(self, *, tenant_id: int | None = None,
                   only_enabled: bool | None = None,
                   action: str | None = None,
                   search: str | None = None) -> list[dict[str, Any]]:
        where: list[str] = []
        params: list[Any] = []
        if tenant_id is not None:
            where.append("tenant_id = ?"); params.append(int(tenant_id))
        if only_enabled is True:
            where.append("enabled = 1")
        elif only_enabled is False:
            where.append("enabled = 0")
        if action:
            where.append("action = ?"); params.append(action)
        if search:
            like = f"%{search.lower()}%"
            where.append("(LOWER(name) LIKE ? OR LOWER(COALESCE(match_subject_regex,'')) LIKE ?)")
            params.extend([like, like])
        where_sql = "WHERE " + " AND ".join(where) if where else ""
        with self._connect() as conn:
            # Ordine = ordine reale di valutazione del rule engine: priority ASC,
            # poi id ASC per stabilità su priorità uguali. Le disabilitate restano
            # intercalate (con opacità ridotta nella UI) così l'operatore vede
            # esattamente la sequenza di valutazione.
            rows = conn.execute(
                f"SELECT * FROM rules {where_sql} ORDER BY priority ASC, id ASC",
                params,
            ).fetchall()
            return [_decode_rule(r) for r in rows]

    def get_rule(self, rule_id: int) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM rules WHERE id = ?", (rule_id,)).fetchone()
            return _decode_rule(row) if row else None

    def upsert_rule(self, data: dict[str, Any], *, tenant_id: int,
                    created_by: str | None = None) -> int:
        """Insert or update di una regola (include i campi gerarchici v2).

        Per gli orfani: almeno un ``match_*_regex``/``match_to_domain`` deve essere
        valorizzato. Per i gruppi: idem (V004 — niente catch-all gerarchici). Per
        i figli: i match_* possono essere tutti vuoti (ereditano dal padre).
        Validazione completa V001-V008/V_PRI_RANGE è demandata al validatore di
        ``rules.validators`` chiamato dal route layer.
        """
        is_group = bool(data.get("is_group"))
        parent_id = data.get("parent_id") or None

        match_present = any(data.get(k) for k in
            ("match_from_regex", "match_to_regex", "match_subject_regex",
             "match_body_regex", "match_to_domain", "match_from_domain"))
        if not match_present and not data.get("scope_ref") and not parent_id and not is_group:
            raise ValueError("Almeno un match_*_regex / match_*_domain deve essere valorizzato")
        if is_group and not match_present:
            raise ValueError("Un gruppo deve avere almeno un match_* condiviso (V004)")

        action_map = data.get("action_map")
        if action_map is not None and not isinstance(action_map, str):
            action_map = json.dumps(action_map)

        # Bool tri-state: None = indifferente; convertiamo in INTEGER NULL
        def _bint(v):
            if v is None or v == "":
                return None
            if isinstance(v, bool):
                return 1 if v else 0
            return 1 if str(v).lower() in ("true", "1", "yes", "on") else 0

        # I gruppi non hanno azione propria: forziamo a stringa vuota convenzionale "group"
        action_value = (data.get("action") or "").strip()
        if is_group:
            action_value = "group"
        elif not action_value:
            action_value = "ignore"

        rid = data.get("id")
        with self.transaction() as conn:
            if rid:
                conn.execute(
                    """UPDATE rules SET
                          name = ?, scope_type = ?, scope_ref = ?, priority = ?, enabled = ?,
                          match_from_regex = ?, match_to_regex = ?, match_subject_regex = ?,
                          match_body_regex = ?, match_to_domain = ?, match_from_domain = ?,
                          match_at_hours = ?, match_in_service = ?, match_contract_active = ?,
                          match_known_customer = ?, match_has_exception_today = ?,
                          match_customer_groups = ?,
                          match_tag = ?, action = ?, action_map = ?, severity = ?,
                          continue_after_match = ?,
                          parent_id = ?, is_group = ?, group_label = ?,
                          exclusive_match = ?, continue_in_group = ?, exit_group_continue = ?,
                          updated_at = datetime('now')
                       WHERE id = ?""",
                    (
                        (data.get("name") or "").strip(),
                        data.get("scope_type") or "global",
                        data.get("scope_ref") or None,
                        int(data.get("priority", 100)),
                        1 if data.get("enabled", True) else 0,
                        data.get("match_from_regex") or None,
                        data.get("match_to_regex") or None,
                        data.get("match_subject_regex") or None,
                        data.get("match_body_regex") or None,
                        data.get("match_to_domain") or None,
                        (data.get("match_from_domain") or "").strip().lower() or None,
                        data.get("match_at_hours") or None,
                        _bint(data.get("match_in_service")),
                        _bint(data.get("match_contract_active")),
                        _bint(data.get("match_known_customer")),
                        _bint(data.get("match_has_exception_today")),
                        data.get("match_customer_groups") or None,
                        data.get("match_tag") or None,
                        action_value,
                        action_map,
                        data.get("severity") or None,
                        1 if data.get("continue_after_match") else 0,
                        int(parent_id) if parent_id else None,
                        1 if is_group else 0,
                        data.get("group_label") or None,
                        1 if data.get("exclusive_match", True) else 0,
                        1 if data.get("continue_in_group") else 0,
                        1 if data.get("exit_group_continue") else 0,
                        int(rid),
                    ),
                )
                return int(rid)
            cur = conn.execute(
                """INSERT INTO rules
                       (tenant_id, name, scope_type, scope_ref, priority, enabled,
                        match_from_regex, match_to_regex, match_subject_regex, match_body_regex,
                        match_to_domain, match_from_domain, match_at_hours, match_in_service,
                        match_contract_active, match_known_customer, match_has_exception_today,
                        match_customer_groups, match_tag, action, action_map, severity,
                        continue_after_match, created_by,
                        parent_id, is_group, group_label,
                        exclusive_match, continue_in_group, exit_group_continue)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                           ?, ?, ?, ?, ?, ?)""",
                (
                    int(tenant_id),
                    (data.get("name") or "").strip(),
                    data.get("scope_type") or "global",
                    data.get("scope_ref") or None,
                    int(data.get("priority", 100)),
                    1 if data.get("enabled", True) else 0,
                    data.get("match_from_regex") or None,
                    data.get("match_to_regex") or None,
                    data.get("match_subject_regex") or None,
                    data.get("match_body_regex") or None,
                    data.get("match_to_domain") or None,
                    (data.get("match_from_domain") or "").strip().lower() or None,
                    data.get("match_at_hours") or None,
                    _bint(data.get("match_in_service")),
                    _bint(data.get("match_contract_active")),
                    _bint(data.get("match_known_customer")),
                    _bint(data.get("match_has_exception_today")),
                    data.get("match_customer_groups") or None,
                    data.get("match_tag") or None,
                    action_value,
                    action_map,
                    data.get("severity") or None,
                    1 if data.get("continue_after_match") else 0,
                    (created_by or "ui")[:100],
                    int(parent_id) if parent_id else None,
                    1 if is_group else 0,
                    data.get("group_label") or None,
                    1 if data.get("exclusive_match", True) else 0,
                    1 if data.get("continue_in_group") else 0,
                    1 if data.get("exit_group_continue") else 0,
                ),
            )
            return int(cur.lastrowid or 0)

    def delete_rule(self, rule_id: int) -> None:
        with self.transaction() as conn:
            conn.execute("DELETE FROM rules WHERE id = ?", (rule_id,))

    def toggle_rule(self, rule_id: int) -> bool:
        with self.transaction() as conn:
            cur = conn.execute(
                "UPDATE rules SET enabled = 1 - enabled WHERE id = ? RETURNING enabled",
                (rule_id,),
            )
            row = cur.fetchone()
            return bool(row[0]) if row else False

    # ============================================ RULES — gerarchia v2 ===

    def list_top_level_items(self, *, tenant_id: int | None = None,
                             only_enabled: bool | None = None) -> list[dict[str, Any]]:
        """Orfane + gruppi (parent_id IS NULL). Ordinati per priority globale ASC."""
        where: list[str] = ["parent_id IS NULL"]
        params: list[Any] = []
        if tenant_id is not None:
            where.append("tenant_id = ?"); params.append(int(tenant_id))
        if only_enabled is True:
            where.append("enabled = 1")
        elif only_enabled is False:
            where.append("enabled = 0")
        where_sql = "WHERE " + " AND ".join(where)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM rules {where_sql} ORDER BY priority ASC, id ASC",
                params,
            ).fetchall()
            return [_decode_rule(r) for r in rows]

    def list_group_children(self, group_id: int, *,
                            only_enabled: bool | None = None) -> list[dict[str, Any]]:
        """Figli di un gruppo, ordinati per priority globale ASC."""
        where = ["parent_id = ?"]
        params: list[Any] = [int(group_id)]
        if only_enabled is True:
            where.append("enabled = 1")
        elif only_enabled is False:
            where.append("enabled = 0")
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM rules WHERE {' AND '.join(where)} ORDER BY priority ASC, id ASC",
                params,
            ).fetchall()
            return [_decode_rule(r) for r in rows]

    def list_rules_grouped(self, *, tenant_id: int | None = None,
                            only_enabled: bool | None = None) -> list[dict[str, Any]]:
        """Struttura adatta a UI tree-view.

        Restituisce una lista ordinata per priority ASC dove ciascun elemento è:
        ``{"type": "orphan", "rule": {...}}`` oppure
        ``{"type": "group", "group": {...}, "children": [...]}``.
        """
        top = self.list_top_level_items(tenant_id=tenant_id, only_enabled=only_enabled)
        grouped: list[dict[str, Any]] = []
        for item in top:
            if item.get("is_group"):
                children = self.list_group_children(item["id"], only_enabled=only_enabled)
                grouped.append({"type": "group", "group": item, "children": children})
            else:
                grouped.append({"type": "orphan", "rule": item})
        return grouped

    def flatten_rules_for_listener(self, *, tenant_id: int | None = None) -> list[dict[str, Any]]:
        """Appiattisce gerarchia in regole flat per `/api/v1/relay/rules/active`.

        Solo regole abilitate. Gruppi senza figli abilitati vengono scartati.
        """
        # Import locale per evitare cicli a livello modulo (rules → storage non dovrebbe esistere)
        from ..rules.flatten import flatten_rules

        top = self.list_top_level_items(tenant_id=tenant_id, only_enabled=True)
        children_by_parent: dict[int, list[dict[str, Any]]] = {}
        for item in top:
            if item.get("is_group"):
                children_by_parent[item["id"]] = self.list_group_children(
                    item["id"], only_enabled=True,
                )
        return flatten_rules(top, children_by_parent, only_enabled=True)

    def get_rule_with_inheritance(self, rule_id: int) -> dict[str, Any] | None:
        """Ritorna la regola con il merge effettivo (per debug/UI).

        Aggiunge campi:
        - ``parent``: dict del padre (o None)
        - ``effective_action_map``: action_map post-merge padre+figlio
        - ``inherited_keys``: chiavi action_map ereditate dal padre
        - ``flow_path``: stringa human-readable
        """
        from ..rules.inheritance import deep_merge_action_map, split_inherited_keys

        rule = self.get_rule(rule_id)
        if not rule:
            return None
        parent = None
        if rule.get("parent_id"):
            parent = self.get_rule(rule["parent_id"])
        effective_map = deep_merge_action_map(
            (parent or {}).get("action_map") or {},
            rule.get("action_map") or {},
        )
        inherited, own = split_inherited_keys(
            (parent or {}).get("action_map") or {}, effective_map,
        )
        rule["parent"] = parent
        rule["effective_action_map"] = effective_map
        rule["inherited_keys"] = sorted(inherited)
        rule["own_action_map_keys"] = sorted(own)
        if parent:
            rule["flow_path"] = f"group:{parent['id']} → rule:{rule['id']}"
        else:
            rule["flow_path"] = f"rule:{rule['id']}"
        return rule

    def promote_rule_to_group(self, rule_id: int, group_label: str,
                              *, group_priority: int | None = None,
                              created_by: str | None = None) -> int:
        """Promuove una regola standalone a gruppo.

        - Crea un nuovo record gruppo con i match_* della regola sorgente e
          ``action_map_defaults`` filtrati per le sole chiavi
          ``PARENT_ACTION_MAP_DEFAULTS``.
        - La regola sorgente diventa figlia del nuovo gruppo (parent_id =
          nuovo_id) e mantiene la propria action+action_map (depurata delle
          chiavi ereditate identiche al padre).
        - Idempotente: se la regola ha già parent_id, ritorna quel parent.

        Returns:
            id del gruppo (nuovo o esistente).
        """
        from ..rules.action_map_schema import PARENT_ACTION_MAP_DEFAULTS

        src = self.get_rule(rule_id)
        if not src:
            raise ValueError(f"Regola {rule_id} non trovata")
        if src.get("parent_id"):
            return int(src["parent_id"])
        if src.get("is_group"):
            raise ValueError("Una regola gruppo non può essere promossa")

        action_map = src.get("action_map") or {}
        parent_defaults = {
            k: v for k, v in action_map.items()
            if k in PARENT_ACTION_MAP_DEFAULTS and v is not None
        }
        child_action_map = {
            k: v for k, v in action_map.items()
            if k not in parent_defaults
        }

        # Padre: stessa priority della regola sorgente meno 1 (slot dedicato)
        # oppure quella esplicita passata dal caller.
        if group_priority is None:
            group_priority = max(1, int(src.get("priority", 100)) - 1)

        with self.transaction() as conn:
            # Inserisce il gruppo (ricopiando i match_* del sorgente)
            cur = conn.execute(
                """INSERT INTO rules
                       (tenant_id, name, scope_type, scope_ref, priority, enabled,
                        match_from_regex, match_to_regex, match_subject_regex, match_body_regex,
                        match_to_domain, match_from_domain, match_at_hours, match_in_service,
                        match_contract_active, match_known_customer, match_has_exception_today,
                        match_tag, action, action_map, severity,
                        continue_after_match, created_by,
                        parent_id, is_group, group_label,
                        exclusive_match, continue_in_group, exit_group_continue)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                           NULL, 1, ?, 1, 0, 0)""",
                (
                    int(src.get("tenant_id", 1)),
                    f"[GRUPPO] {group_label}",
                    src.get("scope_type") or "global",
                    src.get("scope_ref"),
                    int(group_priority),
                    1,
                    src.get("match_from_regex"),
                    src.get("match_to_regex"),
                    src.get("match_subject_regex"),
                    src.get("match_body_regex"),
                    src.get("match_to_domain"),
                    src.get("match_from_domain"),
                    src.get("match_at_hours"),
                    src.get("match_in_service"),
                    src.get("match_contract_active"),
                    src.get("match_known_customer"),
                    src.get("match_has_exception_today"),
                    src.get("match_tag"),
                    "group",
                    json.dumps(parent_defaults),
                    src.get("severity"),
                    0,
                    (created_by or "promote")[:100],
                    group_label,
                ),
            )
            new_group_id = int(cur.lastrowid or 0)
            # Sposta la regola sorgente come figlia del nuovo gruppo, ripulendo
            # i match_* ereditati e le action_map keys ereditate.
            conn.execute(
                """UPDATE rules SET parent_id = ?, action_map = ?, updated_at = datetime('now')
                       WHERE id = ?""",
                (new_group_id, json.dumps(child_action_map), int(rule_id)),
            )
            return new_group_id

    def detect_groupable_rules(self, *, tenant_id: int | None = None,
                               min_cluster_size: int = 2) -> list[dict[str, Any]]:
        """Trova cluster di regole orfane con match_* identici, candidate alla
        promozione a gruppo.

        Algoritmo greedy: due regole sono compatibili se condividono almeno un
        ``match_*`` non vuoto e i match_* in comune sono identici. I tristate
        contano solo se non NULL.

        Returns:
            Lista di cluster ordinata per dimensione DESC. Ciascun cluster:
            ``{"common_matches": {...}, "common_action_map_defaults": {...},
               "rule_ids": [...], "suggested_label": "..."}``.
        """
        from ..rules.action_map_schema import PARENT_ACTION_MAP_DEFAULTS
        from ..rules.validators import MATCH_FIELDS_TEXT, MATCH_FIELDS_TRISTATE

        match_fields = MATCH_FIELDS_TEXT + MATCH_FIELDS_TRISTATE

        orphans = [
            r for r in self.list_top_level_items(
                tenant_id=tenant_id, only_enabled=None,
            )
            if not r.get("is_group")
        ]

        # Costruisci una "fingerprint" dei match_* non vuoti per ogni regola.
        def _fp(r: dict[str, Any]) -> tuple[tuple[str, Any], ...]:
            items: list[tuple[str, Any]] = []
            for f in match_fields:
                v = r.get(f)
                if v not in (None, ""):
                    if f in MATCH_FIELDS_TRISTATE:
                        items.append((f, int(v)))
                    elif f in ("match_to_domain", "match_from_domain"):
                        items.append((f, str(v).lower()))
                    else:
                        items.append((f, str(v)))
            return tuple(sorted(items))

        clusters: dict[tuple, list[dict[str, Any]]] = {}
        for r in orphans:
            fp = _fp(r)
            if not fp:
                continue
            clusters.setdefault(fp, []).append(r)

        result: list[dict[str, Any]] = []
        for fp, rules in clusters.items():
            if len(rules) < min_cluster_size:
                continue
            common_matches = dict(fp)
            # action_map keys condivise a livello padre con stesso valore
            shared_defaults: dict[str, Any] | None = None
            for r in rules:
                am = r.get("action_map") or {}
                defaults_here = {
                    k: am.get(k) for k in PARENT_ACTION_MAP_DEFAULTS
                    if am.get(k) is not None
                }
                if shared_defaults is None:
                    shared_defaults = dict(defaults_here)
                else:
                    shared_defaults = {
                        k: v for k, v in shared_defaults.items()
                        if defaults_here.get(k) == v
                    }
            label = self._suggest_group_label(common_matches)
            result.append({
                "common_matches": common_matches,
                "common_action_map_defaults": shared_defaults or {},
                "rule_ids": [r["id"] for r in rules],
                "rules": rules,
                "suggested_label": label,
                "size": len(rules),
            })
        result.sort(key=lambda c: c["size"], reverse=True)
        return result

    @staticmethod
    def _suggest_group_label(common_matches: dict[str, Any]) -> str:
        """Heuristic per proporre un'etichetta umana a partire dai match comuni."""
        bits: list[str] = []
        if common_matches.get("match_in_service") == 0:
            bits.append("Fuori orario")
        elif common_matches.get("match_in_service") == 1:
            bits.append("In orario")
        if common_matches.get("match_contract_active") == 1:
            bits.append("clienti con contratto")
        elif common_matches.get("match_contract_active") == 0:
            bits.append("clienti senza contratto")
        if common_matches.get("match_known_customer") == 1:
            bits.append("censiti")
        if common_matches.get("match_to_domain"):
            bits.append(f"verso {common_matches['match_to_domain']}")
        return " ".join(bits) if bits else "Gruppo regole correlate"

    # ========================================================= TEMPLATES ===

    def list_templates(self, *, tenant_id: int | None = None,
                       only_enabled: bool | None = None) -> list[dict[str, Any]]:
        where: list[str] = []
        params: list[Any] = []
        if tenant_id is not None:
            where.append("tenant_id = ?"); params.append(int(tenant_id))
        if only_enabled is True:
            where.append("enabled = 1")
        where_sql = "WHERE " + " AND ".join(where) if where else ""
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM reply_templates {where_sql} ORDER BY name", params
            ).fetchall()
            return [_decode_template(r) for r in rows]

    def get_template(self, template_id: int) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM reply_templates WHERE id = ?", (template_id,)).fetchone()
            return _decode_template(row) if row else None

    def upsert_template(self, data: dict[str, Any], *, tenant_id: int) -> int:
        if not (data.get("name") or "").strip():
            raise ValueError("name template obbligatorio")
        if not (data.get("subject_tmpl") or "").strip() or not (data.get("body_html_tmpl") or "").strip():
            raise ValueError("subject_tmpl e body_html_tmpl obbligatori")

        atts = data.get("attachment_paths")
        if atts is not None and not isinstance(atts, str):
            atts = json.dumps(atts)

        tid = data.get("id")
        with self.transaction() as conn:
            if tid:
                conn.execute(
                    """UPDATE reply_templates SET
                          name = ?, description = ?, subject_tmpl = ?, body_html_tmpl = ?,
                          body_text_tmpl = ?, reply_from_name = ?, reply_from_email = ?,
                          attachment_paths = ?, enabled = ?, updated_at = datetime('now'),
                          updated_by = ?
                       WHERE id = ?""",
                    (
                        data["name"].strip(),
                        data.get("description") or None,
                        data["subject_tmpl"].strip(),
                        data["body_html_tmpl"].strip(),
                        (data.get("body_text_tmpl") or "").strip() or None,
                        data.get("reply_from_name") or None,
                        data.get("reply_from_email") or None,
                        atts,
                        1 if data.get("enabled", True) else 0,
                        (data.get("updated_by") or "ui")[:100],
                        int(tid),
                    ),
                )
                return int(tid)
            cur = conn.execute(
                """INSERT INTO reply_templates
                       (tenant_id, name, description, subject_tmpl, body_html_tmpl, body_text_tmpl,
                        reply_from_name, reply_from_email, attachment_paths, enabled, updated_by)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    int(tenant_id),
                    data["name"].strip(),
                    data.get("description") or None,
                    data["subject_tmpl"].strip(),
                    data["body_html_tmpl"].strip(),
                    (data.get("body_text_tmpl") or "").strip() or None,
                    data.get("reply_from_name") or None,
                    data.get("reply_from_email") or None,
                    atts,
                    1 if data.get("enabled", True) else 0,
                    (data.get("updated_by") or "ui")[:100],
                ),
            )
            return int(cur.lastrowid or 0)

    def delete_template(self, template_id: int) -> None:
        with self.transaction() as conn:
            conn.execute("DELETE FROM reply_templates WHERE id = ?", (template_id,))

    # =========================================================== EVENTS ====

    def list_events(self, *, tenant_id: int | None = None,
                    hours: int = 24,
                    page: int = 1, page_size: int = 50,
                    filters: dict[str, Any] | None = None) -> tuple[list[dict[str, Any]], int]:
        where: list[str] = ["received_at > datetime('now', '-' || ? || ' hours')"]
        params: list[Any] = [int(hours)]
        if tenant_id is not None:
            where.append("tenant_id = ?"); params.append(int(tenant_id))
        # Filtri basici (estendibili)
        if filters:
            if filters.get("action"):
                where.append("action_taken = ?"); params.append(filters["action"])
            if filters.get("no_client"):
                where.append("(codice_cliente IS NULL OR codice_cliente = '')")
            if filters.get("no_rule"):
                where.append("rule_id IS NULL")
            if filters.get("only_ticket"):
                where.append("ticket_id IS NOT NULL AND ticket_id <> ''")
            if filters.get("q"):
                like = f"%{filters['q'].strip().lower()}%"
                where.append(
                    "(LOWER(from_address) LIKE ? OR LOWER(to_address) LIKE ? "
                    "OR LOWER(subject) LIKE ? OR LOWER(codice_cliente) LIKE ?)"
                )
                params.extend([like, like, like, like])
        where_sql = "WHERE " + " AND ".join(where)
        with self._connect() as conn:
            total = int(conn.execute(
                f"SELECT COUNT(*) FROM events {where_sql}", params
            ).fetchone()[0])
            offset = (max(1, page) - 1) * page_size
            rows = conn.execute(
                f"SELECT * FROM events {where_sql} ORDER BY received_at DESC LIMIT ? OFFSET ?",
                params + [page_size, offset],
            ).fetchall()
            return [_decode_event(r) for r in rows], total

    def get_event(self, event_id: int) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
            return _decode_event(row) if row else None

    def insert_event(self, data: dict[str, Any]) -> int:
        with self.transaction() as conn:
            cur = conn.execute(
                """INSERT INTO events
                       (tenant_id, relay_event_uuid, received_at, from_address, to_address,
                        subject, message_id, codice_cliente, action_taken, rule_id, ticket_id,
                        payload_metadata, body_text, body_html, body_expires_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT (relay_event_uuid) DO NOTHING""",
                (
                    int(data.get("tenant_id", 1)),
                    str(data["relay_event_uuid"]),
                    data["received_at"],
                    data.get("from_address"),
                    data.get("to_address"),
                    data.get("subject"),
                    data.get("message_id"),
                    data.get("codice_cliente"),
                    data.get("action_taken"),
                    data.get("rule_id"),
                    data.get("ticket_id"),
                    # Il listener invia payload_metadata come stringa JSON già
                    # serializzata; un client che passa un dict viene re-serializzato.
                    (data["payload_metadata"] if isinstance(data.get("payload_metadata"), str)
                     else json.dumps(data["payload_metadata"])) if data.get("payload_metadata") else None,
                    data.get("body_text"),
                    data.get("body_html"),
                    data.get("body_expires_at"),
                ),
            )
            return int(cur.lastrowid or 0)

    def purge_expired_bodies(self) -> int:
        with self.transaction() as conn:
            cur = conn.execute(
                """UPDATE events
                      SET body_text = NULL, body_html = NULL, body_expires_at = NULL
                    WHERE body_expires_at IS NOT NULL
                      AND body_expires_at < datetime('now')
                      AND (body_text IS NOT NULL OR body_html IS NOT NULL)"""
            )
            return cur.rowcount

    # ====================================================== SERVICE HOURS ===

    def list_service_hours(self, *, tenant_id: int | None = None,
                           search: str | None = None) -> list[dict[str, Any]]:
        where: list[str] = []
        params: list[Any] = []
        if tenant_id is not None:
            where.append("tenant_id = ?"); params.append(int(tenant_id))
        if search:
            where.append("LOWER(codice_cliente) LIKE ?")
            params.append(f"%{search.lower()}%")
        where_sql = "WHERE " + " AND ".join(where) if where else ""
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM service_hours {where_sql} ORDER BY codice_cliente", params
            ).fetchall()
            return [_decode_service_hours(r) for r in rows]

    def get_service_hours(self, codice_cliente: str, tenant_id: int) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM service_hours WHERE tenant_id = ? AND codice_cliente = ?",
                (int(tenant_id), codice_cliente),
            ).fetchone()
            return _decode_service_hours(row) if row else None

    def upsert_service_hours(self, data: dict[str, Any], *, tenant_id: int) -> str:
        codcli = (data.get("codice_cliente") or "").strip().upper()
        if not codcli:
            raise ValueError("codice_cliente obbligatorio")
        schedule = data.get("schedule")
        if not isinstance(schedule, dict):
            raise ValueError("schedule deve essere un oggetto JSON")
        sched_json = json.dumps(schedule)
        holidays_json = json.dumps(data.get("holidays") or [])
        excs_json = json.dumps(data.get("schedule_exceptions") or [])
        with self.transaction() as conn:
            conn.execute(
                """INSERT INTO service_hours
                       (tenant_id, codice_cliente, profile, profile_id, timezone,
                        schedule, holidays, schedule_exceptions, ah_key, notes,
                        enabled, updated_by)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(tenant_id, codice_cliente) DO UPDATE SET
                       profile = excluded.profile,
                       profile_id = excluded.profile_id,
                       timezone = excluded.timezone,
                       schedule = excluded.schedule,
                       holidays = excluded.holidays,
                       schedule_exceptions = excluded.schedule_exceptions,
                       ah_key = excluded.ah_key,
                       notes = excluded.notes,
                       enabled = excluded.enabled,
                       updated_at = datetime('now'),
                       updated_by = excluded.updated_by""",
                (
                    int(tenant_id), codcli,
                    (data.get("profile") or "custom")[:40],
                    int(data["profile_id"]) if data.get("profile_id") else None,
                    (data.get("timezone") or "Europe/Rome")[:64],
                    sched_json, holidays_json, excs_json,
                    data.get("ah_key") or None,
                    data.get("notes") or None,
                    1 if data.get("enabled", True) else 0,
                    (data.get("updated_by") or "ui")[:100],
                ),
            )
        return codcli

    def delete_service_hours(self, codice_cliente: str, tenant_id: int) -> None:
        with self.transaction() as conn:
            conn.execute(
                "DELETE FROM service_hours WHERE tenant_id = ? AND codice_cliente = ?",
                (int(tenant_id), codice_cliente),
            )

    def list_profiles(self, *, tenant_id: int | None = None) -> list[dict[str, Any]]:
        # tenant_id NULL = built-in globali; un tenant vede built-in + propri.
        where = "WHERE 1=1"
        params: list[Any] = []
        if tenant_id is not None:
            where += " AND (tenant_id IS NULL OR tenant_id = ?)"
            params.append(int(tenant_id))
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM service_hours_profiles {where} "
                f"ORDER BY is_builtin DESC, name", params,
            ).fetchall()
            return [_decode_profile(r) for r in rows]

    def get_profile(self, profile_id: int) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM service_hours_profiles WHERE id = ?", (profile_id,)
            ).fetchone()
            return _decode_profile(row) if row else None

    def delete_profile(self, profile_id: int) -> None:
        """Elimina un profilo. Built-in (tenant_id IS NULL, is_builtin=1) NON cancellabili."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT is_builtin FROM service_hours_profiles WHERE id = ?", (profile_id,)
            ).fetchone()
            if not row:
                return
            if row[0]:
                raise ValueError("Profili built-in non sono cancellabili")
        with self.transaction() as conn:
            conn.execute("DELETE FROM service_hours_profiles WHERE id = ?", (profile_id,))

    def refresh_holidays_italian(self, year: int | None = None) -> dict[str, int]:
        """Aggiorna holidays di tutti i profili con `holidays_auto=1`.

        Calcola le festività italiane per l'anno corrente (e l'anno successivo
        per planning ahead). Non tocca profili custom con holidays_auto=0.
        """
        from datetime import date, timedelta
        if year is None:
            year = date.today().year
        # Pasqua via algoritmo di Gauss
        def easter(y: int) -> date:
            a = y % 19; b = y // 100; c = y % 100
            d = b // 4; e = b % 4; f = (b + 8) // 25
            g = (b - f + 1) // 3; h = (19*a + b - d - g + 15) % 30
            i = c // 4; k = c % 4; l = (32 + 2*e + 2*i - h - k) % 7
            m = (a + 11*h + 22*l) // 451
            month = (h + l - 7*m + 114) // 31
            day = ((h + l - 7*m + 114) % 31) + 1
            return date(y, month, day)
        holidays_for_year: list[str] = []
        for y in (year, year + 1):
            fixed = [
                date(y, 1, 1), date(y, 1, 6), date(y, 4, 25), date(y, 5, 1),
                date(y, 6, 2), date(y, 8, 15), date(y, 11, 1),
                date(y, 12, 8), date(y, 12, 25), date(y, 12, 26),
            ]
            ed = easter(y)
            fixed.append(ed + timedelta(days=1))  # Pasquetta
            holidays_for_year.extend(d.isoformat() for d in sorted(fixed))
        with self.transaction() as conn:
            cur = conn.execute(
                """UPDATE service_hours_profiles
                      SET holidays = ?,
                          updated_at = datetime('now'),
                          updated_by = 'holidays_refresh'
                    WHERE holidays_auto = 1""",
                (json.dumps(holidays_for_year),),
            )
            updated = cur.rowcount
        return {"updated_profiles": updated, "year": year, "holidays_count": len(holidays_for_year)}

    def get_profile_by_code(self, code: str, *, tenant_id: int | None = None) -> dict[str, Any] | None:
        """Lookup profilo per `code` (es. 'STD'). I built-in hanno tenant_id NULL."""
        if not code:
            return None
        with self._connect() as conn:
            if tenant_id is None:
                row = conn.execute(
                    "SELECT * FROM service_hours_profiles WHERE code = ? AND tenant_id IS NULL",
                    (code,),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM service_hours_profiles "
                    "WHERE code = ? AND (tenant_id IS NULL OR tenant_id = ?) "
                    "ORDER BY tenant_id IS NULL ASC LIMIT 1",
                    (code, int(tenant_id)),
                ).fetchone()
            return _decode_profile(row) if row else None

    def upsert_profile(self, data: dict[str, Any], *, tenant_id: int | None = None) -> int:
        """Idempotent UPSERT su (tenant_id, code) o (tenant_id, name).

        I profili canonici (STD/EXT/H24/NO) sono identificati da `code` con tenant_id=NULL.
        Custom per tenant: tenant_id valorizzato + nome libero.
        """
        code = (data.get("code") or "").strip() or None
        name = (data.get("name") or "").strip()
        if not name and not code:
            raise ValueError("Profilo: serve almeno code o name")
        schedule = data.get("schedule") or {}
        holidays = data.get("holidays") or []
        if isinstance(schedule, (dict, list)):
            schedule = json.dumps(schedule)
        if isinstance(holidays, list):
            holidays = json.dumps(holidays)

        existing = None
        if code:
            existing = self.get_profile_by_code(code, tenant_id=tenant_id)
        if not existing and name:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT * FROM service_hours_profiles "
                    "WHERE name = ? AND COALESCE(tenant_id, -1) = COALESCE(?, -1)",
                    (name, tenant_id),
                ).fetchone()
                existing = _decode_profile(row) if row else None

        with self.transaction() as conn:
            if existing:
                conn.execute(
                    """UPDATE service_hours_profiles SET
                          code = COALESCE(?, code),
                          name = ?,
                          description = ?,
                          details = ?,
                          schedule = ?,
                          holidays = ?,
                          holidays_auto = ?,
                          timezone = ?,
                          is_builtin = ?,
                          exclude_holidays = ?,
                          requires_authorization_always = ?,
                          authorize_outside_hours = ?,
                          enabled = ?,
                          updated_at = datetime('now'),
                          updated_by = ?
                       WHERE id = ?""",
                    (
                        code, name or existing.get("name"),
                        data.get("description"), data.get("details"),
                        schedule, holidays,
                        1 if data.get("holidays_auto") else 0,
                        data.get("timezone") or "Europe/Rome",
                        1 if data.get("is_builtin") else 0,
                        1 if data.get("exclude_holidays", True) else 0,
                        1 if data.get("requires_authorization_always") else 0,
                        1 if data.get("authorize_outside_hours", True) else 0,
                        1 if data.get("enabled", True) else 0,
                        data.get("updated_by") or "sync",
                        existing["id"],
                    ),
                )
                return int(existing["id"])
            cur = conn.execute(
                """INSERT INTO service_hours_profiles
                       (tenant_id, code, name, description, details, schedule, holidays,
                        holidays_auto, timezone, is_builtin, enabled, exclude_holidays,
                        requires_authorization_always, authorize_outside_hours, updated_by)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    tenant_id, code, name, data.get("description"), data.get("details"),
                    schedule, holidays,
                    1 if data.get("holidays_auto") else 0,
                    data.get("timezone") or "Europe/Rome",
                    1 if data.get("is_builtin") else 0,
                    1 if data.get("enabled", True) else 0,
                    1 if data.get("exclude_holidays", True) else 0,
                    1 if data.get("requires_authorization_always") else 0,
                    1 if data.get("authorize_outside_hours", True) else 0,
                    data.get("updated_by") or "sync",
                ),
            )
            return int(cur.lastrowid)

    # ======================================================= AUTH CODES =====

    def list_auth_codes(self, *, tenant_id: int | None = None,
                        only_active: bool = False,
                        codice_cliente: str | None = None,
                        limit: int = 200) -> list[dict[str, Any]]:
        where: list[str] = []
        params: list[Any] = []
        if tenant_id is not None:
            where.append("tenant_id = ?"); params.append(int(tenant_id))
        if only_active:
            where.append("used_at IS NULL AND valid_until > datetime('now')")
        if codice_cliente:
            where.append("codice_cliente = ?"); params.append(codice_cliente)
        where_sql = "WHERE " + " AND ".join(where) if where else ""
        params.append(int(limit))
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM authorization_codes {where_sql} "
                f"ORDER BY generated_at DESC LIMIT ?", params
            ).fetchall()
            return [dict(r) for r in rows]

    def issue_auth_code(self, *, tenant_id: int, codice_cliente: str | None,
                        rule_id: int | None, ttl_hours: int,
                        note: str | None = None) -> dict[str, Any]:
        import secrets
        import string
        from datetime import datetime as _dt, timedelta as _td
        ttl = max(1, min(int(ttl_hours), 720))
        valid_until = (_dt.utcnow() + _td(hours=ttl)).strftime("%Y-%m-%d %H:%M:%S")
        alphabet = string.ascii_uppercase + string.digits
        with self.transaction() as conn:
            for _ in range(8):
                code = "".join(secrets.choice(alphabet) for _ in range(6))
                try:
                    cur = conn.execute(
                        """INSERT INTO authorization_codes
                               (tenant_id, code, codice_cliente, rule_id, valid_until, note)
                           VALUES (?, ?, ?, ?, ?, ?)""",
                        (int(tenant_id), code, codice_cliente, rule_id, valid_until, note),
                    )
                    return {
                        "ok": True,
                        "id": int(cur.lastrowid or 0),
                        "code": code,
                        "valid_until": valid_until,
                    }
                except sqlite3.IntegrityError:
                    continue
            raise ValueError("Impossibile generare codice univoco dopo 8 tentativi")

    def delete_auth_code(self, code_id: int) -> None:
        with self.transaction() as conn:
            conn.execute("DELETE FROM authorization_codes WHERE id = ?", (code_id,))

    # ====================================================== AGGREGATIONS ====

    def list_aggregations(self, *, tenant_id: int | None = None,
                          only_enabled: bool | None = None) -> list[dict[str, Any]]:
        where: list[str] = []
        params: list[Any] = []
        if tenant_id is not None:
            where.append("a.tenant_id = ?"); params.append(int(tenant_id))
        if only_enabled is True:
            where.append("a.enabled = 1")
        elif only_enabled is False:
            where.append("a.enabled = 0")
        where_sql = "WHERE " + " AND ".join(where) if where else ""
        with self._connect() as conn:
            rows = conn.execute(
                f"""SELECT a.*,
                          COALESCE(o.active_count, 0) AS active_occurrences,
                          COALESCE(o.with_ticket, 0) AS occurrences_with_ticket
                     FROM error_aggregations a
                     LEFT JOIN (
                         SELECT aggregation_id,
                                COUNT(*) AS active_count,
                                SUM(CASE WHEN ticket_id IS NOT NULL THEN 1 ELSE 0 END) AS with_ticket
                           FROM error_occurrences
                          WHERE current_count > 0 OR ticket_id IS NOT NULL
                          GROUP BY aggregation_id
                     ) o ON o.aggregation_id = a.id
                     {where_sql}
                    ORDER BY a.priority ASC, a.id ASC""", params,
            ).fetchall()
            return [dict(r) for r in rows]

    def get_aggregation(self, agg_id: int) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM error_aggregations WHERE id = ?", (agg_id,)
            ).fetchone()
            return dict(row) if row else None

    def upsert_aggregation(self, data: dict[str, Any], *, tenant_id: int) -> int:
        if not (data.get("name") or "").strip():
            raise ValueError("name aggregation obbligatorio")
        if not any(data.get(k) for k in
                   ("match_from_regex", "match_subject_regex", "match_body_regex")):
            raise ValueError("Almeno un match_*_regex deve essere valorizzato")
        aid = data.get("id")
        with self.transaction() as conn:
            if aid:
                conn.execute(
                    """UPDATE error_aggregations SET
                          name = ?, description = ?,
                          match_from_regex = ?, match_subject_regex = ?, match_body_regex = ?,
                          fingerprint_template = ?, threshold = ?, consecutive_only = ?,
                          window_hours = ?, reset_subject_regex = ?, reset_from_regex = ?,
                          ticket_settore = ?, ticket_urgenza = ?, ticket_codice_cliente = ?,
                          enabled = ?, priority = ?, updated_at = datetime('now')
                       WHERE id = ?""",
                    (
                        data["name"].strip(), data.get("description") or None,
                        data.get("match_from_regex") or None,
                        data.get("match_subject_regex") or None,
                        data.get("match_body_regex") or None,
                        data.get("fingerprint_template") or "${from}|${subject_normalized}",
                        int(data.get("threshold", 2)),
                        1 if data.get("consecutive_only") else 0,
                        int(data.get("window_hours", 24)),
                        data.get("reset_subject_regex") or None,
                        data.get("reset_from_regex") or None,
                        data.get("ticket_settore") or None,
                        data.get("ticket_urgenza") or None,
                        data.get("ticket_codice_cliente") or None,
                        1 if data.get("enabled", True) else 0,
                        int(data.get("priority", 100)),
                        int(aid),
                    ),
                )
                return int(aid)
            cur = conn.execute(
                """INSERT INTO error_aggregations
                       (tenant_id, name, description,
                        match_from_regex, match_subject_regex, match_body_regex,
                        fingerprint_template, threshold, consecutive_only, window_hours,
                        reset_subject_regex, reset_from_regex,
                        ticket_settore, ticket_urgenza, ticket_codice_cliente,
                        enabled, priority, created_by)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    int(tenant_id),
                    data["name"].strip(), data.get("description") or None,
                    data.get("match_from_regex") or None,
                    data.get("match_subject_regex") or None,
                    data.get("match_body_regex") or None,
                    data.get("fingerprint_template") or "${from}|${subject_normalized}",
                    int(data.get("threshold", 2)),
                    1 if data.get("consecutive_only") else 0,
                    int(data.get("window_hours", 24)),
                    data.get("reset_subject_regex") or None,
                    data.get("reset_from_regex") or None,
                    data.get("ticket_settore") or None,
                    data.get("ticket_urgenza") or None,
                    data.get("ticket_codice_cliente") or None,
                    1 if data.get("enabled", True) else 0,
                    int(data.get("priority", 100)),
                    (data.get("created_by") or "ui")[:100],
                ),
            )
            return int(cur.lastrowid or 0)

    def delete_aggregation(self, agg_id: int) -> None:
        with self.transaction() as conn:
            conn.execute("DELETE FROM error_aggregations WHERE id = ?", (agg_id,))

    def toggle_aggregation(self, agg_id: int) -> bool:
        with self.transaction() as conn:
            cur = conn.execute(
                "UPDATE error_aggregations SET enabled = 1 - enabled WHERE id = ? RETURNING enabled",
                (agg_id,),
            )
            row = cur.fetchone()
            return bool(row[0]) if row else False

    def reset_occurrence(self, occ_id: int) -> None:
        """Azzera il counter di una singola occurrence."""
        with self.transaction() as conn:
            conn.execute(
                """UPDATE error_occurrences
                      SET current_count = 0,
                          last_reset_at = datetime('now'),
                          total_resets = COALESCE(total_resets, 0) + 1,
                          ticket_id = NULL,
                          ticket_opened_at = NULL
                    WHERE id = ?""",
                (occ_id,),
            )

    def reset_all_occurrences(self, *, tenant_id: int, aggregation_id: int | None = None) -> int:
        """Azzera counter di tutte le occurrences di un tenant (opzionale per regola)."""
        with self.transaction() as conn:
            if aggregation_id:
                cur = conn.execute(
                    """UPDATE error_occurrences
                          SET current_count = 0,
                              last_reset_at = datetime('now'),
                              total_resets = COALESCE(total_resets, 0) + 1,
                              ticket_id = NULL, ticket_opened_at = NULL
                        WHERE tenant_id = ? AND aggregation_id = ?""",
                    (int(tenant_id), int(aggregation_id)),
                )
            else:
                cur = conn.execute(
                    """UPDATE error_occurrences
                          SET current_count = 0,
                              last_reset_at = datetime('now'),
                              total_resets = COALESCE(total_resets, 0) + 1,
                              ticket_id = NULL, ticket_opened_at = NULL
                        WHERE tenant_id = ?""",
                    (int(tenant_id),),
                )
            return cur.rowcount

    def delete_occurrence(self, occ_id: int) -> None:
        with self.transaction() as conn:
            conn.execute("DELETE FROM error_occurrences WHERE id = ?", (occ_id,))

    # =========================================================== ROUTES =====

    def list_routes(self, *, tenant_id: int | None = None,
                    only_enabled: bool | None = None) -> list[dict[str, Any]]:
        where: list[str] = []
        params: list[Any] = []
        if tenant_id is not None:
            where.append("tenant_id = ?"); params.append(int(tenant_id))
        if only_enabled is True:
            where.append("enabled = 1")
        elif only_enabled is False:
            where.append("enabled = 0")
        ws = "WHERE " + " AND ".join(where) if where else ""
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM routes {ws} ORDER BY domain, local_part", params
            ).fetchall()
            return [dict(r) for r in rows]

    def get_route(self, route_id: int) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM routes WHERE id = ?", (route_id,)).fetchone()
            return dict(row) if row else None

    def upsert_route(self, data: dict[str, Any], *, tenant_id: int) -> int:
        local_part = (data.get("local_part") or "").strip()
        domain = (data.get("domain") or "").strip().lower()
        if not local_part or not domain:
            raise ValueError("local_part e domain obbligatori")
        rid = data.get("id")
        with self.transaction() as conn:
            if rid:
                conn.execute(
                    """UPDATE routes SET
                          local_part = ?, domain = ?, codice_cliente = ?,
                          forward_target = ?, forward_port = ?, forward_tls = ?,
                          redirect_target = ?, enabled = ?, apply_rules = ?, notes = ?,
                          updated_at = datetime('now')
                       WHERE id = ?""",
                    (local_part, domain, data.get("codice_cliente"),
                     data.get("forward_target"), int(data.get("forward_port") or 25),
                     data.get("forward_tls") or "opportunistic",
                     data.get("redirect_target"),
                     1 if data.get("enabled", True) else 0,
                     1 if data.get("apply_rules", True) else 0,
                     data.get("notes"), int(rid)),
                )
                return int(rid)
            cur = conn.execute(
                """INSERT INTO routes (tenant_id, local_part, domain, codice_cliente,
                       forward_target, forward_port, forward_tls,
                       redirect_target, enabled, apply_rules, notes)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (int(tenant_id), local_part, domain, data.get("codice_cliente"),
                 data.get("forward_target"), int(data.get("forward_port") or 25),
                 data.get("forward_tls") or "opportunistic",
                 data.get("redirect_target"),
                 1 if data.get("enabled", True) else 0,
                 1 if data.get("apply_rules", True) else 0,
                 data.get("notes")),
            )
            return int(cur.lastrowid or 0)

    def delete_route(self, route_id: int) -> None:
        with self.transaction() as conn:
            conn.execute("DELETE FROM routes WHERE id = ?", (route_id,))

    # ====================================================== DOMAIN ROUTING ===

    def list_domain_routing(self, *, tenant_id: int | None = None) -> list[dict[str, Any]]:
        where = "WHERE tenant_id = ?" if tenant_id is not None else ""
        params = [int(tenant_id)] if tenant_id is not None else []
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM domain_routing {where} ORDER BY domain", params
            ).fetchall()
            return [dict(r) for r in rows]

    def get_domain_routing(self, domain_id: int) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM domain_routing WHERE id = ?", (domain_id,)).fetchone()
            return dict(row) if row else None

    def upsert_domain_routing(self, data: dict[str, Any], *, tenant_id: int) -> int:
        domain = (data.get("domain") or "").strip().lower()
        if not domain:
            raise ValueError("domain obbligatorio")
        did = data.get("id")
        with self.transaction() as conn:
            if did:
                conn.execute(
                    """UPDATE domain_routing SET
                          domain = ?, smarthost_host = ?, smarthost_port = ?,
                          smarthost_tls = ?, apply_rules = ?, enabled = ?, notes = ?,
                          updated_at = datetime('now')
                       WHERE id = ?""",
                    (domain, data.get("smarthost_host"),
                     int(data.get("smarthost_port") or 25),
                     data.get("smarthost_tls") or "opportunistic",
                     1 if data.get("apply_rules", True) else 0,
                     1 if data.get("enabled", True) else 0,
                     data.get("notes"), int(did)),
                )
                return int(did)
            cur = conn.execute(
                """INSERT INTO domain_routing (tenant_id, domain, smarthost_host,
                       smarthost_port, smarthost_tls, apply_rules, enabled, notes)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (int(tenant_id), domain, data.get("smarthost_host"),
                 int(data.get("smarthost_port") or 25),
                 data.get("smarthost_tls") or "opportunistic",
                 1 if data.get("apply_rules", True) else 0,
                 1 if data.get("enabled", True) else 0,
                 data.get("notes")),
            )
            return int(cur.lastrowid or 0)

    def delete_domain_routing(self, domain_id: int) -> None:
        with self.transaction() as conn:
            conn.execute("DELETE FROM domain_routing WHERE id = ?", (domain_id,))

    # ========================================================== ADDRESSES ===

    def list_addresses(self, kind: str, *, tenant_id: int | None = None,
                       search: str | None = None,
                       limit: int = 500) -> list[dict[str, Any]]:
        if kind not in ("from", "to"):
            raise ValueError("kind deve essere 'from' o 'to'")
        tbl = f"addresses_{kind}"
        where: list[str] = []
        params: list[Any] = []
        if tenant_id is not None:
            where.append("tenant_id = ?"); params.append(int(tenant_id))
        if search:
            where.append("LOWER(email_address) LIKE ?")
            params.append(f"%{search.lower()}%")
        ws = "WHERE " + " AND ".join(where) if where else ""
        params.append(int(limit))
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM {tbl} {ws} ORDER BY last_seen_at DESC LIMIT ?", params
            ).fetchall()
            return [dict(r) for r in rows]

    def upsert_address_codcli(self, kind: str, addr_id: int, codcli: str | None) -> None:
        if kind not in ("from", "to"):
            raise ValueError("kind deve essere 'from' o 'to'")
        with self.transaction() as conn:
            conn.execute(
                f"UPDATE addresses_{kind} SET codice_cliente = ? WHERE id = ?",
                (codcli, addr_id),
            )

    def delete_address(self, kind: str, addr_id: int) -> None:
        if kind not in ("from", "to"):
            raise ValueError("kind deve essere 'from' o 'to'")
        with self.transaction() as conn:
            conn.execute(f"DELETE FROM addresses_{kind} WHERE id = ?", (addr_id,))

    # ====================================================== PRIVACY BYPASS ===

    def set_address_privacy_bypass(self, kind: str, addr_id: int, *,
                                    on: bool, reason: str | None = None,
                                    actor: str | None = None) -> None:
        """Attiva/disattiva privacy bypass su un singolo indirizzo.

        Operazione tracciata in `privacy_bypass_audit` per traceability GDPR.
        """
        if kind not in ("from", "to"):
            raise ValueError("kind deve essere 'from' o 'to'")
        tbl = f"addresses_{kind}"
        with self.transaction() as conn:
            row = conn.execute(
                f"SELECT email_address, tenant_id FROM {tbl} WHERE id = ?", (addr_id,)
            ).fetchone()
            if not row:
                raise ValueError(f"Indirizzo {kind} #{addr_id} non trovato")
            conn.execute(
                f"""UPDATE {tbl} SET
                       privacy_bypass = ?,
                       privacy_bypass_reason = ?,
                       privacy_bypass_at = datetime('now'),
                       privacy_bypass_by = ?
                    WHERE id = ?""",
                (1 if on else 0, reason, actor, addr_id),
            )
            conn.execute(
                """INSERT INTO privacy_bypass_audit
                       (tenant_id, target_kind, target_value, action, reason, actor)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    int(row["tenant_id"]),
                    f"address_{kind}",
                    row["email_address"],
                    "enable" if on else "disable",
                    reason,
                    actor,
                ),
            )

    def list_addresses_privacy_bypass(self, kind: str, *,
                                       tenant_id: int | None = None) -> list[dict[str, Any]]:
        """Indirizzi con privacy_bypass attivo. Usato dall'API endpoint
        listener e dalle pagine UI dedicate."""
        if kind not in ("from", "to"):
            raise ValueError("kind deve essere 'from' o 'to'")
        tbl = f"addresses_{kind}"
        where = ["privacy_bypass = 1"]
        params: list[Any] = []
        if tenant_id is not None:
            where.append("tenant_id = ?"); params.append(int(tenant_id))
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM {tbl} WHERE {' AND '.join(where)} "
                f"ORDER BY privacy_bypass_at DESC", params
            ).fetchall()
            return [dict(r) for r in rows]

    def list_privacy_bypass_domains(self, *, tenant_id: int | None = None,
                                     only_enabled: bool | None = None) -> list[dict[str, Any]]:
        where: list[str] = []
        params: list[Any] = []
        if tenant_id is not None:
            where.append("tenant_id = ?"); params.append(int(tenant_id))
        if only_enabled is True:
            where.append("enabled = 1")
        ws = "WHERE " + " AND ".join(where) if where else ""
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM privacy_bypass_domains {ws} ORDER BY domain", params
            ).fetchall()
            return [dict(r) for r in rows]

    def upsert_privacy_bypass_domain(self, *, tenant_id: int, domain: str,
                                      scope: str = "both", reason: str | None = None,
                                      enabled: bool = True,
                                      actor: str | None = None,
                                      domain_id: int | None = None) -> int:
        if scope not in ("from", "to", "both"):
            raise ValueError("scope deve essere from/to/both")
        domain = domain.strip().lower()
        if not domain or "." not in domain:
            raise ValueError("Dominio non valido")
        with self.transaction() as conn:
            if domain_id:
                conn.execute(
                    """UPDATE privacy_bypass_domains
                          SET domain = ?, scope = ?, reason = ?, enabled = ?
                        WHERE id = ?""",
                    (domain, scope, reason, 1 if enabled else 0, int(domain_id)),
                )
                conn.execute(
                    """INSERT INTO privacy_bypass_audit
                           (tenant_id, target_kind, target_value, action, reason, actor)
                       VALUES (?, 'domain', ?, ?, ?, ?)""",
                    (int(tenant_id), f"{scope}:{domain}",
                     "enable" if enabled else "disable", reason, actor),
                )
                return int(domain_id)
            cur = conn.execute(
                """INSERT INTO privacy_bypass_domains
                       (tenant_id, domain, scope, reason, enabled, created_by)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (int(tenant_id), domain, scope, reason, 1 if enabled else 0, actor),
            )
            new_id = int(cur.lastrowid or 0)
            conn.execute(
                """INSERT INTO privacy_bypass_audit
                       (tenant_id, target_kind, target_value, action, reason, actor)
                   VALUES (?, 'domain', ?, 'create', ?, ?)""",
                (int(tenant_id), f"{scope}:{domain}", reason, actor),
            )
            return new_id

    def delete_privacy_bypass_domain(self, domain_id: int, *, actor: str | None = None) -> None:
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT tenant_id, domain, scope FROM privacy_bypass_domains WHERE id = ?",
                (domain_id,),
            ).fetchone()
            if not row:
                return
            conn.execute("DELETE FROM privacy_bypass_domains WHERE id = ?", (domain_id,))
            conn.execute(
                """INSERT INTO privacy_bypass_audit
                       (tenant_id, target_kind, target_value, action, actor)
                   VALUES (?, 'domain', ?, 'delete', ?)""",
                (int(row["tenant_id"]), f"{row['scope']}:{row['domain']}", actor),
            )

    def list_privacy_bypass_active(self, *, tenant_id: int | None = None) -> dict[str, Any]:
        """Struttura completa per l'endpoint listener.

        Returns:
            dict con chiavi:
              - ``from``: lista email mittenti in bypass (lower-case)
              - ``to``:   lista email destinatari in bypass (lower-case)
              - ``from_domains``: domini con scope='from' o 'both'
              - ``to_domains``:   domini con scope='to'   o 'both'
        """
        from_emails = [
            r["email_address"].lower()
            for r in self.list_addresses_privacy_bypass("from", tenant_id=tenant_id)
            if r.get("email_address")
        ]
        to_emails = [
            r["email_address"].lower()
            for r in self.list_addresses_privacy_bypass("to", tenant_id=tenant_id)
            if r.get("email_address")
        ]
        domains = self.list_privacy_bypass_domains(tenant_id=tenant_id, only_enabled=True)
        from_domains: list[str] = []
        to_domains: list[str] = []
        for d in domains:
            scope = d.get("scope", "both")
            dom = d.get("domain", "").lower()
            if not dom:
                continue
            if scope in ("from", "both"):
                from_domains.append(dom)
            if scope in ("to", "both"):
                to_domains.append(dom)
        return {
            "from": sorted(set(from_emails)),
            "to": sorted(set(to_emails)),
            "from_domains": sorted(set(from_domains)),
            "to_domains": sorted(set(to_domains)),
        }

    # ============================================================ AI ASSISTANT ===

    def list_ai_providers(self, *, tenant_id: int | None = None) -> list[dict[str, Any]]:
        where: list[str] = []
        params: list[Any] = []
        if tenant_id is not None:
            where.append("tenant_id = ?"); params.append(int(tenant_id))
        ws = "WHERE " + " AND ".join(where) if where else ""
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM ai_providers {ws} ORDER BY name", params,
            ).fetchall()
            return [dict(r) for r in rows]

    def upsert_ai_provider(self, data: dict[str, Any], *, tenant_id: int = 1,
                            actor: str | None = None) -> int:
        with self.transaction() as conn:
            pid = data.get("id")
            if pid:
                conn.execute(
                    """UPDATE ai_providers
                          SET name = ?, kind = ?, endpoint = ?, api_key_env = ?,
                              default_model = ?, enabled = ?, notes = ?
                        WHERE id = ?""",
                    (data.get("name"), data.get("kind"), data.get("endpoint"),
                     data.get("api_key_env"), data.get("default_model"),
                     1 if data.get("enabled") else 0, data.get("notes"), int(pid)),
                )
                return int(pid)
            cur = conn.execute(
                """INSERT INTO ai_providers
                       (tenant_id, name, kind, endpoint, api_key_env,
                        default_model, enabled, notes, created_by)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (int(tenant_id), data.get("name"), data.get("kind"),
                 data.get("endpoint"), data.get("api_key_env"),
                 data.get("default_model"),
                 1 if data.get("enabled", True) else 0,
                 data.get("notes"), (actor or "ui")[:100]),
            )
            return int(cur.lastrowid or 0)

    def delete_ai_provider(self, provider_id: int) -> None:
        with self.transaction() as conn:
            conn.execute("DELETE FROM ai_providers WHERE id = ?", (provider_id,))

    def list_ai_jobs(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM ai_jobs ORDER BY job_code"
            ).fetchall()
            return [dict(r) for r in rows]

    def list_ai_job_bindings(self, *, tenant_id: int | None = None,
                              job_code: str | None = None,
                              only_enabled: bool | None = None) -> list[dict[str, Any]]:
        where: list[str] = []
        params: list[Any] = []
        if tenant_id is not None:
            where.append("tenant_id = ?"); params.append(int(tenant_id))
        if job_code:
            where.append("job_code = ?"); params.append(job_code)
        if only_enabled is True:
            where.append("enabled = 1")
        ws = "WHERE " + " AND ".join(where) if where else ""
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM ai_job_bindings {ws} "
                f"ORDER BY job_code, version DESC", params,
            ).fetchall()
            return [dict(r) for r in rows]

    def upsert_ai_job_binding(self, data: dict[str, Any], *,
                               tenant_id: int = 1,
                               actor: str | None = None,
                               new_version: bool = False) -> int:
        """Inserisce o aggiorna un binding.

        Se ``new_version=True``, crea una nuova versione (incrementa version)
        e disabilita le precedenti dello stesso job_code.
        """
        with self.transaction() as conn:
            bid = data.get("id")
            if bid and not new_version:
                conn.execute(
                    """UPDATE ai_job_bindings SET
                          job_code = ?, provider_id = ?, model_id = ?,
                          system_prompt_template = ?, user_prompt_template = ?,
                          temperature = ?, max_tokens = ?, timeout_ms = ?,
                          fallback_provider_id = ?, fallback_model_id = ?,
                          traffic_split = ?, enabled = ?, notes = ?
                       WHERE id = ?""",
                    (data.get("job_code"), data.get("provider_id"), data.get("model_id"),
                     data.get("system_prompt_template"), data.get("user_prompt_template"),
                     float(data.get("temperature", 0.0) or 0.0),
                     int(data.get("max_tokens", 1024) or 1024),
                     data.get("timeout_ms"),
                     data.get("fallback_provider_id"), data.get("fallback_model_id"),
                     int(data.get("traffic_split", 100) or 100),
                     1 if data.get("enabled") else 0,
                     data.get("notes"), int(bid)),
                )
                return int(bid)
            # Calcola la nuova version
            row = conn.execute(
                "SELECT MAX(version) AS v FROM ai_job_bindings WHERE tenant_id = ? AND job_code = ?",
                (int(tenant_id), data.get("job_code")),
            ).fetchone()
            next_version = int((row["v"] if row and row["v"] else 0)) + 1
            if new_version:
                conn.execute(
                    "UPDATE ai_job_bindings SET enabled = 0 WHERE tenant_id = ? AND job_code = ?",
                    (int(tenant_id), data.get("job_code")),
                )
            cur = conn.execute(
                """INSERT INTO ai_job_bindings
                       (tenant_id, job_code, provider_id, model_id,
                        system_prompt_template, user_prompt_template,
                        temperature, max_tokens, timeout_ms,
                        fallback_provider_id, fallback_model_id,
                        traffic_split, enabled, version, notes, created_by)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (int(tenant_id), data.get("job_code"), data.get("provider_id"),
                 data.get("model_id"),
                 data.get("system_prompt_template"), data.get("user_prompt_template"),
                 float(data.get("temperature", 0.0) or 0.0),
                 int(data.get("max_tokens", 1024) or 1024),
                 data.get("timeout_ms"),
                 data.get("fallback_provider_id"), data.get("fallback_model_id"),
                 int(data.get("traffic_split", 100) or 100),
                 1 if data.get("enabled", True) else 0,
                 next_version, data.get("notes"), (actor or "ui")[:100]),
            )
            return int(cur.lastrowid or 0)

    def insert_ai_decision(self, data: dict[str, Any]) -> int:
        am = data.get("suggested_actions_json")
        if isinstance(am, (dict, list)):
            am = json.dumps(am, ensure_ascii=False)
        ro = data.get("raw_output_json")
        if isinstance(ro, (dict, list)):
            ro = json.dumps(ro, ensure_ascii=False)
        with self.transaction() as conn:
            cur = conn.execute(
                """INSERT INTO ai_decisions
                       (tenant_id, event_uuid, job_code, binding_id,
                        provider, model, prompt_hash, pii_redactions_count,
                        classification, urgenza_proposta, intent, summary,
                        suggested_actions_json, raw_output_json, confidence,
                        latency_ms, input_tokens, output_tokens, cost_usd,
                        applied, shadow_mode, error, fallback_used, applied_by)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                           ?, ?, ?, ?, ?)""",
                (
                    int(data.get("tenant_id", 1)),
                    data.get("event_uuid"),
                    data.get("job_code"),
                    data.get("binding_id"),
                    data.get("provider"),
                    data.get("model"),
                    data.get("prompt_hash"),
                    int(data.get("pii_redactions_count") or 0),
                    data.get("classification"),
                    data.get("urgenza_proposta"),
                    data.get("intent"),
                    data.get("summary"),
                    am, ro,
                    data.get("confidence"),
                    data.get("latency_ms"),
                    data.get("input_tokens"),
                    data.get("output_tokens"),
                    data.get("cost_usd"),
                    1 if data.get("applied") else 0,
                    1 if data.get("shadow_mode") else 0,
                    data.get("error"),
                    1 if data.get("fallback_used") else 0,
                    data.get("applied_by"),
                ),
            )
            return int(cur.lastrowid or 0)

    def list_ai_decisions(self, *, tenant_id: int | None = None,
                           job_code: str | None = None,
                           hours: int | None = None,
                           limit: int = 100) -> list[dict[str, Any]]:
        where: list[str] = []
        params: list[Any] = []
        if tenant_id is not None:
            where.append("tenant_id = ?"); params.append(int(tenant_id))
        if job_code:
            where.append("job_code = ?"); params.append(job_code)
        if hours:
            where.append("created_at >= datetime('now', ?)")
            params.append(f"-{int(hours)} hours")
        ws = "WHERE " + " AND ".join(where) if where else ""
        params.append(int(limit))
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM ai_decisions {ws} "
                f"ORDER BY created_at DESC LIMIT ?", params,
            ).fetchall()
            return [_decode_ai_decision(r) for r in rows]

    def get_ai_decision(self, decision_id: int) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM ai_decisions WHERE id = ?", (decision_id,),
            ).fetchone()
            return _decode_ai_decision(row) if row else None

    def sum_ai_decisions_cost_today(self, *, tenant_id: int = 1,
                                     day: str | None = None) -> float:
        from datetime import date as _date
        d = day or _date.today().isoformat()
        with self._connect() as conn:
            row = conn.execute(
                """SELECT COALESCE(SUM(cost_usd), 0) AS s
                     FROM ai_decisions
                    WHERE tenant_id = ? AND DATE(created_at) = ?""",
                (int(tenant_id), d),
            ).fetchone()
            return float(row["s"] or 0)

    def list_ai_pii_dictionary(self, *, tenant_id: int | None = None) -> list[dict[str, Any]]:
        where: list[str] = []
        params: list[Any] = []
        if tenant_id is not None:
            where.append("tenant_id = ?"); params.append(int(tenant_id))
        ws = "WHERE " + " AND ".join(where) if where else ""
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM ai_pii_dictionary {ws} ORDER BY kind, value", params,
            ).fetchall()
            return [dict(r) for r in rows]

    # ============================================ AI RULE PROPOSALS (F3.5) ===

    def list_ai_rule_proposals(self, *, tenant_id: int | None = None,
                                 state: str | None = None,
                                 limit: int = 200) -> list[dict[str, Any]]:
        where: list[str] = []
        params: list[Any] = []
        if tenant_id is not None:
            where.append("tenant_id = ?"); params.append(int(tenant_id))
        if state:
            where.append("state = ?"); params.append(state)
        ws = "WHERE " + " AND ".join(where) if where else ""
        params.append(int(limit))
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM ai_rule_proposals {ws} "
                f"ORDER BY created_at DESC LIMIT ?", params,
            ).fetchall()
            return [_decode_ai_proposal(r) for r in rows]

    def get_ai_rule_proposal(self, proposal_id: int) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM ai_rule_proposals WHERE id = ?", (proposal_id,),
            ).fetchone()
            return _decode_ai_proposal(row) if row else None

    def upsert_ai_rule_proposal(self, data: dict[str, Any]) -> int:
        """Insert/update di una proposta. Idempotente per fingerprint_hex
        (UNIQUE non strict ma logico via dedup nel proposer)."""
        am = data.get("suggested_action_map_json")
        if isinstance(am, (dict, list)):
            am = json.dumps(am, ensure_ascii=False)
        pid = data.get("id")
        with self.transaction() as conn:
            if pid:
                fields = []
                values: list[Any] = []
                for col in ("suggested_match_subject", "suggested_match_from",
                              "suggested_match_to", "suggested_match_in_service",
                              "suggested_match_contract_active", "suggested_action",
                              "confidence", "evidence_decision_ids", "sample_subjects",
                              "state", "accepted_rule_id", "reviewer", "review_notes",
                              "fingerprint_hex"):
                    if col in data:
                        fields.append(f"{col} = ?")
                        values.append(data[col])
                if "suggested_action_map_json" in data:
                    fields.append("suggested_action_map_json = ?")
                    values.append(am)
                if data.get("review_at") == "datetime('now')":
                    fields.append("review_at = datetime('now')")
                if not fields:
                    return int(pid)
                values.append(int(pid))
                conn.execute(
                    f"UPDATE ai_rule_proposals SET {', '.join(fields)} WHERE id = ?",
                    values,
                )
                return int(pid)
            cur = conn.execute(
                """INSERT INTO ai_rule_proposals
                       (tenant_id, fingerprint_hex, suggested_match_subject,
                        suggested_match_from, suggested_match_to,
                        suggested_match_in_service, suggested_match_contract_active,
                        suggested_action, suggested_action_map_json, confidence,
                        evidence_decision_ids, sample_subjects, state)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    int(data.get("tenant_id", 1)),
                    data.get("fingerprint_hex"),
                    data.get("suggested_match_subject"),
                    data.get("suggested_match_from"),
                    data.get("suggested_match_to"),
                    data.get("suggested_match_in_service"),
                    data.get("suggested_match_contract_active"),
                    data.get("suggested_action"),
                    am,
                    data.get("confidence"),
                    data.get("evidence_decision_ids"),
                    data.get("sample_subjects"),
                    data.get("state", "pending"),
                ),
            )
            return int(cur.lastrowid or 0)

    # ============================================ AI ERROR CLUSTERS (F2) ===

    def list_ai_error_clusters(self, *, tenant_id: int | None = None,
                                states: tuple[str, ...] | None = None,
                                limit: int = 200) -> list[dict[str, Any]]:
        where: list[str] = []
        params: list[Any] = []
        if tenant_id is not None:
            where.append("tenant_id = ?"); params.append(int(tenant_id))
        if states:
            placeholders = ",".join("?" * len(states))
            where.append(f"state IN ({placeholders})")
            params.extend(states)
        ws = "WHERE " + " AND ".join(where) if where else ""
        params.append(int(limit))
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM ai_error_clusters {ws} "
                f"ORDER BY last_seen DESC LIMIT ?", params,
            ).fetchall()
            return [dict(r) for r in rows]

    def get_ai_error_cluster(self, cluster_id: int) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM ai_error_clusters WHERE id = ?", (cluster_id,),
            ).fetchone()
            return dict(row) if row else None

    def upsert_ai_error_cluster(self, data: dict[str, Any]) -> int:
        """Insert/update di un cluster errori.

        Per update: passa ``id`` + i campi da modificare (gli altri restano).
        Per insert: passa almeno ``tenant_id`` + ``fingerprint_hex`` +
        ``representative_subject``.
        """
        cid = data.get("id")
        with self.transaction() as conn:
            if cid:
                # Update parziale (solo campi forniti)
                fields = []
                values: list[Any] = []
                for col in ("count", "state", "manual_threshold",
                              "manual_recovery_window_min", "ticket_id",
                              "representative_subject", "representative_body_excerpt",
                              "notes", "fingerprint_hex"):
                    if col in data:
                        fields.append(f"{col} = ?")
                        values.append(data[col])
                # Campi datetime gestiti come now() se passati come stringa magica
                if data.get("last_seen") == "datetime('now')":
                    fields.append("last_seen = datetime('now')")
                if data.get("recovery_seen_at") == "datetime('now')":
                    fields.append("recovery_seen_at = datetime('now')")
                if not fields:
                    return int(cid)
                values.append(int(cid))
                conn.execute(
                    f"UPDATE ai_error_clusters SET {', '.join(fields)} WHERE id = ?",
                    values,
                )
                return int(cid)
            cur = conn.execute(
                """INSERT INTO ai_error_clusters
                       (tenant_id, fingerprint_hex, representative_subject,
                        representative_body_excerpt, count, state,
                        manual_threshold, manual_recovery_window_min, notes)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    int(data.get("tenant_id", 1)),
                    data.get("fingerprint_hex"),
                    data.get("representative_subject"),
                    data.get("representative_body_excerpt"),
                    int(data.get("count", 1)),
                    data.get("state", "accumulating"),
                    int(data.get("manual_threshold", 5)),
                    int(data.get("manual_recovery_window_min", 60)),
                    data.get("notes"),
                ),
            )
            return int(cur.lastrowid or 0)

    def insert_ai_shadow_audit(self, *, tenant_id: int, transition: str,
                                actor: str | None = None,
                                decisions_seen: int = 0,
                                avg_confidence: float | None = None,
                                notes: str | None = None) -> int:
        with self.transaction() as conn:
            cur = conn.execute(
                """INSERT INTO ai_shadow_audit
                       (tenant_id, transition, actor, decisions_seen, avg_confidence, notes)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (int(tenant_id), transition, actor, int(decisions_seen),
                 avg_confidence, notes),
            )
            return int(cur.lastrowid or 0)

    def list_ai_shadow_audit(self, *, tenant_id: int | None = None,
                              limit: int = 50) -> list[dict[str, Any]]:
        where: list[str] = []
        params: list[Any] = []
        if tenant_id is not None:
            where.append("tenant_id = ?"); params.append(int(tenant_id))
        ws = "WHERE " + " AND ".join(where) if where else ""
        params.append(int(limit))
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM ai_shadow_audit {ws} ORDER BY at DESC LIMIT ?",
                params,
            ).fetchall()
            return [dict(r) for r in rows]

    def upsert_ai_pii_dictionary_entry(self, *, tenant_id: int, kind: str,
                                         value: str, replacement: str,
                                         source: str = "manual") -> int:
        with self.transaction() as conn:
            existing = conn.execute(
                "SELECT id FROM ai_pii_dictionary WHERE tenant_id = ? AND value = ?",
                (int(tenant_id), value),
            ).fetchone()
            if existing:
                conn.execute(
                    """UPDATE ai_pii_dictionary
                          SET kind = ?, replacement = ?, source = ?,
                              last_seen_at = datetime('now')
                        WHERE id = ?""",
                    (kind, replacement, source, int(existing[0])),
                )
                return int(existing[0])
            cur = conn.execute(
                """INSERT INTO ai_pii_dictionary
                       (tenant_id, kind, value, replacement, source)
                   VALUES (?, ?, ?, ?, ?)""",
                (int(tenant_id), kind, value, replacement, source),
            )
            return int(cur.lastrowid or 0)

    # ============================================ API KEYS (cifrate) ===

    def list_api_keys(self, *, tenant_id: int | None = None,
                       only_enabled: bool | None = None) -> list[dict[str, Any]]:
        where: list[str] = []
        params: list[Any] = []
        if tenant_id is not None:
            where.append("tenant_id = ?"); params.append(int(tenant_id))
        if only_enabled is True:
            where.append("enabled = 1")
        ws = "WHERE " + " AND ".join(where) if where else ""
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM api_keys {ws} ORDER BY env_var_name", params,
            ).fetchall()
            return [dict(r) for r in rows]

    def get_api_key(self, key_id: int) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM api_keys WHERE id = ?", (key_id,)).fetchone()
            return dict(row) if row else None

    def upsert_api_key(self, *, tenant_id: int, name: str, env_var_name: str,
                        value_encrypted: bytes, masked_preview: str,
                        description: str | None = None, enabled: bool = True,
                        actor: str | None = None,
                        key_id: int | None = None) -> int:
        with self.transaction() as conn:
            if key_id:
                conn.execute(
                    """UPDATE api_keys SET name = ?, env_var_name = ?,
                          value_encrypted = ?, masked_preview = ?,
                          description = ?, enabled = ?,
                          last_rotated_at = datetime('now')
                       WHERE id = ?""",
                    (name, env_var_name, value_encrypted, masked_preview,
                     description, 1 if enabled else 0, int(key_id)),
                )
                return int(key_id)
            cur = conn.execute(
                """INSERT INTO api_keys
                       (tenant_id, name, env_var_name, value_encrypted,
                        masked_preview, description, enabled, created_by)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (int(tenant_id), name, env_var_name, value_encrypted,
                 masked_preview, description, 1 if enabled else 0,
                 (actor or "ui")[:100]),
            )
            return int(cur.lastrowid or 0)

    def delete_api_key(self, key_id: int) -> None:
        with self.transaction() as conn:
            conn.execute("DELETE FROM api_keys WHERE id = ?", (key_id,))

    def toggle_api_key(self, key_id: int) -> bool:
        with self.transaction() as conn:
            cur = conn.execute(
                "UPDATE api_keys SET enabled = 1 - enabled WHERE id = ? RETURNING enabled",
                (key_id,),
            )
            row = cur.fetchone()
            return bool(row[0]) if row else False

    # ============================================ MODULE INSTALL LOG ===

    def list_module_install_log(self, *, module_code: str | None = None,
                                  limit: int = 50) -> list[dict[str, Any]]:
        where: list[str] = []
        params: list[Any] = []
        if module_code:
            where.append("module_code = ?"); params.append(module_code)
        ws = "WHERE " + " AND ".join(where) if where else ""
        params.append(int(limit))
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM module_install_log {ws} "
                f"ORDER BY started_at DESC LIMIT ?", params,
            ).fetchall()
            return [dict(r) for r in rows]

    def insert_module_install_log(self, *, module_code: str, operation: str,
                                    status: str = "running",
                                    output: str | None = None,
                                    return_code: int | None = None,
                                    duration_ms: int | None = None,
                                    actor: str | None = None) -> int:
        with self.transaction() as conn:
            cur = conn.execute(
                """INSERT INTO module_install_log
                       (module_code, operation, status, output, return_code,
                        duration_ms, actor, finished_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (module_code, operation, status, output, return_code,
                 duration_ms, actor,
                 None if status == "running" else "datetime('now')"),
            )
            return int(cur.lastrowid or 0)

    def update_module_install_log(self, log_id: int, *, status: str,
                                    output: str | None = None,
                                    return_code: int | None = None,
                                    duration_ms: int | None = None) -> None:
        with self.transaction() as conn:
            conn.execute(
                """UPDATE module_install_log SET
                       status = ?, output = COALESCE(?, output),
                       return_code = COALESCE(?, return_code),
                       duration_ms = COALESCE(?, duration_ms),
                       finished_at = datetime('now')
                    WHERE id = ?""",
                (status, output, return_code, duration_ms, int(log_id)),
            )

    def list_privacy_bypass_audit(self, *, tenant_id: int | None = None,
                                   limit: int = 200) -> list[dict[str, Any]]:
        where = ["1=1"]
        params: list[Any] = []
        if tenant_id is not None:
            where.append("tenant_id = ?"); params.append(int(tenant_id))
        params.append(int(limit))
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM privacy_bypass_audit WHERE {' AND '.join(where)} "
                f"ORDER BY at DESC LIMIT ?", params,
            ).fetchall()
            return [dict(r) for r in rows]

    # ============================================================ SETTINGS ===

    def list_settings(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT key, value, description, updated_at FROM settings ORDER BY key"
            ).fetchall()
            return [dict(r) for r in rows]

    def get_setting(self, key: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
            return row["value"] if row else None

    def upsert_setting(self, key: str, value: str, description: str | None = None) -> None:
        with self.transaction() as conn:
            conn.execute(
                """INSERT INTO settings (key, value, description, updated_at)
                   VALUES (?, ?, ?, datetime('now'))
                   ON CONFLICT(key) DO UPDATE SET
                       value = excluded.value,
                       description = COALESCE(excluded.description, description),
                       updated_at = datetime('now')""",
                (key, value, description),
            )

    def list_occurrences(self, *, tenant_id: int | None = None,
                         aggregation_id: int | None = None,
                         filter_state: str = "all",
                         limit: int = 200) -> list[dict[str, Any]]:
        where: list[str] = []
        params: list[Any] = []
        if tenant_id is not None:
            where.append("o.tenant_id = ?"); params.append(int(tenant_id))
        if aggregation_id:
            where.append("o.aggregation_id = ?"); params.append(int(aggregation_id))
        if filter_state == "active":
            where.append("o.current_count > 0")
        elif filter_state == "with_ticket":
            where.append("o.ticket_id IS NOT NULL")
        elif filter_state == "above":
            where.append("o.current_count >= a.threshold")
        elif filter_state == "recent_reset":
            where.append("o.last_reset_at > datetime('now', '-24 hours')")
        where_sql = "WHERE " + " AND ".join(where) if where else ""
        params.append(int(limit))
        with self._connect() as conn:
            rows = conn.execute(
                f"""SELECT o.*, a.name AS aggregation_name,
                          a.threshold AS aggregation_threshold,
                          a.window_hours AS aggregation_window_hours
                     FROM error_occurrences o
                     JOIN error_aggregations a ON a.id = o.aggregation_id
                     {where_sql}
                    ORDER BY o.last_seen DESC LIMIT ?""", params,
            ).fetchall()
            return [dict(r) for r in rows]

    # ============================================================ customer_groups

    def list_customer_groups(self, *, tenant_id: int = 1,
                              only_enabled: bool = False) -> list[dict[str, Any]]:
        """Lista gruppi clienti con conteggio membri."""
        sql = """
            SELECT g.*, COUNT(m.id) AS member_count
              FROM customer_groups g
              LEFT JOIN customer_group_members m ON m.group_id = g.id
             WHERE g.tenant_id = ?
        """
        params: list[Any] = [int(tenant_id)]
        if only_enabled:
            sql += " AND g.enabled = 1"
        sql += " GROUP BY g.id ORDER BY g.name COLLATE NOCASE"
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]

    def get_customer_group(self, group_id: int) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM customer_groups WHERE id = ?", (int(group_id),)
            ).fetchone()
            return dict(row) if row else None

    def get_customer_group_by_code(self, code: str, *,
                                    tenant_id: int = 1) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM customer_groups WHERE tenant_id = ? AND code = ?",
                (int(tenant_id), code),
            ).fetchone()
            return dict(row) if row else None

    def upsert_customer_group(self, *, group_id: int | None = None,
                               tenant_id: int = 1,
                               code: str, name: str,
                               description: str | None = None,
                               color: str | None = None,
                               enabled: bool = True,
                               actor: str | None = None) -> int:
        """Crea o aggiorna un gruppo. Ritorna l'id."""
        code = (code or "").strip().lower()
        name = (name or "").strip()
        if not code or not name:
            raise ValueError("code e name obbligatori")
        with self.transaction() as conn:
            if group_id:
                conn.execute(
                    """UPDATE customer_groups
                          SET code = ?, name = ?, description = ?, color = ?,
                              enabled = ?, updated_at = datetime('now')
                        WHERE id = ? AND tenant_id = ?""",
                    (code, name, description, color, 1 if enabled else 0,
                     int(group_id), int(tenant_id)),
                )
                return int(group_id)
            cur = conn.execute(
                """INSERT INTO customer_groups
                       (tenant_id, code, name, description, color, enabled, created_by)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (int(tenant_id), code, name, description, color,
                 1 if enabled else 0, actor),
            )
            return int(cur.lastrowid or 0)

    def delete_customer_group(self, group_id: int) -> None:
        with self.transaction() as conn:
            conn.execute("DELETE FROM customer_groups WHERE id = ?", (int(group_id),))

    def list_group_members(self, group_id: int) -> list[dict[str, Any]]:
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(
                """SELECT id, codice_cliente, added_at, added_by
                     FROM customer_group_members
                    WHERE group_id = ?
                    ORDER BY codice_cliente""", (int(group_id),)
            ).fetchall()]

    def list_groups_for_customer(self, codice_cliente: str, *,
                                  tenant_id: int = 1) -> list[dict[str, Any]]:
        """Tutti i gruppi a cui appartiene un cliente."""
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(
                """SELECT g.*
                     FROM customer_groups g
                     JOIN customer_group_members m ON m.group_id = g.id
                    WHERE g.tenant_id = ? AND m.codice_cliente = ?
                    ORDER BY g.name COLLATE NOCASE""",
                (int(tenant_id), codice_cliente),
            ).fetchall()]

    def set_customer_groups(self, codice_cliente: str, group_ids: list[int], *,
                             tenant_id: int = 1, actor: str | None = None) -> int:
        """Imposta atomicamente i gruppi di un cliente. Returns count."""
        target_ids = set(int(g) for g in (group_ids or []) if g)
        with self.transaction() as conn:
            current = {
                int(r[0]): int(r[1]) for r in conn.execute(
                    "SELECT id, group_id FROM customer_group_members "
                    "WHERE tenant_id = ? AND codice_cliente = ?",
                    (int(tenant_id), codice_cliente),
                ).fetchall()
            }
            current_ids = set(current.values())
            to_remove = current_ids - target_ids
            to_add = target_ids - current_ids
            for gid in to_remove:
                conn.execute(
                    "DELETE FROM customer_group_members "
                    "WHERE tenant_id = ? AND codice_cliente = ? AND group_id = ?",
                    (int(tenant_id), codice_cliente, int(gid)),
                )
            for gid in to_add:
                conn.execute(
                    """INSERT OR IGNORE INTO customer_group_members
                           (tenant_id, group_id, codice_cliente, added_by)
                       VALUES (?, ?, ?, ?)""",
                    (int(tenant_id), int(gid), codice_cliente, actor),
                )
            return len(target_ids)

    def list_all_customer_group_memberships(self, *,
                                             tenant_id: int = 1) -> list[dict[str, Any]]:
        """Per il sync verso il listener: ogni riga {codice_cliente, group_code}."""
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(
                """SELECT m.codice_cliente, g.code AS group_code, g.id AS group_id
                     FROM customer_group_members m
                     JOIN customer_groups g ON g.id = m.group_id
                    WHERE m.tenant_id = ? AND g.enabled = 1""",
                (int(tenant_id),),
            ).fetchall()]


# ============================================================= HELPERS ===

def _decode_rule(row) -> dict[str, Any]:
    d = dict(row)
    if d.get("action_map") and isinstance(d["action_map"], str):
        try:
            d["action_map"] = json.loads(d["action_map"])
        except (TypeError, ValueError):
            d["action_map"] = {}
    return d


def _decode_ai_decision(row) -> dict[str, Any]:
    d = dict(row)
    for k in ("suggested_actions_json", "raw_output_json"):
        if d.get(k) and isinstance(d[k], str):
            try:
                d[k] = json.loads(d[k])
            except (TypeError, ValueError):
                d[k] = {}
    return d


def _decode_ai_proposal(row) -> dict[str, Any]:
    d = dict(row)
    if d.get("suggested_action_map_json") and isinstance(d["suggested_action_map_json"], str):
        try:
            d["suggested_action_map_json"] = json.loads(d["suggested_action_map_json"])
        except (TypeError, ValueError):
            d["suggested_action_map_json"] = {}
    return d


def _decode_template(row) -> dict[str, Any]:
    d = dict(row)
    if d.get("attachment_paths") and isinstance(d["attachment_paths"], str):
        try:
            d["attachment_paths"] = json.loads(d["attachment_paths"])
        except (TypeError, ValueError):
            d["attachment_paths"] = []
    return d


def _decode_event(row) -> dict[str, Any]:
    d = dict(row)
    if d.get("payload_metadata") and isinstance(d["payload_metadata"], str):
        try:
            d["payload_metadata"] = json.loads(d["payload_metadata"])
        except (TypeError, ValueError):
            pass
    return d


def _decode_service_hours(row) -> dict[str, Any]:
    d = dict(row)
    for k in ("schedule", "holidays", "schedule_exceptions"):
        if d.get(k) and isinstance(d[k], str):
            try:
                d[k] = json.loads(d[k])
            except (TypeError, ValueError):
                d[k] = [] if k != "schedule" else {}
    return d


def _decode_profile(row) -> dict[str, Any]:
    d = dict(row)
    for k in ("schedule", "holidays"):
        if d.get(k) and isinstance(d[k], str):
            try:
                d[k] = json.loads(d[k])
            except (TypeError, ValueError):
                d[k] = [] if k != "schedule" else {}
    # Boolean coercion sui flag (SQLite restituisce int)
    for k in ("holidays_auto", "is_builtin", "enabled", "exclude_holidays",
              "requires_authorization_always", "authorize_outside_hours"):
        if k in d and d[k] is not None:
            d[k] = bool(d[k])
    return d
