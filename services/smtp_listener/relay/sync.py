"""Sync periodico anagrafica + regole dal manager.

Funziona in due modalità:
- atomic_replace: scarica via API e sostituisce atomicamente la cache locale
- bootstrap_routes: carica routes da file YAML (Fase 1-3); dalla Fase 4 lo stesso meccanismo
  potrà essere alimentato dall'API manager senza modificare il chiamante.

Il manager irraggiungibile NON è un errore fatale: la cache resta valida con grace TTL definito
in `manager.cache_grace_ttl_sec`. Il listener continua a operare con i dati cached.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from relay.config import RelayConfig
from relay.manager_client import ManagerBackend, ManagerError
from relay.storage import Storage

logger = logging.getLogger(__name__)


def _check_cache_grace(storage: Storage, cfg: RelayConfig | None) -> None:
    """Verifica se la cache e' stale oltre cache_grace_ttl_sec.

    Se tutti i sync recenti hanno fallito da piu' di N secondi, logga
    CRITICAL e popola un flag in sync_meta consumabile da /health esterno.
    Best-effort, non blocca il listener.
    """
    try:
        if cfg is None:
            return
        ttl = int(getattr(cfg.manager, "cache_grace_ttl_sec", 1800) or 1800)
        last_ok = storage.get_heartbeat("sync_last_ok")
        if not last_ok:
            return
        last_dt = datetime.fromisoformat(last_ok.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        elapsed = (now - last_dt).total_seconds()
        if elapsed > ttl:
            storage.set_heartbeat("cache_stale", "true")
            logger.critical(
                "CACHE STALE: ultimo sync OK %d secondi fa (limite %ds). "
                "Le regole/clienti potrebbero essere disallineate. Verificare admin.",
                int(elapsed), ttl,
            )
        else:
            storage.set_heartbeat("cache_stale", "false")
    except Exception as exc:  # noqa: BLE001
        logger.debug("cache grace check skip: %s", exc)


def sync_customers_and_rules(backend: ManagerBackend, storage: Storage,
                              cfg: RelayConfig | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"customers": None, "rules": None, "routes": None, "settings": None, "domain_routing": None, "templates": None, "errors": []}
    try:
        customers_payload = backend.fetch_active_customers()
        n = storage.replace_customers(customers_payload.customers)
        result["customers"] = {"synced_at": customers_payload.synced_at, "count": n}
        logger.info("Sync clienti OK: %d clienti aggiornati", n)
    except ManagerError as exc:
        logger.warning("Sync clienti fallito: %s", exc)
        result["errors"].append(f"customers: {exc}")

    # M029: rule_sets PRIMA delle rules cosi' la cache e' coerente quando
    # il rule engine valuta (rule.rule_set_id deve essere risolvibile).
    try:
        if hasattr(backend, "fetch_active_rule_sets"):
            rs_payload = backend.fetch_active_rule_sets()
            n = storage.replace_rule_sets(rs_payload.rule_sets)
            result["rule_sets"] = {"synced_at": rs_payload.synced_at, "count": n}
            if n:
                logger.info("Sync rule_sets OK: %d set aggiornati", n)
    except (ManagerError, AttributeError) as exc:
        logger.debug("Sync rule_sets skip/fallito: %s", exc)
        result["errors"].append(f"rule_sets: {exc}")

    try:
        rules_payload = backend.fetch_active_rules()
        n = storage.replace_rules(rules_payload.rules)
        result["rules"] = {"synced_at": rules_payload.synced_at, "count": n}
        logger.info("Sync regole OK: %d regole aggiornate", n)
    except ManagerError as exc:
        logger.warning("Sync regole fallito: %s", exc)
        result["errors"].append(f"rules: {exc}")

    try:
        routes_payload = backend.fetch_active_routes()
        n = storage.replace_routes(routes_payload.routes)
        result["routes"] = {"synced_at": routes_payload.synced_at, "count": n}
        logger.info("Sync routes OK: %d routes aggiornati", n)
    except ManagerError as exc:
        logger.warning("Sync routes fallito: %s", exc)
        result["errors"].append(f"routes: {exc}")

    try:
        settings_payload = backend.fetch_active_settings()
        n = storage.replace_settings(settings_payload.settings)
        result["settings"] = {"synced_at": settings_payload.synced_at, "count": n}
        logger.info("Sync settings OK: %d settings aggiornati", n)
    except ManagerError as exc:
        logger.warning("Sync settings fallito: %s", exc)
        result["errors"].append(f"settings: {exc}")

    try:
        dr_payload = backend.fetch_active_domain_routing()
        n = storage.replace_domain_routing(dr_payload.domains)
        result["domain_routing"] = {"synced_at": dr_payload.synced_at, "count": n}
        logger.info("Sync domain routing OK: %d domini aggiornati", n)
    except ManagerError as exc:
        logger.warning("Sync domain routing fallito: %s", exc)
        result["errors"].append(f"domain_routing: {exc}")

    try:
        cg_payload = backend.fetch_active_customer_groups()
        n_g, n_m = storage.replace_customer_groups(cg_payload.groups, cg_payload.members)
        result["customer_groups"] = {"synced_at": cg_payload.synced_at,
                                       "groups": n_g, "memberships": n_m}
        logger.info("Sync customer groups OK: %d gruppi / %d membership", n_g, n_m)
    except ManagerError as exc:
        logger.warning("Sync customer groups fallito: %s", exc)
        result["errors"].append(f"customer_groups: {exc}")

    try:
        tpl_payload = backend.fetch_active_templates()
        n = storage.replace_templates(tpl_payload.templates)
        result["templates"] = {"synced_at": tpl_payload.synced_at, "count": n}
        logger.info("Sync templates OK: %d template aggiornati", n)
    except ManagerError as exc:
        logger.warning("Sync templates fallito: %s", exc)
        result["errors"].append(f"templates: {exc}")

    try:
        agg_payload = backend.fetch_active_aggregations()
        n = storage.replace_aggregations(agg_payload.aggregations)
        result["aggregations"] = {"synced_at": agg_payload.synced_at, "count": n}
        logger.info("Sync aggregations OK: %d aggregazioni aggiornate", n)
    except ManagerError as exc:
        logger.warning("Sync aggregations fallito: %s", exc)
        result["errors"].append(f"aggregations: {exc}")

    try:
        pb_payload = backend.fetch_active_privacy_bypass()
        n = storage.replace_privacy_bypass(
            from_emails=pb_payload.from_emails,
            to_emails=pb_payload.to_emails,
            from_domains=pb_payload.from_domains,
            to_domains=pb_payload.to_domains,
        )
        result["privacy_bypass"] = {"synced_at": pb_payload.synced_at, "count": n}
        if n:
            logger.info("Sync privacy bypass OK: %d entries (from_email=%d, to_email=%d, from_dom=%d, to_dom=%d)",
                        n, len(pb_payload.from_emails), len(pb_payload.to_emails),
                        len(pb_payload.from_domains), len(pb_payload.to_domains))
    except ManagerError as exc:
        logger.warning("Sync privacy bypass fallito: %s", exc)
        result["errors"].append(f"privacy_bypass: {exc}")

    # H24 Fase E — sync mappatura source_domain → h24_alias (multi-brand)
    try:
        h24t_payload = backend.fetch_active_h24_targets()
        n = storage.replace_h24_targets(h24t_payload.targets)
        result["h24_targets"] = {"synced_at": h24t_payload.synced_at, "count": n}
        if n:
            logger.info("Sync H24 targets OK: %d mappature", n)
    except (ManagerError, AttributeError) as exc:
        # AttributeError: backend pre-Fase-E senza fetch_active_h24_targets
        logger.debug("Sync H24 targets skip/fallito: %s", exc)
        result["errors"].append(f"h24_targets: {exc}")

    # Recipient groups (Migration 027) — sync gruppi destinatari + membri
    try:
        rg_payload = backend.fetch_active_recipient_groups()
        n_groups, n_members = storage.replace_recipient_groups(rg_payload.groups)
        result["recipient_groups"] = {
            "synced_at": rg_payload.synced_at,
            "groups": n_groups, "members": n_members,
        }
        if n_groups:
            logger.info("Sync recipient_groups OK: %d gruppi, %d membri",
                        n_groups, n_members)
    except (ManagerError, AttributeError) as exc:
        logger.debug("Sync recipient_groups skip/fallito: %s", exc)
        result["errors"].append(f"recipient_groups: {exc}")

    # M038: Domain resolve strategy
    try:
        domain_strategies = backend.fetch_active_domain_strategies()
        n = storage.replace_domain_strategies(domain_strategies)
        result["domain_strategies"] = {"count": n}
        if n:
            logger.info("Sync domain_strategies OK: %d domini con strategy", n)
    except (ManagerError, AttributeError) as exc:
        logger.debug("Sync domain_strategies skip/fallito: %s", exc)
        result["errors"].append(f"domain_strategies: {exc}")

    # Aggiorna heartbeat "sync_last_ok" se almeno una sezione e' andata bene
    # senza errors gravi (customers + rules sono i critici). Cache grace check.
    try:
        ok_customers = result.get("customers") is not None
        ok_rules = result.get("rules") is not None
        if ok_customers and ok_rules:
            storage.set_heartbeat("sync_last_ok", _now_iso())
        _check_cache_grace(storage, cfg)
    except Exception as exc:  # noqa: BLE001
        logger.debug("heartbeat/grace update skip: %s", exc)

    return result


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_routes_from_yaml(cfg: RelayConfig, storage: Storage) -> int:
    """Carica routes dai file YAML configurati (fallback bootstrap).

    Le routes API del manager sono prioritarie. Se la cache routes è già popolata
    dall'ultimo sync API, questa funzione NON fa nulla per evitare di sovrascriverla.
    Se invece la cache è vuota e ci sono `routes_files` configurati, carica dai YAML
    come bootstrap iniziale.
    """
    if not cfg.routes_files:
        return 0
    if storage.list_routes():
        return 0

    routes: list[dict[str, Any]] = []
    for relpath in cfg.routes_files:
        path = Path(relpath)
        if not path.is_absolute():
            path = (Path.cwd() / relpath).resolve()
        if not path.exists():
            logger.warning("File routes %s non trovato, skip", path)
            continue
        with path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        for rt in data.get("routes", []):
            alias = rt.get("alias", "")
            if "@" in alias:
                local, _, domain = alias.partition("@")
                rt_norm = dict(rt)
                rt_norm["local_part"] = local
                rt_norm["domain"] = domain
                routes.append(rt_norm)

    if routes:
        n = storage.replace_routes(routes)
        logger.info("Bootstrap: caricati %d routes da YAML (cache era vuota)", n)
        return n
    return 0


def flush_events_to_manager(backend: ManagerBackend, storage: Storage, batch_size: int = 100) -> dict[str, Any]:
    rows = storage.fetch_unsent_events(limit=batch_size)
    if not rows:
        return {"flushed": 0}
    events_payload: list[dict[str, Any]] = []
    uuids: list[str] = []
    for r in rows:
        # sqlite3.Row supporta __getitem__ ma non .get(); proteggiamo con try
        try:
            bt = r["body_text"]
        except (IndexError, KeyError):
            bt = None
        try:
            bh = r["body_html"]
        except (IndexError, KeyError):
            bh = None
        # M036: thread tracking - safe access (campi opzionali)
        try: in_reply_to = r["in_reply_to"]
        except (IndexError, KeyError): in_reply_to = None
        try: references_json = r["references_json"]
        except (IndexError, KeyError): references_json = None
        try: reply_to_event_uuid = r["reply_to_event_uuid"]
        except (IndexError, KeyError): reply_to_event_uuid = None
        try: thread_root_uuid = r["thread_root_uuid"]
        except (IndexError, KeyError): thread_root_uuid = None
        events_payload.append(
            {
                "relay_event_uuid": r["event_uuid"],
                "received_at": r["received_at"],
                "from_address": r["from_address"],
                "to_address": r["to_address"],
                "subject": r["subject"],
                "message_id": r["message_id"],
                "codice_cliente": r["codcli"],
                "action_taken": r["action_taken"],
                "rule_id": r["rule_id"],
                "ticket_id": r["ticket_id"],
                "payload_metadata": r["payload_metadata"],
                "body_text": bt,
                "body_html": bh,
                # M036: thread tracking
                "in_reply_to": in_reply_to,
                "references_json": references_json,
                "reply_to_event_uuid": reply_to_event_uuid,
                "thread_root_uuid": thread_root_uuid,
            }
        )
        uuids.append(r["event_uuid"])
    try:
        resp = backend.submit_events(events_payload)
        storage.mark_events_sent(uuids)
        logger.info("Flush events OK: %d inviati, risposta=%s", len(uuids), resp)
        return {"flushed": len(uuids), "response": resp}
    except ManagerError as exc:
        logger.warning("Flush events fallito (riprovo al prossimo ciclo): %s", exc)
        return {"flushed": 0, "error": str(exc)}
