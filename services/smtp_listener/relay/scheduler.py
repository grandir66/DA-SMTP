"""Scheduler asincrono del relay con 5 loop indipendenti.

- sync_loop:           sync clienti + regole verso il manager
- routes_reload_loop:  ricarica routes da YAML (Fase 4: spostato in DB manager)
- events_flush_loop:   batch POST eventi audit verso il manager
- outbound_drain_loop: drena outbound_queue (forward/redirect) via SmtpForwarder
- dispatch_drain_loop: drena dispatch_queue (create_ticket) via Manager API

Fault tolerance: se il manager è down, i loop sync/flush/dispatch falliscono in modo
gracefull (warning, retry al prossimo tick). Il listener continua a ricevere e accodare.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

from relay.config import RelayConfig
from relay.forwarder import SmtpForwarder
from relay.manager_client import ManagerBackend, ManagerError
from relay.storage import Storage
from relay.sync import flush_events_to_manager, load_routes_from_yaml, sync_customers_and_rules

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _backoff_for(attempts: int, schedule: list[int]) -> int:
    if not schedule:
        return 60
    idx = min(attempts, len(schedule) - 1)
    return int(schedule[idx])


async def _sync_loop(cfg: RelayConfig, backend: ManagerBackend, storage: Storage, stop: asyncio.Event) -> None:
    interval = cfg.manager.sync_interval_sec
    while not stop.is_set():
        try:
            sync_customers_and_rules(backend, storage)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Sync loop: %s", exc)
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            continue


async def _events_flush_loop(cfg: RelayConfig, backend: ManagerBackend, storage: Storage, stop: asyncio.Event) -> None:
    interval = cfg.scheduler.events_flush_interval_sec
    while not stop.is_set():
        try:
            flush_events_to_manager(backend, storage)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Events flush loop: %s", exc)
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            continue


async def _routes_reload_loop(cfg: RelayConfig, storage: Storage, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            load_routes_from_yaml(cfg, storage)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Routes reload: %s", exc)
        try:
            await asyncio.wait_for(stop.wait(), timeout=60)
        except asyncio.TimeoutError:
            continue


def _drain_outbound_once(cfg: RelayConfig, storage: Storage, forwarder: SmtpForwarder, batch: int = 20) -> int:
    now = _now_iso()
    with storage._connect() as conn:  # type: ignore[attr-defined]
        rows = conn.execute(
            """SELECT * FROM outbound_queue
                WHERE state IN ('pending','error') AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                ORDER BY id ASC LIMIT ?""",
            (now, batch),
        ).fetchall()

    processed = 0
    for row in rows:
        rcpt = json.loads(row["rcpt_to_json"] or "[]")
        result = forwarder.relay(
            mime_bytes=row["mime_blob"],
            mail_from=row["mail_from"] or "",
            rcpt_to=rcpt,
            smarthost=row["smarthost"],
            smarthost_port=int(row["smarthost_port"] or 25),
            tls_mode=row["smarthost_tls"] or cfg.outbound.default_tls,
            extra_headers=[("X-Domarc-Original-Rcpt", json.dumps(rcpt))],
        )
        attempts = int(row["attempts"]) + 1

        with storage.transaction() as conn:
            conn.execute(
                """INSERT INTO forward_log
                       (queue_id, event_uuid, attempted_at, smarthost, smtp_response, ok, duration_ms)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    row["id"],
                    row["event_uuid"],
                    _now_iso(),
                    result.smarthost,
                    f"{result.smtp_code} {result.smtp_message}".strip(),
                    1 if result.ok else 0,
                    result.duration_ms,
                ),
            )

            if result.ok:
                conn.execute(
                    "UPDATE outbound_queue SET state='sent', delivered_at=?, attempts=?, last_error=NULL WHERE id=?",
                    (_now_iso(), attempts, row["id"]),
                )
            else:
                if attempts >= cfg.outbound.max_attempts:
                    conn.execute(
                        "UPDATE outbound_queue SET state='dead', attempts=?, last_error=? WHERE id=?",
                        (attempts, result.error or "max attempts", row["id"]),
                    )
                    conn.execute(
                        """INSERT INTO quarantine
                               (event_uuid, mime_blob, reason, from_address, to_address, decision, created_at)
                           VALUES (?, ?, 'forward_dead_letter', ?, ?, 'pending', ?)""",
                        (
                            row["event_uuid"],
                            row["mime_blob"],
                            row["mail_from"],
                            ",".join(rcpt),
                            _now_iso(),
                        ),
                    )
                    logger.warning(
                        "Outbound DEAD: id=%s smarthost=%s rcpt=%s err=%s (dead-letter quarantena)",
                        row["id"], row["smarthost"], rcpt, result.error,
                    )
                else:
                    backoff = _backoff_for(attempts - 1, cfg.outbound.backoff_seconds)
                    next_at = (datetime.now(timezone.utc) + timedelta(seconds=backoff)).isoformat(timespec="seconds")
                    conn.execute(
                        "UPDATE outbound_queue SET state='error', attempts=?, next_attempt_at=?, last_error=? WHERE id=?",
                        (attempts, next_at, result.error or "?", row["id"]),
                    )
                    logger.info("Outbound retry %d/%d in %ds id=%s err=%s",
                                attempts, cfg.outbound.max_attempts, backoff, row["id"], result.error)
        processed += 1
    return processed


