"""DAO SQLite per il relay.

Persistenza locale: cache anagrafica/regole/routes scaricate via API, code outbound/dispatch,
log eventi/quarantena/forward. Tutta la persistenza vive qui — nessun accesso al DB del manager.

WAL mode + foreign keys + busy_timeout per gestire l'accesso concorrente di listener e scheduler.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS customers_cache (
    codcli              TEXT PRIMARY KEY,
    ragione_sociale     TEXT,
    domains_json        TEXT NOT NULL DEFAULT '[]',
    aliases_json        TEXT NOT NULL DEFAULT '[]',
    contract_active     INTEGER NOT NULL DEFAULT 0,
    service_hours_json  TEXT,
    synced_at           TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_customers_synced ON customers_cache(synced_at);

CREATE TABLE IF NOT EXISTS rules_cache (
    id                       INTEGER PRIMARY KEY,
    name                     TEXT,
    applies_to               TEXT NOT NULL,
    scope_type               TEXT,
    scope_ref                TEXT,
    priority                 INTEGER NOT NULL DEFAULT 100,
    enabled                  INTEGER NOT NULL DEFAULT 1,
    match_from_regex         TEXT,
    match_from_domain        TEXT,
    match_to_regex           TEXT,
    match_to_domain          TEXT,
    match_subject_regex      TEXT,
    match_body_regex         TEXT,
    match_at_hours           TEXT,
    match_in_service         INTEGER,
    match_contract_active    INTEGER,
    match_known_customer     INTEGER,
    match_has_exception_today INTEGER,
    match_customer_groups    TEXT,                                -- CSV "top,sanita"
    match_tag                TEXT,
    action                   TEXT NOT NULL,
    action_map_json          TEXT NOT NULL DEFAULT '{}',
    continue_after_match     INTEGER NOT NULL DEFAULT 0,
    synced_at                TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_rules_priority ON rules_cache(applies_to, enabled, priority);

-- Customer groups (admin migration 018): cache locale per match
-- per gruppo nelle regole (match_customer_groups).
CREATE TABLE IF NOT EXISTS customer_groups_cache (
    id          INTEGER PRIMARY KEY,
    code        TEXT NOT NULL UNIQUE,
    name        TEXT,
    enabled     INTEGER NOT NULL DEFAULT 1,
    synced_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS customer_group_members_cache (
    codcli      TEXT NOT NULL,
    group_code  TEXT NOT NULL,
    PRIMARY KEY (codcli, group_code)
);
CREATE INDEX IF NOT EXISTS idx_customer_group_members_codcli
    ON customer_group_members_cache(codcli);

-- Privacy bypass list (admin migration 011): indirizzi/domini esclusi dal
-- rule engine. Cached come tabelle dedicate per lookup O(1) durante la
-- pipeline. Sostituite atomicamente al sync.
CREATE TABLE IF NOT EXISTS privacy_bypass_cache (
    kind     TEXT NOT NULL CHECK (kind IN ('from_email', 'to_email', 'from_domain', 'to_domain')),
    value    TEXT NOT NULL,
    synced_at TEXT NOT NULL,
    PRIMARY KEY (kind, value)
);
CREATE INDEX IF NOT EXISTS idx_privacy_bypass_cache_kind ON privacy_bypass_cache(kind);

CREATE TABLE IF NOT EXISTS routes_cache (
    local_part      TEXT NOT NULL,
    domain          TEXT NOT NULL,
    codcli          TEXT,
    default_action  TEXT,
    forward_target  TEXT,
    forward_port    INTEGER,
    forward_tls     TEXT,
    redirect_target TEXT,
    enabled         INTEGER NOT NULL DEFAULT 1,
    apply_rules     INTEGER NOT NULL DEFAULT 1,
    notes           TEXT,
    PRIMARY KEY (local_part, domain)
);

CREATE TABLE IF NOT EXISTS outbound_queue (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    event_uuid       TEXT NOT NULL,
    action           TEXT NOT NULL,
    mime_blob        BLOB NOT NULL,
    mail_from        TEXT,
    rcpt_to_json     TEXT NOT NULL,
    smarthost        TEXT NOT NULL,
    smarthost_port   INTEGER NOT NULL,
    smarthost_tls    TEXT NOT NULL DEFAULT 'opportunistic',
    state            TEXT NOT NULL DEFAULT 'pending',
    attempts         INTEGER NOT NULL DEFAULT 0,
    next_attempt_at  TEXT,
    last_error       TEXT,
    delivered_at     TEXT,
    created_at       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_outbound_pending
    ON outbound_queue(state, next_attempt_at)
    WHERE state IN ('pending','error');

CREATE TABLE IF NOT EXISTS dispatch_queue (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    event_uuid      TEXT NOT NULL,
    payload_json    TEXT NOT NULL,
    state           TEXT NOT NULL DEFAULT 'pending',
    attempts        INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT,
    last_error      TEXT,
    manager_response TEXT,
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_dispatch_pending
    ON dispatch_queue(state, next_attempt_at)
    WHERE state IN ('pending','error');

CREATE TABLE IF NOT EXISTS events_log (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    event_uuid         TEXT NOT NULL UNIQUE,
    received_at        TEXT NOT NULL,
    from_address       TEXT,
    to_address         TEXT,
    subject            TEXT,
    message_id         TEXT,
    codcli             TEXT,
    action_taken       TEXT,
    rule_id            INTEGER,
    ticket_id          TEXT,
    payload_metadata   TEXT,
    sent_to_manager_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_events_unsent ON events_log(sent_to_manager_at)
    WHERE sent_to_manager_at IS NULL;

CREATE TABLE IF NOT EXISTS quarantine (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    event_uuid   TEXT NOT NULL,
    mime_blob    BLOB NOT NULL,
    reason       TEXT NOT NULL,
    from_address TEXT,
    to_address   TEXT,
    decision     TEXT NOT NULL DEFAULT 'pending',
    reviewed_at  TEXT,
    notes        TEXT,
    created_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_quarantine_decision ON quarantine(decision, created_at DESC);

CREATE TABLE IF NOT EXISTS forward_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    queue_id      INTEGER,
    event_uuid    TEXT,
    attempted_at  TEXT NOT NULL,
    smarthost     TEXT,
    smtp_response TEXT,
    ok            INTEGER NOT NULL,
    duration_ms   INTEGER
);

CREATE INDEX IF NOT EXISTS idx_forwardlog_event ON forward_log(event_uuid, attempted_at DESC);

CREATE TABLE IF NOT EXISTS rate_buckets (
    bucket_key TEXT PRIMARY KEY,
    count      INTEGER NOT NULL,
    window_start TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sync_meta (
    name         TEXT PRIMARY KEY,
    last_sync_at TEXT,
    etag         TEXT,
    payload_hash TEXT
);

CREATE TABLE IF NOT EXISTS settings_cache (
    key         TEXT PRIMARY KEY,
    value_json  TEXT NOT NULL,
    description TEXT,
    synced_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS domain_routing_cache (
    domain         TEXT PRIMARY KEY,
    smarthost      TEXT NOT NULL,
    smarthost_port INTEGER NOT NULL DEFAULT 25,
    smarthost_tls  TEXT NOT NULL DEFAULT 'opportunistic',
    enabled        INTEGER NOT NULL DEFAULT 1,
    apply_rules    INTEGER NOT NULL DEFAULT 1,
    notes          TEXT,
    synced_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS templates_cache (
    id               INTEGER PRIMARY KEY,
    name             TEXT NOT NULL,
    description      TEXT,
    subject_tmpl     TEXT NOT NULL,
    body_html_tmpl   TEXT,
    body_text_tmpl   TEXT,
    reply_from_name  TEXT,
    reply_from_email TEXT,
    synced_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_templates_name ON templates_cache(LOWER(name));

CREATE TABLE IF NOT EXISTS aggregations_cache (
    id                       INTEGER PRIMARY KEY,
    name                     TEXT NOT NULL,
    description              TEXT,
    match_from_regex         TEXT,
    match_subject_regex      TEXT,
    match_body_regex         TEXT,
    fingerprint_template     TEXT NOT NULL,
    threshold                INTEGER NOT NULL DEFAULT 2,
    consecutive_only         INTEGER NOT NULL DEFAULT 0,
    window_hours             INTEGER NOT NULL DEFAULT 24,
    reset_subject_regex      TEXT,
    reset_from_regex         TEXT,
    ticket_settore           TEXT,
    ticket_urgenza           TEXT,
    ticket_codice_cliente    TEXT,
    priority                 INTEGER NOT NULL DEFAULT 100,
    synced_at                TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_aggregations_priority ON aggregations_cache(priority);

CREATE TABLE IF NOT EXISTS error_occurrences_local (
    aggregation_id           INTEGER NOT NULL,
    fingerprint              TEXT NOT NULL,
    current_count            INTEGER NOT NULL DEFAULT 1,
    first_seen               TEXT NOT NULL,
    last_seen                TEXT NOT NULL,
    sample_from              TEXT,
    sample_subject           TEXT,
    sample_received_at       TEXT,
    sample_message_id        TEXT,
    ticket_opened_at         TEXT,
    ticket_id                TEXT,
    last_reset_at            TEXT,
    total_resets             INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (aggregation_id, fingerprint)
);
CREATE INDEX IF NOT EXISTS idx_occurrences_active
    ON error_occurrences_local(aggregation_id, last_seen DESC);

-- H24 multi-brand: cache mappatura source_domain → h24_alias.
-- Sync periodico da admin via /api/v1/relay/h24-targets/active.
-- Usato da auto_reply per popolare {{ h24_inbound_alias }} basato sul
-- dominio del MITTENTE (Fase E).
CREATE TABLE IF NOT EXISTS h24_targets_cache (
    id              INTEGER PRIMARY KEY,
    source_domain   TEXT NOT NULL UNIQUE,
    h24_alias       TEXT NOT NULL,
    urgent_fee_eur  INTEGER,
    enabled         INTEGER NOT NULL DEFAULT 1,
    synced_at       TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_h24_targets_active
    ON h24_targets_cache(source_domain) WHERE enabled = 1;
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Storage:
    def __init__(self, sqlite_path: str | Path):
        self._path = Path(sqlite_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, isolation_level=None, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA synchronous = NORMAL;")
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute("PRAGMA busy_timeout = 5000;")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            # Mini-migration: aggiunge colonne nuove a tabelle preesistenti.
            # SQLite non supporta `IF NOT EXISTS` su ALTER, gestiamo via try/except.
            for tbl, col, ddl in (
                ("rules_cache", "match_to_regex", "ALTER TABLE rules_cache ADD COLUMN match_to_regex TEXT"),
                ("rules_cache", "match_from_domain", "ALTER TABLE rules_cache ADD COLUMN match_from_domain TEXT"),
                ("rules_cache", "match_contract_active", "ALTER TABLE rules_cache ADD COLUMN match_contract_active INTEGER"),
                ("rules_cache", "match_known_customer", "ALTER TABLE rules_cache ADD COLUMN match_known_customer INTEGER"),
                ("rules_cache", "match_has_exception_today", "ALTER TABLE rules_cache ADD COLUMN match_has_exception_today INTEGER"),
                ("rules_cache", "match_customer_groups", "ALTER TABLE rules_cache ADD COLUMN match_customer_groups TEXT"),
                ("rules_cache", "match_tag", "ALTER TABLE rules_cache ADD COLUMN match_tag TEXT"),
                ("routes_cache", "apply_rules", "ALTER TABLE routes_cache ADD COLUMN apply_rules INTEGER NOT NULL DEFAULT 1"),
                ("domain_routing_cache", "apply_rules", "ALTER TABLE domain_routing_cache ADD COLUMN apply_rules INTEGER NOT NULL DEFAULT 1"),
                # delay_minutes: timer mode per error_aggregations (apre ticket dopo
                # N minuti se la fingerprint non viene resettata da reset_*_regex).
                ("aggregations_cache", "delay_minutes", "ALTER TABLE aggregations_cache ADD COLUMN delay_minutes INTEGER"),
                ("error_occurrences_local", "pending_ticket_until", "ALTER TABLE error_occurrences_local ADD COLUMN pending_ticket_until TEXT"),
            ):
                try:
                    conn.execute(ddl)
                    logger.info("Migrazione: aggiunta colonna %s.%s", tbl, col)
                except sqlite3.OperationalError as exc:
                    if "duplicate column name" not in str(exc).lower():
                        raise
            # CREATE TABLE IF NOT EXISTS già nel _SCHEMA, ma per DB esistente è già stata creata
            # all'init e i nuovi schema vengono ignorati. La tabella templates_cache viene
            # creata dal _SCHEMA stesso (CREATE TABLE IF NOT EXISTS), quindi nessuna ALTER serve.
        logger.info("Database SQLite inizializzato in %s", self._path)

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE;")
                yield conn
                conn.execute("COMMIT;")
            except Exception:
                conn.execute("ROLLBACK;")
                raise
            finally:
                conn.close()

    # ------------------------------------------------------------------ events_log

    def insert_event(
        self,
        *,
        from_address: str | None,
        to_address: str | None,
        subject: str | None,
        message_id: str | None,
        codcli: str | None,
        action_taken: str | None,
        rule_id: int | None = None,
        ticket_id: str | None = None,
        payload_metadata: dict[str, Any] | None = None,
        event_uuid: str | None = None,
        body_text: str | None = None,
        body_html: str | None = None,
    ) -> str:
        evt_uuid = event_uuid or str(uuid.uuid4())
        # Cap di sicurezza per non saturare il DB con MIME enormi.
        # 32 KB plain è ampio per ogni testo umano; HTML 64 KB.
        bt = (body_text or "")[:32_000] or None
        bh = (body_html or "")[:64_000] or None
        with self.transaction() as conn:
            conn.execute(
                """INSERT INTO events_log
                       (event_uuid, received_at, from_address, to_address, subject, message_id,
                        codcli, action_taken, rule_id, ticket_id, payload_metadata,
                        body_text, body_html)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    evt_uuid,
                    _now_iso(),
                    from_address,
                    to_address,
                    subject,
                    message_id,
                    codcli,
                    action_taken,
                    rule_id,
                    ticket_id,
                    json.dumps(payload_metadata or {}, ensure_ascii=False),
                    bt,
                    bh,
                ),
            )
        return evt_uuid

    def fetch_unsent_events(self, limit: int = 100) -> list[sqlite3.Row]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM events_log
                    WHERE sent_to_manager_at IS NULL
                    ORDER BY received_at ASC
                    LIMIT ?""",
                (limit,),
            ).fetchall()
        return rows

    def mark_events_sent(self, event_uuids: Iterable[str]) -> None:
        ids = list(event_uuids)
        if not ids:
            return
        placeholders = ",".join("?" for _ in ids)
        with self.transaction() as conn:
            conn.execute(
                f"UPDATE events_log SET sent_to_manager_at = ? WHERE event_uuid IN ({placeholders})",
                (_now_iso(), *ids),
            )

    # ------------------------------------------------------------------ privacy_bypass_cache

    def replace_privacy_bypass(self, *, from_emails: list[str], to_emails: list[str],
                                from_domains: list[str], to_domains: list[str]) -> int:
        """Sostituisce atomicamente la cache privacy bypass.

        Returns:
            numero totale di entries nella cache (somma 4 liste).
        """
        synced = _now_iso()
        with self.transaction() as conn:
            conn.execute("DELETE FROM privacy_bypass_cache;")
            stmt = ("INSERT OR IGNORE INTO privacy_bypass_cache (kind, value, synced_at) "
                    "VALUES (?, ?, ?)")
            for v in from_emails:
                conn.execute(stmt, ("from_email", v.lower(), synced))
            for v in to_emails:
                conn.execute(stmt, ("to_email", v.lower(), synced))
            for v in from_domains:
                conn.execute(stmt, ("from_domain", v.lower(), synced))
            for v in to_domains:
                conn.execute(stmt, ("to_domain", v.lower(), synced))
            self._set_sync_meta(conn, "privacy_bypass", synced)
        return (len(from_emails) + len(to_emails) +
                len(from_domains) + len(to_domains))

    def is_privacy_bypassed(self, from_address: str | None,
                             to_addresses: list[str] | None) -> tuple[bool, str | None]:
        """Verifica se la mail è in privacy bypass.

        Args:
            from_address: indirizzo mittente.
            to_addresses: lista di tutti i destinatari (To, Cc, Bcc, ecc.).

        Returns:
            ``(matched, reason)``: True se almeno UNO dei due (from o uno
            qualsiasi dei to) è in lista. ``reason`` è una stringa breve
            che indica perché (audit log).
        """
        from_lower = (from_address or "").lower().strip()
        from_domain = from_lower.rpartition("@")[2] if "@" in from_lower else ""
        with self._connect() as conn:
            # Match esatto from
            if from_lower:
                row = conn.execute(
                    "SELECT 1 FROM privacy_bypass_cache WHERE kind = 'from_email' AND value = ?",
                    (from_lower,),
                ).fetchone()
                if row:
                    return True, "from_email"
            # Match dominio from
            if from_domain:
                row = conn.execute(
                    "SELECT 1 FROM privacy_bypass_cache WHERE kind = 'from_domain' AND value = ?",
                    (from_domain,),
                ).fetchone()
                if row:
                    return True, "from_domain"
            # Match esatto to (uno qualsiasi)
            for to in (to_addresses or []):
                to_l = (to or "").lower().strip()
                if not to_l:
                    continue
                row = conn.execute(
                    "SELECT 1 FROM privacy_bypass_cache WHERE kind = 'to_email' AND value = ?",
                    (to_l,),
                ).fetchone()
                if row:
                    return True, "to_email"
                # Dominio to
                to_dom = to_l.rpartition("@")[2] if "@" in to_l else ""
                if to_dom:
                    row = conn.execute(
                        "SELECT 1 FROM privacy_bypass_cache WHERE kind = 'to_domain' AND value = ?",
                        (to_dom,),
                    ).fetchone()
                    if row:
                        return True, "to_domain"
        return False, None

    # ------------------------------------------------------------------ customers_cache

    def replace_customers(self, customers: list[dict[str, Any]]) -> int:
        synced = _now_iso()
        with self.transaction() as conn:
            conn.execute("DELETE FROM customers_cache;")
            for c in customers:
                conn.execute(
                    """INSERT INTO customers_cache
                           (codcli, ragione_sociale, domains_json, aliases_json,
                            contract_active, service_hours_json, synced_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        c["codcli"],
                        c.get("ragione_sociale"),
                        json.dumps(c.get("domains", []), ensure_ascii=False),
                        json.dumps(c.get("aliases", []), ensure_ascii=False),
                        1 if c.get("contract_active") else 0,
                        json.dumps(c.get("service_hours"), ensure_ascii=False) if c.get("service_hours") else None,
                        synced,
                    ),
                )
            self._set_sync_meta(conn, "customers", synced)
        return len(customers)

    def find_customer_by_domain(self, domain: str) -> sqlite3.Row | None:
        domain_low = domain.lower()
        with self._connect() as conn:
            for row in conn.execute("SELECT * FROM customers_cache").fetchall():
                domains = json.loads(row["domains_json"] or "[]")
                if domain_low in (d.lower() for d in domains):
                    return row
        return None

    def find_customer_by_alias(self, alias: str) -> sqlite3.Row | None:
        alias_low = alias.lower()
        with self._connect() as conn:
            for row in conn.execute("SELECT * FROM customers_cache").fetchall():
                aliases = json.loads(row["aliases_json"] or "[]")
                if alias_low in (a.lower() for a in aliases):
                    return row
        return None

    # ------------------------------------------------------------------ rules_cache

    def replace_rules(self, rules: list[dict[str, Any]]) -> int:
        synced = _now_iso()
        with self.transaction() as conn:
            conn.execute("DELETE FROM rules_cache;")
            for r in rules:
                def _tristate(v: Any) -> int | None:
                    return None if v is None else (1 if v else 0)
                conn.execute(
                    """INSERT INTO rules_cache
                           (id, name, applies_to, scope_type, scope_ref, priority, enabled,
                            match_from_regex, match_from_domain,
                            match_to_regex, match_to_domain,
                            match_subject_regex, match_body_regex,
                            match_at_hours, match_in_service, match_contract_active,
                            match_known_customer, match_has_exception_today,
                            match_customer_groups, match_tag,
                            action, action_map_json, continue_after_match, synced_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        r["id"],
                        r.get("name"),
                        r.get("applies_to", "smtp"),
                        r.get("scope_type"),
                        r.get("scope_ref"),
                        int(r.get("priority", 100)),
                        1 if r.get("enabled", True) else 0,
                        r.get("match_from_regex"),
                        r.get("match_from_domain"),
                        r.get("match_to_regex"),
                        r.get("match_to_domain"),
                        r.get("match_subject_regex"),
                        r.get("match_body_regex"),
                        r.get("match_at_hours"),
                        _tristate(r.get("match_in_service")),
                        _tristate(r.get("match_contract_active")),
                        _tristate(r.get("match_known_customer")),
                        _tristate(r.get("match_has_exception_today")),
                        r.get("match_customer_groups"),
                        r.get("match_tag"),
                        r["action"],
                        json.dumps(r.get("action_map", {}), ensure_ascii=False),
                        1 if r.get("continue_after_match") else 0,
                        synced,
                    ),
                )
            self._set_sync_meta(conn, "rules", synced)
        return len(rules)

    def fetch_active_rules(self) -> list[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute(
                """SELECT * FROM rules_cache
                    WHERE enabled = 1 AND applies_to IN ('smtp','any')
                    ORDER BY priority ASC, id ASC"""
            ).fetchall()

    # ------------------------------------------------------------------ customer_groups_cache

    def replace_customer_groups(self, groups: list[dict[str, Any]],
                                 members: list[dict[str, Any]]) -> tuple[int, int]:
        """Sostituisce atomicamente cache gruppi clienti + membership.

        Args:
            groups: [{"id":1, "code":"top", "name":"Top", "enabled":True}, ...]
            members: [{"codcli":"00123", "group_codes":["top","sanita"]}, ...]
        Returns: (n_groups, n_memberships)
        """
        synced = _now_iso()
        n_members = 0
        with self.transaction() as conn:
            conn.execute("DELETE FROM customer_groups_cache;")
            conn.execute("DELETE FROM customer_group_members_cache;")
            for g in groups:
                conn.execute(
                    """INSERT INTO customer_groups_cache (id, code, name, enabled, synced_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    (int(g["id"]), g["code"], g.get("name"),
                     1 if g.get("enabled", True) else 0, synced),
                )
            for m in members:
                cc = m.get("codcli")
                if not cc:
                    continue
                for code in (m.get("group_codes") or []):
                    if not code:
                        continue
                    conn.execute(
                        "INSERT OR IGNORE INTO customer_group_members_cache (codcli, group_code) VALUES (?, ?)",
                        (cc, code),
                    )
                    n_members += 1
            self._set_sync_meta(conn, "customer_groups", synced)
        return (len(groups), n_members)

    def get_groups_for_codcli(self, codcli: str | None) -> set[str]:
        """Codes dei gruppi a cui appartiene un cliente (vuoto se codcli None)."""
        if not codcli:
            return set()
        with self._connect() as conn:
            return {
                r["group_code"] for r in conn.execute(
                    "SELECT group_code FROM customer_group_members_cache WHERE codcli = ?",
                    (codcli,),
                ).fetchall()
            }

    # ------------------------------------------------------------------ routes_cache

    def replace_routes(self, routes: list[dict[str, Any]]) -> int:
        with self.transaction() as conn:
            conn.execute("DELETE FROM routes_cache;")
            for rt in routes:
                conn.execute(
                    """INSERT INTO routes_cache
                           (local_part, domain, codcli, default_action,
                            forward_target, forward_port, forward_tls, redirect_target,
                            enabled, apply_rules, notes)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        rt["local_part"].lower(),
                        rt["domain"].lower(),
                        rt.get("codcli"),
                        rt.get("default_action"),
                        rt.get("forward_target"),
                        rt.get("forward_port"),
                        rt.get("forward_tls"),
                        rt.get("redirect_target"),
                        1 if rt.get("enabled", True) else 0,
                        1 if rt.get("apply_rules", True) else 0,
                        rt.get("notes"),
                    ),
                )
        return len(routes)

    def find_route(self, local_part: str, domain: str) -> sqlite3.Row | None:
        with self._connect() as conn:
            return conn.execute(
                "SELECT * FROM routes_cache WHERE local_part = ? AND domain = ? AND enabled = 1",
                (local_part.lower(), domain.lower()),
            ).fetchone()

    def list_routes(self) -> list[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute(
                "SELECT * FROM routes_cache WHERE enabled = 1 ORDER BY domain, local_part"
            ).fetchall()

    # ------------------------------------------------------------------ aggregations_cache + error_occurrences_local

    def replace_aggregations(self, aggs: list[dict[str, Any]]) -> int:
        synced = _now_iso()
        with self.transaction() as conn:
            conn.execute("DELETE FROM aggregations_cache;")
            # NOTA: NON cancelliamo error_occurrences_local: il counter va preservato
            # tra sync. Cleanup automatico delle occurrences orfane (aggregation_id non
            # più presente) via DELETE finale.
            for a in aggs:
                conn.execute(
                    """INSERT INTO aggregations_cache
                           (id, name, description,
                            match_from_regex, match_subject_regex, match_body_regex,
                            fingerprint_template,
                            threshold, consecutive_only, window_hours,
                            reset_subject_regex, reset_from_regex,
                            ticket_settore, ticket_urgenza, ticket_codice_cliente,
                            priority, delay_minutes, synced_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        int(a["id"]),
                        a.get("name") or f"agg_{a['id']}",
                        a.get("description"),
                        a.get("match_from_regex"),
                        a.get("match_subject_regex"),
                        a.get("match_body_regex"),
                        a.get("fingerprint_template") or "${from}|${subject_normalized}",
                        int(a.get("threshold", 2)),
                        1 if a.get("consecutive_only") else 0,
                        int(a.get("window_hours", 24)),
                        a.get("reset_subject_regex"),
                        a.get("reset_from_regex"),
                        a.get("ticket_settore"),
                        a.get("ticket_urgenza"),
                        a.get("ticket_codice_cliente"),
                        int(a.get("priority", 100)),
                        int(a["delay_minutes"]) if a.get("delay_minutes") not in (None, "") else None,
                        synced,
                    ),
                )
            # Cleanup occurrences orfane (aggregation_id non più presente)
            conn.execute(
                "DELETE FROM error_occurrences_local "
                "WHERE aggregation_id NOT IN (SELECT id FROM aggregations_cache)"
            )
            self._set_sync_meta(conn, "aggregations", synced)
        return len(aggs)

    def fetch_active_aggregations(self) -> list[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute(
                "SELECT * FROM aggregations_cache ORDER BY priority ASC, id ASC"
            ).fetchall()

    def find_occurrence(self, aggregation_id: int, fingerprint: str) -> sqlite3.Row | None:
        with self._connect() as conn:
            return conn.execute(
                "SELECT * FROM error_occurrences_local WHERE aggregation_id = ? AND fingerprint = ?",
                (aggregation_id, fingerprint),
            ).fetchone()

    def upsert_occurrence(
        self,
        *,
        aggregation_id: int,
        fingerprint: str,
        sample_from: str | None,
        sample_subject: str | None,
        sample_message_id: str | None,
        is_reset: bool,
        is_outside_window: bool,
        ticket_id: str | None = None,
        ticket_opened_at: str | None = None,
    ) -> dict[str, Any]:
        """Aggiorna lo stato di una occurrence applicando reset/window/increment.

        Ritorna dict con `current_count`, `first_time`, `was_reset`, `was_window_expired`,
        `ticket_already_opened`.
        """
        now = _now_iso()
        with self.transaction() as conn:
            existing = conn.execute(
                "SELECT * FROM error_occurrences_local WHERE aggregation_id = ? AND fingerprint = ?",
                (aggregation_id, fingerprint),
            ).fetchone()

            if existing is None:
                conn.execute(
                    """INSERT INTO error_occurrences_local
                           (aggregation_id, fingerprint, current_count,
                            first_seen, last_seen,
                            sample_from, sample_subject, sample_received_at, sample_message_id,
                            total_resets)
                       VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, 0)""",
                    (aggregation_id, fingerprint, now, now,
                     sample_from, sample_subject, now, sample_message_id),
                )
                return {
                    "current_count": 1, "first_time": True,
                    "was_reset": False, "was_window_expired": False,
                    "ticket_already_opened": False, "first_seen": now, "last_seen": now,
                    "ticket_id": None,
                }

            current = int(existing["current_count"])
            ticket_already = existing["ticket_id"] is not None

            if is_reset:
                # Counter azzerato manualmente da reset_trigger.
                # Si libera anche ticket_id/ticket_opened_at così il ciclo successivo
                # (recovery → nuovo errore) può aprire un nuovo ticket.
                conn.execute(
                    """UPDATE error_occurrences_local SET
                           current_count = 0,
                           last_seen = ?,
                           sample_from = ?, sample_subject = ?, sample_received_at = ?, sample_message_id = ?,
                           last_reset_at = ?,
                           total_resets = total_resets + 1,
                           ticket_id = NULL,
                           ticket_opened_at = NULL,
                           pending_ticket_until = NULL
                       WHERE aggregation_id = ? AND fingerprint = ?""",
                    (now, sample_from, sample_subject, now, sample_message_id, now,
                     aggregation_id, fingerprint),
                )
                return {
                    "current_count": 0, "first_time": False,
                    "was_reset": True, "was_window_expired": False,
                    "ticket_already_opened": False,
                    "first_seen": existing["first_seen"], "last_seen": now,
                    "ticket_id": None,
                }

            if is_outside_window:
                # Finestra scaduta: reset a 1 e first_seen=now (nuovo ciclo)
                conn.execute(
                    """UPDATE error_occurrences_local SET
                           current_count = 1,
                           first_seen = ?, last_seen = ?,
                           sample_from = ?, sample_subject = ?, sample_received_at = ?, sample_message_id = ?,
                           ticket_opened_at = NULL, ticket_id = NULL
                       WHERE aggregation_id = ? AND fingerprint = ?""",
                    (now, now, sample_from, sample_subject, now, sample_message_id,
                     aggregation_id, fingerprint),
                )
                return {
                    "current_count": 1, "first_time": False,
                    "was_reset": False, "was_window_expired": True,
                    "ticket_already_opened": False,
                    "first_seen": now, "last_seen": now,
                    "ticket_id": None,
                }

            # Increment normale
            new_count = current + 1
            conn.execute(
                """UPDATE error_occurrences_local SET
                       current_count = ?, last_seen = ?,
                       sample_from = ?, sample_subject = ?, sample_received_at = ?, sample_message_id = ?
                   WHERE aggregation_id = ? AND fingerprint = ?""",
                (new_count, now, sample_from, sample_subject, now, sample_message_id,
                 aggregation_id, fingerprint),
            )
            return {
                "current_count": new_count, "first_time": False,
                "was_reset": False, "was_window_expired": False,
                "ticket_already_opened": ticket_already,
                "first_seen": existing["first_seen"], "last_seen": now,
                "ticket_id": existing["ticket_id"],
            }

    def mark_occurrence_ticket(
        self,
        aggregation_id: int,
        fingerprint: str,
        ticket_id: str,
        opened_at: str | None = None,
    ) -> None:
        with self.transaction() as conn:
            conn.execute(
                """UPDATE error_occurrences_local SET
                       ticket_opened_at = ?, ticket_id = ?,
                       pending_ticket_until = NULL
                   WHERE aggregation_id = ? AND fingerprint = ?""",
                (opened_at or _now_iso(), ticket_id, aggregation_id, fingerprint),
            )

    # --- Timer mode: pending_ticket_until -----------------------------------

    def set_pending_ticket_until(
        self,
        aggregation_id: int,
        fingerprint: str,
        pending_until_iso: str,
    ) -> None:
        """Imposta `pending_ticket_until` su una occurrence (timer mode).

        Idempotente: se già settato, lo lascia (non vogliamo ri-spostare la
        scadenza ad ogni Problem ripetuto — il timer parte dalla prima
        occorrenza).
        """
        with self.transaction() as conn:
            conn.execute(
                """UPDATE error_occurrences_local SET
                       pending_ticket_until = COALESCE(pending_ticket_until, ?)
                   WHERE aggregation_id = ? AND fingerprint = ?""",
                (pending_until_iso, aggregation_id, fingerprint),
            )

    # ----------------------------------- H24 targets cache (multi-brand) --

    def replace_h24_targets(self, targets: list[dict[str, Any]]) -> int:
        """Sostituisce la cache mappatura source_domain → h24_alias.
        Chiamato dal sync periodico (Fase E).
        """
        synced = _now_iso()
        with self.transaction() as conn:
            conn.execute("DELETE FROM h24_targets_cache;")
            for t in targets:
                if not t.get("source_domain") or not t.get("h24_alias"):
                    continue
                conn.execute(
                    """INSERT INTO h24_targets_cache
                           (id, source_domain, h24_alias, urgent_fee_eur,
                            enabled, synced_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        int(t["id"]),
                        (t["source_domain"] or "").strip().lower(),
                        (t["h24_alias"] or "").strip().lower(),
                        int(t["urgent_fee_eur"]) if t.get("urgent_fee_eur") not in (None, "") else None,
                        1 if t.get("enabled", True) else 0,
                        synced,
                    ),
                )
            self._set_sync_meta(conn, "h24_targets", synced)
        return len(targets)

    def find_h24_target_by_domain(self, source_domain: str) -> dict[str, Any] | None:
        """Lookup brand-aware: dato il dominio del mittente, ritorna l'alias
        H24 da inserire nel mailto. Usato da auto_reply.build_context."""
        d = (source_domain or "").strip().lower()
        if not d:
            return None
        with self._connect() as conn:
            row = conn.execute(
                """SELECT * FROM h24_targets_cache
                     WHERE source_domain = ? AND enabled = 1""",
                (d,),
            ).fetchone()
            return dict(row) if row else None

    # ------------------------------------------------------ pending tickets --

    def find_due_pending_occurrences(self, now_iso: str | None = None) -> list[sqlite3.Row]:
        """Restituisce le occurrences in timer mode che hanno superato la scadenza
        e per cui non è stato ancora aperto un ticket.

        Usato dallo scheduler ogni 60s per aprire i ticket "differiti".
        """
        ts = now_iso or _now_iso()
        with self._connect() as conn:
            return conn.execute(
                """SELECT eo.*, ac.name AS aggregation_name,
                          ac.ticket_settore, ac.ticket_urgenza,
                          ac.ticket_codice_cliente
                     FROM error_occurrences_local eo
                     JOIN aggregations_cache ac ON ac.id = eo.aggregation_id
                    WHERE eo.pending_ticket_until IS NOT NULL
                      AND eo.pending_ticket_until <= ?
                      AND eo.ticket_opened_at IS NULL
                      AND eo.current_count > 0""",
                (ts,),
            ).fetchall()

    def reset_all_occurrences_for(self, aggregation_id: int, from_address: str | None = None) -> int:
        """Azzera il counter di tutte le occurrences attive (con ticket aperto o no) di una
        aggregation, opzionalmente filtrate per mittente. Usato quando arriva un reset_trigger:
        la mail di reset chiude tutti gli errori "in corso" di quel mittente.

        Il reset libera anche `ticket_id`/`ticket_opened_at` così il prossimo ciclo di errori
        (dopo il recovery) può aprire un nuovo ticket invece di restare bloccato da
        `ticket_already_opened=True`.
        """
        now = _now_iso()
        with self.transaction() as conn:
            if from_address:
                cur = conn.execute(
                    """UPDATE error_occurrences_local SET
                           current_count = 0,
                           last_seen = ?,
                           last_reset_at = ?,
                           total_resets = total_resets + 1,
                           ticket_id = NULL,
                           ticket_opened_at = NULL,
                           pending_ticket_until = NULL
                       WHERE aggregation_id = ? AND LOWER(sample_from) = LOWER(?)
                         AND (current_count > 0 OR ticket_id IS NOT NULL OR pending_ticket_until IS NOT NULL)""",
                    (now, now, aggregation_id, from_address),
                )
            else:
                cur = conn.execute(
                    """UPDATE error_occurrences_local SET
                           current_count = 0,
                           last_seen = ?,
                           last_reset_at = ?,
                           total_resets = total_resets + 1,
                           ticket_id = NULL,
                           ticket_opened_at = NULL,
                           pending_ticket_until = NULL
                       WHERE aggregation_id = ?
                         AND (current_count > 0 OR ticket_id IS NOT NULL OR pending_ticket_until IS NOT NULL)""",
                    (now, now, aggregation_id),
                )
            return cur.rowcount

    def list_active_occurrences(self, aggregation_id: int | None = None,
                                limit: int = 200) -> list[sqlite3.Row]:
        with self._connect() as conn:
            if aggregation_id is not None:
                return conn.execute(
                    "SELECT * FROM error_occurrences_local WHERE aggregation_id = ? "
                    "ORDER BY last_seen DESC LIMIT ?",
                    (aggregation_id, limit),
                ).fetchall()
            return conn.execute(
                "SELECT * FROM error_occurrences_local ORDER BY last_seen DESC LIMIT ?",
                (limit,),
            ).fetchall()

    # ------------------------------------------------------------------ domain_routing_cache

    def replace_domain_routing(self, domains: list[dict[str, Any]]) -> int:
        synced = _now_iso()
        with self.transaction() as conn:
            conn.execute("DELETE FROM domain_routing_cache;")
            for d in domains:
                conn.execute(
                    """INSERT INTO domain_routing_cache
                           (domain, smarthost, smarthost_port, smarthost_tls,
                            enabled, apply_rules, notes, synced_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        str(d["domain"]).lower(),
                        d["smarthost"],
                        int(d.get("smarthost_port", 25)),
                        d.get("smarthost_tls", "opportunistic"),
                        1 if d.get("enabled", True) else 0,
                        1 if d.get("apply_rules", True) else 0,
                        d.get("notes"),
                        synced,
                    ),
                )
            self._set_sync_meta(conn, "domain_routing", synced)
        return len(domains)

    def find_domain_routing(self, domain: str) -> sqlite3.Row | None:
        with self._connect() as conn:
            return conn.execute(
                "SELECT * FROM domain_routing_cache WHERE domain = ? AND enabled = 1",
                (domain.lower(),),
            ).fetchone()

    def list_accepted_domains(self) -> list[str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT domain FROM domain_routing_cache WHERE enabled = 1 ORDER BY domain"
            ).fetchall()
        return [r["domain"] for r in rows]

    def list_domain_routing(self) -> list[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute(
                "SELECT * FROM domain_routing_cache WHERE enabled = 1 ORDER BY domain"
            ).fetchall()

    # ------------------------------------------------------------------ templates_cache

    def replace_templates(self, templates: list[dict[str, Any]]) -> int:
        synced = _now_iso()
        with self.transaction() as conn:
            conn.execute("DELETE FROM templates_cache;")
            for t in templates:
                conn.execute(
                    """INSERT INTO templates_cache
                           (id, name, description, subject_tmpl,
                            body_html_tmpl, body_text_tmpl,
                            reply_from_name, reply_from_email, synced_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        int(t["id"]),
                        t.get("name") or f"template_{t['id']}",
                        t.get("description"),
                        t.get("subject_tmpl") or "",
                        t.get("body_html_tmpl"),
                        t.get("body_text_tmpl"),
                        t.get("reply_from_name"),
                        t.get("reply_from_email"),
                        synced,
                    ),
                )
            self._set_sync_meta(conn, "templates", synced)
        return len(templates)

    def find_template_by_id(self, tpl_id: int | str | None) -> sqlite3.Row | None:
        if tpl_id is None:
            return None
        try:
            tid = int(tpl_id)
        except (TypeError, ValueError):
            return None
        with self._connect() as conn:
            return conn.execute("SELECT * FROM templates_cache WHERE id = ?", (tid,)).fetchone()

    def find_template_by_name(self, name: str | None) -> sqlite3.Row | None:
        if not name:
            return None
        with self._connect() as conn:
            return conn.execute(
                "SELECT * FROM templates_cache WHERE LOWER(name) = LOWER(?)",
                (name.strip(),),
            ).fetchone()

    def list_templates(self) -> list[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute("SELECT * FROM templates_cache ORDER BY id").fetchall()

    # ------------------------------------------------------------------

    def pick_smarthost_for_domain(
        self,
        rcpt_domain: str | None,
        default_smarthost: str,
        default_smarthost_port: int = 25,
        default_tls: str = "opportunistic",
    ) -> dict[str, Any]:
        """Sceglie lo smarthost in base al dominio del destinatario.

        Se il dominio è in `domain_routing_cache`, ritorna quello smarthost.
        Altrimenti ritorna il fallback (`default_smarthost`), che dovrebbe essere il server
        SMTP interno autorizzato a relayare per tutti i domini (es. smtp.domarc.it).
        """
        if rcpt_domain:
            row = self.find_domain_routing(rcpt_domain)
            if row is not None:
                return {
                    "smarthost": row["smarthost"],
                    "smarthost_port": int(row["smarthost_port"] or 25),
                    "smarthost_tls": row["smarthost_tls"] or "opportunistic",
                    "source": "domain_routing",
                    "domain": rcpt_domain.lower(),
                }
        return {
            "smarthost": default_smarthost,
            "smarthost_port": default_smarthost_port,
            "smarthost_tls": default_tls,
            "source": "default",
            "domain": (rcpt_domain or "").lower(),
        }

    # ------------------------------------------------------------------ settings_cache

    def replace_settings(self, settings: dict[str, dict[str, Any]]) -> int:
        synced = _now_iso()
        with self.transaction() as conn:
            conn.execute("DELETE FROM settings_cache;")
            for key, payload in settings.items():
                value = payload.get("value") if isinstance(payload, dict) else payload
                description = payload.get("description") if isinstance(payload, dict) else None
                conn.execute(
                    "INSERT INTO settings_cache (key, value_json, description, synced_at) VALUES (?, ?, ?, ?)",
                    (key, json.dumps(value, ensure_ascii=False), description, synced),
                )
            self._set_sync_meta(conn, "settings", synced)
        return len(settings)

    def get_setting(self, key: str, default: Any = None) -> Any:
        with self._connect() as conn:
            row = conn.execute("SELECT value_json FROM settings_cache WHERE key = ?", (key,)).fetchone()
        if not row:
            return default
        try:
            return json.loads(row["value_json"])
        except (TypeError, ValueError):
            return default

    def is_passthrough_only(self) -> bool:
        """KILL SWITCH: se True, il listener bypassa rule engine + IA e fa solo
        default_delivery via smarthost. Da usare in caso di problemi al cutover.
        Setting `relay_passthrough_only` (string 'true'/'false', default false).
        """
        v = self.get_setting("relay_passthrough_only", default=False)
        if isinstance(v, str):
            return v.strip().lower() in ("true", "1", "yes", "on")
        return bool(v)

    def list_settings(self) -> list[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute("SELECT key, value_json, description, synced_at FROM settings_cache ORDER BY key").fetchall()

    # ------------------------------------------------------------------ quarantine

    QUARANTINE_HARD_CAP = 10000
    QUARANTINE_WARN_THRESHOLD = 5000

    def add_quarantine(
        self,
        *,
        event_uuid: str,
        mime_blob: bytes,
        reason: str,
        from_address: str | None,
        to_address: str | None,
        notes: str | None = None,
    ) -> int:
        """Inserisce mail in quarantine con soft cap (WARN > 5k) e hard cap (10k).

        Hard cap: oltre 10k righe, viene cancellato batch più vecchio (LRU 100 righe)
        per liberare spazio. Evita saturazione disco in caso di smarthost down
        prolungato. Override del cap via setting `quarantine_hard_cap`.
        """
        with self.transaction() as conn:
            count = conn.execute("SELECT COUNT(*) FROM quarantine").fetchone()[0]
            if count >= self.QUARANTINE_HARD_CAP:
                # LRU: rimuovi le 100 più vecchie per fare spazio
                conn.execute(
                    "DELETE FROM quarantine WHERE id IN "
                    "(SELECT id FROM quarantine ORDER BY created_at ASC LIMIT 100)"
                )
                logger.warning(
                    "quarantine HARD CAP raggiunto (%d) — purgate 100 righe più vecchie. "
                    "Verifica smarthost/configurazione.", count,
                )
            elif count >= self.QUARANTINE_WARN_THRESHOLD:
                logger.warning(
                    "quarantine WARN threshold (%d/%d) — pulire o investigare.",
                    count, self.QUARANTINE_HARD_CAP,
                )
            cur = conn.execute(
                """INSERT INTO quarantine
                       (event_uuid, mime_blob, reason, from_address, to_address, notes, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (event_uuid, mime_blob, reason, from_address, to_address, notes, _now_iso()),
            )
            return int(cur.lastrowid or 0)

    # ------------------------------------------------------------------ sync_meta

    def _set_sync_meta(self, conn: sqlite3.Connection, name: str, synced_at: str) -> None:
        conn.execute(
            """INSERT INTO sync_meta (name, last_sync_at) VALUES (?, ?)
               ON CONFLICT(name) DO UPDATE SET last_sync_at = excluded.last_sync_at""",
            (name, synced_at),
        )

    def get_sync_meta(self, name: str) -> sqlite3.Row | None:
        with self._connect() as conn:
            return conn.execute("SELECT * FROM sync_meta WHERE name = ?", (name,)).fetchone()

    # ------------------------------------------------------------------ utility

    def health(self) -> dict[str, Any]:
        with self._connect() as conn:
            stats: dict[str, Any] = {}
            for tbl in (
                "customers_cache",
                "rules_cache",
                "routes_cache",
                "domain_routing_cache",
                "outbound_queue",
                "dispatch_queue",
                "events_log",
                "quarantine",
            ):
                row = conn.execute(f"SELECT COUNT(*) AS n FROM {tbl}").fetchone()
                stats[tbl] = int(row["n"])
            stats["sqlite_path"] = str(self._path)
            return stats
