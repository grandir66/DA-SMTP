"""SyncEngine: orchestratore fetch -> map -> upsert -> on_missing -> audit.

Usage tipico:
    from domarc_relay_admin.customer_sync.engine import SyncEngine
    eng = SyncEngine(storage=storage)
    report = eng.run(source, triggered_by='manual:admin', dry_run=False)
"""
from __future__ import annotations

import logging
import socket
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from . import get_provider
from . import mapper as _mapper

logger = logging.getLogger(__name__)


class SyncEngineError(RuntimeError):
    pass


class SyncEngine:
    def __init__(self, storage) -> None:
        self.storage = storage

    def run(self, source: dict[str, Any], *, triggered_by: str,
            dry_run: bool = False, lock_ttl_sec: int = 3600
            ) -> dict[str, Any]:
        """Esegue un sync completo per una sorgente. Ritorna report dict."""
        source_id = int(source["id"])
        holder = f"pid:{os.getpid()}@{socket.gethostname()}"

        if not self.storage.acquire_sync_lock(source_id, ttl_sec=lock_ttl_sec,
                                              holder=holder):
            logger.warning("Sync %s: lock occupato, skip", source_id)
            return {"status": "skipped",
                    "error": "lock occupato (un'altra istanza sta gia' eseguendo)"}

        run_id = self.storage.insert_customer_sync_run(
            source_id=source_id, triggered_by=triggered_by, dry_run=dry_run,
        )
        started = time.monotonic()
        n_fetched = 0
        n_inserted = 0
        n_updated = 0
        n_unchanged = 0
        n_flagged_missing = 0
        n_errored = 0
        n_gmr_added_total = 0
        n_gmr_removed_total = 0
        report: dict[str, Any] = {}
        status = "running"
        error_message: str | None = None
        sample: list[dict[str, Any]] = []

        try:
            kind = source["kind"]
            config = source.get("config_json") or {}
            mapping = source.get("mapping_json") or {}
            query = source.get("query_or_path")
            on_missing = source.get("on_missing", "flag")

            provider = get_provider(kind, config=config,
                                    query_or_path=query, storage=self.storage)
            fetched_codclis: set[str] = set()

            # M034: pre-fetch group_membership_rules per evitare N+1 query.
            # Filtriamo per source_id: rules con source_id=NULL (globali) +
            # rules specifiche di questa sorgente.
            try:
                gmr_rules = self.storage.list_group_membership_rules(
                    only_enabled=True, source_id=source_id,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("M034 list_group_membership_rules failed: %s", exc)
                gmr_rules = []
            n_gmr_added_total = 0
            n_gmr_removed_total = 0

            for raw in provider.fetch():
                n_fetched += 1
                try:
                    mapped = _mapper.apply(raw, mapping)
                    codcli = (mapped.get("codcli") or "").strip().upper()
                    if not codcli:
                        n_errored += 1
                        if len(sample) < 50 and dry_run:
                            sample.append({"_error": "codcli mancante", "raw": raw})
                        continue
                    fetched_codclis.add(codcli)

                    if dry_run:
                        if len(sample) < 50:
                            sample.append({"codcli": codcli, "mapped": mapped})
                        continue

                    result = self.storage.upsert_customer_record(
                        mapped, source_id=source_id,
                    )
                    if result == "inserted":
                        n_inserted += 1
                    elif result == "updated":
                        n_updated += 1
                    else:
                        n_unchanged += 1

                    # M034: applica auto-assignments di gruppi.
                    # Eval su record = canonical (mapped) + raw del gestionale.
                    # Le rules possono guardare in entrambi.
                    if gmr_rules:
                        try:
                            eval_record = {**raw, **mapped}
                            target_groups = self.storage.evaluate_membership_rules(
                                eval_record, source_id=source_id,
                                rules_cache=gmr_rules,
                            )
                            n_add, n_rem = self.storage.apply_auto_assignments(
                                codcli, target_groups,
                            )
                            n_gmr_added_total += n_add
                            n_gmr_removed_total += n_rem
                        except Exception as exc:  # noqa: BLE001
                            logger.warning(
                                "M034 apply_auto_assignments failed for %s: %s",
                                codcli, exc,
                            )
                except Exception as exc:  # noqa: BLE001
                    n_errored += 1
                    logger.exception("Sync %s record error: %s", source_id, exc)
                    if len(sample) < 50 and dry_run:
                        sample.append({"_error": str(exc), "raw": raw})

            # Gestione codcli scomparsi
            if not dry_run and on_missing != "keep" and fetched_codclis:
                existing = set(
                    self.storage.list_customer_codclis_for_source(source_id)
                )
                missing = list(existing - fetched_codclis)
                if missing:
                    if on_missing == "flag":
                        n_flagged_missing = self.storage.flag_missing_customers(missing)
                    elif on_missing == "delete":
                        n_flagged_missing = self.storage.delete_customers(missing)

            status = "ok" if n_errored == 0 else "partial"

        except Exception as exc:  # noqa: BLE001
            logger.exception("Sync %s error: %s", source_id, exc)
            status = "error"
            error_message = str(exc)[:1000]
        finally:
            self.storage.release_sync_lock(source_id)

        duration_ms = int((time.monotonic() - started) * 1000)
        finished_at = datetime.utcnow().isoformat(timespec="seconds")

        report = {
            "status": status,
            "n_fetched": n_fetched,
            "n_inserted": n_inserted,
            "n_updated": n_updated,
            "n_unchanged": n_unchanged,
            "n_flagged_missing": n_flagged_missing,
            "n_errored": n_errored,
            # M034: contatori auto-assignment gruppi
            "n_auto_groups_added": n_gmr_added_total,
            "n_auto_groups_removed": n_gmr_removed_total,
            "duration_ms": duration_ms,
            "error_message": error_message,
            "dry_run": dry_run,
        }
        if dry_run:
            report["sample"] = sample

        self.storage.update_customer_sync_run(
            run_id,
            finished_at=finished_at,
            duration_ms=duration_ms,
            status=status,
            n_fetched=n_fetched,
            n_inserted=n_inserted,
            n_updated=n_updated,
            n_unchanged=n_unchanged,
            n_flagged_missing=n_flagged_missing,
            n_errored=n_errored,
            error_message=error_message,
            report_json=report if dry_run else None,
        )

        # Aggiorna stato sorgente + next_run_at (solo per run reali)
        if not dry_run:
            schedule_hours = int(source.get("schedule_hours") or 24)
            next_run_at = (datetime.utcnow()
                           + timedelta(hours=schedule_hours)
                           ).isoformat(timespec="seconds")
            self.storage.update_customer_sync_source_state(
                source_id,
                last_run_at=finished_at,
                last_run_status=status,
                last_run_error=error_message or "",
                next_run_at=next_run_at,
            )

        report["run_id"] = run_id
        return report