async def _outbound_drain_loop(cfg: RelayConfig, storage: Storage, stop: asyncio.Event) -> None:
    forwarder = SmtpForwarder(helo_hostname=cfg.outbound.helo_hostname, timeout_sec=cfg.outbound.timeout_sec)
    interval = cfg.scheduler.outbound_drain_interval_sec
    while not stop.is_set():
        try:
            n = _drain_outbound_once(cfg, storage, forwarder)
            if n:
                logger.debug("Outbound drain: %d messaggi processati", n)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Outbound drain loop: %s", exc)
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            continue


def _drain_dispatch_once(cfg: RelayConfig, backend: ManagerBackend, storage: Storage, batch: int = 10) -> int:
    now = _now_iso()
    with storage._connect() as conn:  # type: ignore[attr-defined]
        rows = conn.execute(
            """SELECT * FROM dispatch_queue
                WHERE state IN ('pending','error') AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                ORDER BY id ASC LIMIT ?""",
            (now, batch),
        ).fetchall()

    processed = 0
    for row in rows:
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, ValueError):
            payload = {}
        result = backend.submit_ticket(payload)
        attempts = int(row["attempts"]) + 1

        with storage.transaction() as conn:
            if result.ok:
                conn.execute(
                    "UPDATE dispatch_queue SET state='sent', attempts=?, manager_response=?, last_error=NULL WHERE id=?",
                    (attempts, result.response_body[:500], row["id"]),
                )
                if result.ticket_id:
                    conn.execute(
                        "UPDATE events_log SET ticket_id = ? WHERE event_uuid = ?",
                        (result.ticket_id, row["event_uuid"]),
                    )
                    # Per ticket aggregation (timer mode o threshold-based) aggiorna
                    # anche error_occurrences_local sostituendo il placeholder
                    # `pending-dispatch-{qid}` con il vero ticket_id ritornato dal manager.
                    meta = payload.get("metadata") if isinstance(payload, dict) else None
                    if isinstance(meta, dict) and meta.get("aggregation_id") and meta.get("fingerprint"):
                        conn.execute(
                            "UPDATE error_occurrences_local SET ticket_id = ? "
                            "WHERE aggregation_id = ? AND fingerprint = ? AND ticket_id LIKE 'pending-dispatch-%'",
                            (result.ticket_id, int(meta["aggregation_id"]), meta["fingerprint"]),
                        )
                logger.info("Ticket creato OK: queue_id=%s ticket_id=%s", row["id"], result.ticket_id)
            else:
                if attempts >= cfg.outbound.max_attempts:
                    # CRITICAL log: dispatch dead = ticket non creato dopo max_attempts.
                    logger.critical(
                        "DISPATCH DEAD-LETTER: queue_id=%s event_uuid=%s err=%s "
                        "→ il ticket NON è stato creato. Indagine richiesta.",
                        row["id"], row["event_uuid"], result.error,
                    )
                    # Marca payload_metadata in events_log + REWIND sent_to_manager_at = NULL
                    # per forzare un re-flush dell'evento all'admin (così la dashboard
                    # vede `dispatch_dead=true`). Senza il rewind, il flag rimane confinato
                    # nel DB locale e l'admin non lo vedrà mai.
                    try:
                        existing = conn.execute(
                            "SELECT payload_metadata FROM events_log WHERE event_uuid = ?",
                            (row["event_uuid"],),
                        ).fetchone()
                        if existing:
                            try:
                                pm = json.loads(existing["payload_metadata"] or "{}")
                            except (TypeError, ValueError):
                                pm = {}
                            pm["dispatch_dead"] = True
                            pm["dispatch_dead_at"] = _now_iso()
                            pm["dispatch_dead_error"] = (result.error or "max attempts")[:500]
                            conn.execute(
                                "UPDATE events_log SET payload_metadata = ?, "
                                "sent_to_manager_at = NULL WHERE event_uuid = ?",
                                (json.dumps(pm, ensure_ascii=False), row["event_uuid"]),
                            )
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("Marker dispatch_dead in events_log fallito: %s", exc)
                    conn.execute(
                        "UPDATE dispatch_queue SET state='dead', attempts=?, last_error=? WHERE id=?",
                        (attempts, result.error or "max attempts", row["id"]),
                    )
                else:
                    backoff = _backoff_for(attempts - 1, cfg.outbound.backoff_seconds)
                    next_at = (datetime.now(timezone.utc) + timedelta(seconds=backoff)).isoformat(timespec="seconds")
                    conn.execute(
                        "UPDATE dispatch_queue SET state='error', attempts=?, next_attempt_at=?, last_error=? WHERE id=?",
                        (attempts, next_at, result.error or "?", row["id"]),
                    )
        processed += 1
    return processed


