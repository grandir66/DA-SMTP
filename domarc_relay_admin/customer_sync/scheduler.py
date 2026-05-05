"""Scheduler thread per customer_sync_sources.

Loop ogni 60s: legge le sorgenti enabled con next_run_at NULL o <= now,
acquisisce lock atomico (ttl=1h per gestire worker gunicorn concorrenti),
esegue SyncEngine.run() in thread separato.

Il lifecycle del thread e' guidato da un threading.Event per shutdown
pulito, simile al pattern di postgres_source.start_sync_thread().

Avviato da app.py:create_app() dopo l'init dello storage. Idempotente
(start multipli sono no-op).
"""
from __future__ import annotations

import logging
import os
import threading
import time

from .engine import SyncEngine

logger = logging.getLogger(__name__)


_SCHEDULER_THREAD: threading.Thread | None = None
_SCHEDULER_STOP = threading.Event()
_LOCK = threading.Lock()


def start_sync_scheduler(storage, *, check_interval_sec: int = 60,
                         enabled: bool = True) -> None:
    """Avvia thread daemon di scheduler. Idempotente.

    `check_interval_sec`: ogni quanti secondi controllare se ci sono sorgenti
    da eseguire. Default 60s. Lo schedule effettivo per sorgente e' regolato
    da `customer_sync_sources.schedule_hours`.
    """
    global _SCHEDULER_THREAD
    if not enabled:
        logger.info("customer_sync scheduler disabilitato via config.")
        return

    with _LOCK:
        if _SCHEDULER_THREAD is not None and _SCHEDULER_THREAD.is_alive():
            return
        _SCHEDULER_STOP.clear()
        t = threading.Thread(
            target=_scheduler_loop,
            args=(storage, check_interval_sec),
            name="customer-sync-scheduler",
            daemon=True,
        )
        _SCHEDULER_THREAD = t
        t.start()
        logger.info("customer_sync scheduler avviato (check ogni %ds)",
                    check_interval_sec)


def stop_sync_scheduler() -> None:
    _SCHEDULER_STOP.set()


def _scheduler_loop(storage, interval_sec: int) -> None:
    """Loop infinito: ogni N sec scansiona sorgenti due e avvia run."""
    # Piccolo delay all'avvio per dare tempo all'app di inizializzarsi
    if _SCHEDULER_STOP.wait(timeout=10):
        return

    while not _SCHEDULER_STOP.is_set():
        try:
            _tick(storage)
        except Exception as exc:  # noqa: BLE001
            logger.exception("customer_sync scheduler tick fallito: %s", exc)
        if _SCHEDULER_STOP.wait(timeout=interval_sec):
            return


def _tick(storage) -> None:
    """Una iterazione del loop: lancia run per sorgenti due."""
    due = storage.list_due_customer_sync_sources()
    if not due:
        return
    for source in due:
        if _SCHEDULER_STOP.is_set():
            return
        # Ogni sorgente in thread separato per non bloccare le altre
        t = threading.Thread(
            target=_run_one_safely,
            args=(storage, source),
            name=f"customer-sync-{source['id']}",
            daemon=True,
        )
        t.start()


def _run_one_safely(storage, source: dict) -> None:
    try:
        engine = SyncEngine(storage)
        engine.run(source, triggered_by="schedule", dry_run=False)
    except Exception as exc:  # noqa: BLE001
        logger.exception("customer_sync run %s fallito: %s",
                         source.get("id"), exc)
