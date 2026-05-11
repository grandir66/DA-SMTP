"""Thread daemon che esegue cleanup periodici su admin.db.

Avviato da `app.py` al boot, gira in background:
- Ogni 10 minuti: `purge_expired_bodies` (svuota body_text/html oltre TTL).
- Ogni 24h alle 03:30 UTC: retention DELETE su tabelle audit / log
  (eventi vecchi, login_audit, customers_pg_sync_log, privacy_bypass_audit).

Best-effort: ogni eccezione viene loggata ma il thread non muore.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


_RUNNING = False
_LOCK = threading.Lock()


def start_retention_thread(storage: Any, *,
                            body_purge_interval_sec: int = 600,
                            retention_check_interval_sec: int = 1800,
                            ) -> None:
    """Idempotente: chiamabile più volte, parte UNA sola istanza."""
    global _RUNNING
    with _LOCK:
        if _RUNNING:
            logger.debug("retention thread già attivo, skip")
            return
        _RUNNING = True

    def _loop():
        last_body_purge = 0.0
        last_retention = 0.0
        last_retention_date = None
        while True:
            try:
                now = time.monotonic()
                # 1) Body purge (ogni 10 min)
                if (now - last_body_purge) > body_purge_interval_sec:
                    last_body_purge = now
                    try:
                        n = storage.purge_expired_bodies()
                        if n:
                            logger.info("Retention: purge_expired_bodies → %d eventi svuotati", n)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("purge_expired_bodies failed: %s", exc)

                # 2) Retention notturna (una volta al giorno, 03:30 UTC)
                if (now - last_retention) > retention_check_interval_sec:
                    last_retention = now
                    utc_now = datetime.now(timezone.utc)
                    today = utc_now.date()
                    if utc_now.hour == 3 and (last_retention_date != today):
                        last_retention_date = today
                        _run_nightly_retention(storage)
            except Exception as exc:  # noqa: BLE001
                logger.exception("retention loop error: %s", exc)
            time.sleep(60)

    t = threading.Thread(target=_loop, name="domarc-retention", daemon=True)
    t.start()
    logger.info("Retention thread avviato (body_purge=%ds, check=%ds)",
                 body_purge_interval_sec, retention_check_interval_sec)


def _run_nightly_retention(storage: Any) -> None:
    """DELETE di righe vecchie da tabelle audit/log. Best-effort, partial OK."""
    # Configurazione retention (giorni). Se la tabella non esiste, lo storage
    # solleva e prendiamo l'eccezione → skip.
    targets = [
        ("events", "received_at", 180, "events flushati al manager"),
        ("login_audit", "ts", 365, "audit login"),
        ("privacy_bypass_audit", "ts", 365, "audit privacy bypass"),
        ("customers_pg_sync_log", "ts", 90, "log sync customers PG"),
        ("ai_decisions", "ts", 180, "AI decisions (se attive)"),
        ("auth_codes", "valid_until", 30, "codici H24 monouso scaduti"),
    ]
    for tbl, col, days, desc in targets:
        try:
            with storage.transaction() as conn:
                # Verifica esistenza tabella + colonna (best-effort)
                try:
                    cur = conn.execute(
                        f"DELETE FROM {tbl} WHERE {col} < datetime('now', '-{int(days)} days')"
                    )
                    deleted = cur.rowcount
                    if deleted:
                        logger.info("Retention notturna %s: %d righe eliminate (%s)",
                                     tbl, deleted, desc)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("Retention skip %s: %s", tbl, exc)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Retention %s lock fallito: %s", tbl, exc)

    # Incremental VACUUM (libera spazio dopo DELETE)
    try:
        with storage._connect() as conn:
            conn.execute("PRAGMA incremental_vacuum(1000)")
    except Exception as exc:  # noqa: BLE001
        logger.debug("incremental_vacuum non supportato/fallito: %s", exc)
