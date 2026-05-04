"""CLI del relay.

Sottocomandi:
- listener:  avvia il server SMTP (foreground, gestito da systemd in produzione)
- scheduler: avvia i loop di sync/flush (foreground, secondo processo systemd)
- sync:      esegue una sync una-tantum verso il manager (utile per debug)
- status:    stampa health + statistiche cache locale (JSON)
- routes:    elenca le routes attualmente in cache
- version:   stampa la versione del pacchetto
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

from relay import __version__
from relay.config import RelayConfig, load_config
from relay.listener import run_listener
from relay.manager_client import build_backend
from relay.scheduler import run_scheduler
from relay.storage import Storage
from relay.sync import flush_events_to_manager, load_routes_from_yaml, sync_customers_and_rules


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )


def _resolve_config_path(arg: str | None) -> Path:
    if arg:
        return Path(arg).expanduser().resolve()
    env = os.environ.get("RELAY_CONFIG")
    if env:
        return Path(env).expanduser().resolve()
    candidates = [
        Path.cwd() / "conf" / "relay.yaml",
        Path("/etc/stormshield-smtp-relay/relay.yaml"),
    ]
    for c in candidates:
        if c.exists():
            return c
    raise SystemExit(
        "Nessun file di configurazione trovato. Specifica --config <path>, oppure imposta RELAY_CONFIG, "
        "oppure crea ./conf/relay.yaml o /etc/stormshield-smtp-relay/relay.yaml"
    )


def _load(args: argparse.Namespace) -> tuple[RelayConfig, Storage]:
    cfg_path = _resolve_config_path(args.config)
    cfg = load_config(cfg_path)
    storage = Storage(cfg.storage.sqlite_path)
    return cfg, storage


def cmd_listener(args: argparse.Namespace) -> int:
    cfg, storage = _load(args)
    asyncio.run(run_listener(cfg, storage))
    return 0


def cmd_scheduler(args: argparse.Namespace) -> int:
    cfg, storage = _load(args)
    backend = build_backend(cfg.manager, storage=storage)
    try:
        asyncio.run(run_scheduler(cfg, backend, storage))
    finally:
        if hasattr(backend, "close"):
            backend.close()
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    cfg, storage = _load(args)
    backend = build_backend(cfg.manager, storage=storage)
    try:
        result = sync_customers_and_rules(backend, storage)
        load_routes_from_yaml(cfg, storage)
        flushed = flush_events_to_manager(backend, storage)
        result["events_flush"] = flushed
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        if hasattr(backend, "close"):
            backend.close()
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    cfg, storage = _load(args)
    health = storage.health()
    health["version"] = __version__
    health["manager_url"] = cfg.manager.base_url
    health["listener"] = {
        "bind_host": cfg.listener.bind_host,
        "bind_port": cfg.listener.bind_port,
        "hostname": cfg.listener.hostname,
        "accepted_domains": cfg.listener.accepted_domains,
    }
    for name in ("customers", "rules"):
        meta = storage.get_sync_meta(name)
        health[f"sync_{name}_at"] = dict(meta)["last_sync_at"] if meta else None
    print(json.dumps(health, ensure_ascii=False, indent=2))
    return 0


def cmd_routes(args: argparse.Namespace) -> int:
    cfg, storage = _load(args)
    rows = storage.list_routes()
    out = [
        {
            "alias": f"{r['local_part']}@{r['domain']}",
            "codcli": r["codcli"],
            "default_action": r["default_action"],
            "forward_target": r["forward_target"],
            "redirect_target": r["redirect_target"],
        }
        for r in rows
    ]
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def cmd_version(args: argparse.Namespace) -> int:
    print(__version__)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="relay", description="Stormshield SMTP Relay")
    parser.add_argument("-c", "--config", help="Path al file relay.yaml")
    parser.add_argument("-v", "--verbose", action="store_true", help="Log a livello DEBUG")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("listener", help="Avvia il server SMTP")
    sub.add_parser("scheduler", help="Avvia i loop di sync e flush eventi")
    sub.add_parser("sync", help="Esegue una sync una-tantum")
    sub.add_parser("status", help="Stampa health e statistiche")
    sub.add_parser("routes", help="Elenca le routes in cache")
    sub.add_parser("version", help="Stampa la versione")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _setup_logging(args.verbose)

    handlers = {
        "listener": cmd_listener,
        "scheduler": cmd_scheduler,
        "sync": cmd_sync,
        "status": cmd_status,
        "routes": cmd_routes,
        "version": cmd_version,
    }
    return handlers[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