async def _dispatch_drain_loop(cfg: RelayConfig, backend: ManagerBackend, storage: Storage, stop: asyncio.Event) -> None:
    interval = cfg.scheduler.dispatch_drain_interval_sec
    while not stop.is_set():
        try:
            n = _drain_dispatch_once(cfg, backend, storage)
            if n:
                logger.debug("Dispatch drain: %d ticket processati", n)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Dispatch drain loop: %s", exc)
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            continue


def _process_pending_tickets_once(storage: Storage) -> int:
    """Apre i ticket per le occurrences in timer mode con `pending_ticket_until`
    scaduto (delay_minutes elapsed senza Reset).

    Per ognuna: enqueue su dispatch_queue + marca ticket_opened_at su occurrence
    + svuota pending_ticket_until.
    """
    import uuid as _uuid
    from relay import actions  # import locale per evitare circular
    rows = storage.find_due_pending_occurrences()
    if not rows:
        return 0
    processed = 0
    for r in rows:
        try:
            # Event UUID sintetico per dispatch_queue (timer ticket non hanno
            # un singolo evento di origine — sono il prodotto di N Problem
            # accumulati senza Reset).
            synthetic_uuid = str(_uuid.uuid4())
            ticket_payload = {
                "channel": "smtp_aggregated_error",
                "external_id": f"agg-{r['aggregation_id']}-{r['fingerprint'][:32]}",
                "subject": f"[{r['aggregation_name']}] {r['sample_subject'] or '(no subject)'}",
                "body": (
                    f"Errore non risolto entro la finestra timer.\n\n"
                    f"Aggregation: {r['aggregation_name']}\n"
                    f"Fingerprint: {r['fingerprint']}\n"
                    f"Occorrenze accumulate: {r['current_count']}\n"
                    f"Mittente: {r['sample_from']}\n"
                    f"Oggetto: {r['sample_subject']}\n"
                    f"Prima occorrenza: {r['first_seen']}\n"
                    f"Ultima occorrenza: {r['last_seen']}\n"
                    f"Scadenza timer: {r['pending_ticket_until']}\n"
                ),
                "from_address": r["sample_from"],
                "to_address": None,
                "message_id": None,
                "codice_cliente": r["ticket_codice_cliente"],
                "settore": r["ticket_settore"],
                "urgenza": r["ticket_urgenza"],
                "metadata": {
                    "aggregation_id": int(r["aggregation_id"]),
                    "aggregation_name": r["aggregation_name"],
                    "fingerprint": r["fingerprint"],
                    "occurrences_count": int(r["current_count"]),
                    "timer_mode": True,
                    "pending_until": r["pending_ticket_until"],
                },
            }
            qid = actions._enqueue_dispatch(
                storage,
                event_uuid=synthetic_uuid,
                payload=ticket_payload,
            )
            storage.mark_occurrence_ticket(
                aggregation_id=int(r["aggregation_id"]),
                fingerprint=r["fingerprint"],
                ticket_id=f"pending-dispatch-{qid}",
            )
            logger.info(
                "Timer aggregation '%s' SCADUTO — ticket enqueued (qid=%d) "
                "fingerprint=%s count=%d",
                r["aggregation_name"], qid, r["fingerprint"], int(r["current_count"]),
            )
            processed += 1
        except Exception as exc:  # noqa: BLE001
            logger.exception("Pending ticket processing failed (agg=%s fp=%s): %s",
                              r["aggregation_id"], r["fingerprint"], exc)
    return processed


