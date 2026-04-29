"""Entrypoint CLI per `domarc-smtp-relay-admin`.

Comandi:
- serve         avvia il web server
- migrate       applica migration pendenti e exit
- migrate-to-pg esporta SQLite → PostgreSQL (stub v1.0)
"""
from __future__ import annotations

import argparse
import logging
import sys

from . import __version__
from .app import create_app
from .config import load_config


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="domarc-smtp-relay-admin",
        description=f"Domarc SMTP Relay — Admin Web v{__version__}",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_serve = sub.add_parser("serve", help="Avvia il web server")
    p_serve.add_argument("--host", default=None, help="Override DOMARC_RELAY_BIND_HOST")
    p_serve.add_argument("--port", type=int, default=None, help="Override DOMARC_RELAY_BIND_PORT")
    p_serve.add_argument("--debug", action="store_true", help="Flask debug mode")

    sub.add_parser("migrate", help="Applica migration pendenti ed esci")

    p_pg = sub.add_parser("migrate-to-pg", help="Esporta DB SQLite → PostgreSQL (stub)")
    p_pg.add_argument("--source", default=None, help="Path SQLite sorgente")
    p_pg.add_argument("--target", required=True, help="DSN PostgreSQL target")

    sub.add_parser("version", help="Stampa versione ed esci")

    args = parser.parse_args()
    if args.cmd == "version":
        print(f"domarc-smtp-relay-admin v{__version__}")
        return 0
    if args.cmd == "migrate":
        cfg = load_config()
        app = create_app(cfg, init_db=True)
        storage = app.extensions["domarc_storage"]
        n = storage.apply_migrations()
        print(f"Applicate {n} migrazioni. Schema version: {storage.schema_version()}")
        return 0
    if args.cmd == "migrate-to-pg":
        return migrate_to_pg(args)
    if args.cmd == "serve":
        cfg = load_config()
        if args.host:
            cfg.bind_host = args.host
        if args.port:
            cfg.bind_port = args.port
        if args.debug:
            cfg.debug = True
        app = create_app(cfg)
        logging.info("Domarc SMTP Relay Admin v%s avvio su %s:%d (backend=%s)",
                     __version__, cfg.bind_host, cfg.bind_port, cfg.db_backend)
        # Per produzione: gunicorn o uWSGI. Qui usiamo il dev server.
        app.run(host=cfg.bind_host, port=cfg.bind_port, debug=cfg.debug, threaded=True)
        return 0
    parser.print_help()
    return 2


def migrate_to_pg(args=None) -> int:
    """Stub v1.0. Implementazione completa in fase post-v1.0 quando arriva
    un cliente che chiede PostgreSQL."""
    print(
        "ERRORE: migrate-to-pg non ancora implementato in v1.0.\n"
        "PostgreSQL è documentato come opzione opt-in nel piano standalone (D6).\n"
        "Per ora resta su SQLite (default).",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