async def _pending_tickets_loop(storage: Storage, stop: asyncio.Event) -> None:
    """Ogni 60s scansiona occurrences con pending_ticket_until scaduto e apre
    i ticket. Indipendente dal sync verso manager.
    """
    interval = 60
    while not stop.is_set():
        try:
            n = _process_pending_tickets_once(storage)
            if n:
                logger.info("Pending tickets timer: %d ticket aperti", n)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Pending tickets loop: %s", exc)
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            continue


async def _h24_maintenance_loop(cfg: RelayConfig, stop: asyncio.Event) -> None:
    """H24 Fase E — cleanup nightly codici monouso scaduti.

    Ogni 24h (sleep 86400s) chiama POST /api/v1/relay/maintenance/cleanup-oneshot-codes
    con retention_days=7. Skippa se admin non raggiungibile (best-effort).
    """
    import httpx
    interval = 86400  # 1 giorno
    base_url = cfg.manager.base_url.rstrip("/")
    api_key = cfg.manager.api_key
    while not stop.is_set():
        # Aspetta 60s al boot per non sovrapporsi al primo sync
        try:
            await asyncio.wait_for(stop.wait(), timeout=60)
            break  # stop arrivato
        except asyncio.TimeoutError:
            pass
        try:
            with httpx.Client(timeout=30.0, verify=cfg.manager.verify_tls) as cli:
                resp = cli.post(
                    f"{base_url}/api/v1/relay/maintenance/cleanup-oneshot-codes",
                    json={"retention_days": 7},
                    headers={"X-API-Key": api_key},
                )
            if resp.status_code == 200:
                deleted = resp.json().get("deleted", 0)
                if deleted:
                    logger.info("H24 maintenance: %d codici monouso scaduti eliminati", deleted)
            else:
                logger.warning("H24 maintenance HTTP %d: %s", resp.status_code, resp.text[:200])
        except Exception as exc:  # noqa: BLE001
            logger.debug("H24 maintenance skip (admin non raggiungibile): %s", exc)
        # Aspetta 24h o stop
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            continue


async def _h24_usage_flush_loop(cfg: RelayConfig, storage: Storage, stop: asyncio.Event) -> None:
    """H24 Fase E (stub) — rendicontazione utilizzi codici permanenti al manager.

    Predisposto per il futuro: ogni 5 min chiama un endpoint manager (TBD)
    che riceve il batch di customer_h24_codes_usage non ancora reportati,
    poi chiama mark_h24_usages_reported.

    OGGI: l'endpoint manager per la rendicontazione H24 NON ESISTE ANCORA.
    Il loop logga DEBUG e attende. Al momento dell'introduzione dell'endpoint
    si abilita il chiamante effettivo (TODO).
    """
    interval = 300  # 5 min
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
        # TODO: quando manager espone POST /api/v1/relay/h24-events:
        #   1. lista non rendicontati via API admin (list_unreported_h24_usages)
        #   2. POST batch al manager
        #   3. su 200, chiama POST /api/v1/relay/auth-codes/usage/<id>/reported
        #      (endpoint da aggiungere in admin per chiamare mark_h24_usages_reported)
        # Per ora, log silenzioso che il loop è vivo:
        logger.debug("H24 usage flush loop tick (rendicontazione manager non ancora attiva)")


async def run_scheduler(cfg: RelayConfig, backend: ManagerBackend, storage: Storage) -> None:
    stop = asyncio.Event()
    tasks = [
        asyncio.create_task(_sync_loop(cfg, backend, storage, stop), name="sync_loop"),
        asyncio.create_task(_events_flush_loop(cfg, backend, storage, stop), name="events_flush"),
        asyncio.create_task(_routes_reload_loop(cfg, storage, stop), name="routes_reload"),
        asyncio.create_task(_outbound_drain_loop(cfg, storage, stop), name="outbound_drain"),
        asyncio.create_task(_dispatch_drain_loop(cfg, backend, storage, stop), name="dispatch_drain"),
        asyncio.create_task(_pending_tickets_loop(storage, stop), name="pending_tickets"),
        asyncio.create_task(_h24_maintenance_loop(cfg, stop), name="h24_maintenance"),
        asyncio.create_task(_h24_usage_flush_loop(cfg, storage, stop), name="h24_usage_flush"),
    ]

    logger.info("Scheduler avviato con %d loop attivi", len(tasks))
    try:
        await asyncio.gather(*tasks)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        stop.set()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        logger.info("Scheduler fermato")
