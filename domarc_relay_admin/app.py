"""Flask app factory Domarc SMTP Relay Admin."""
from __future__ import annotations

import logging
import os
from datetime import timedelta
from pathlib import Path
from typing import Any

from flask import Flask

from . import __version__
from .config import AppConfig, load_config


def create_app(config: AppConfig | None = None, *, init_db: bool = True) -> Flask:
    cfg = config or load_config()

    # Setup logging base
    log_level = logging.DEBUG if cfg.debug else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )

    # Template + static path: pacchetto-locale, no dipendenze dal manager
    pkg_dir = Path(__file__).parent
    template_dir = pkg_dir.parent / "templates"
    static_dir = pkg_dir.parent / "static"

    app = Flask(
        __name__,
        template_folder=str(template_dir),
        static_folder=str(static_dir),
        static_url_path="/static",
    )
    app.config["SECRET_KEY"] = cfg.secret_key
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(hours=8)
    app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024  # 10 MB upload limit (allegati template)

    # Storage init (può applicare migrazioni se init_db=True)
    from .storage import get_storage
    storage = get_storage(cfg)
    if init_db and hasattr(storage, "apply_migrations"):
        try:
            storage.apply_migrations()
        except Exception as exc:  # noqa: BLE001
            logging.error("apply_migrations all'init fallito: %s", exc)

    # Customer source
    from .customer_sources import get_customer_source
    try:
        customer_source = get_customer_source(cfg)
    except Exception as exc:  # noqa: BLE001
        logging.warning("Customer source init fallito (continua con stub): %s", exc)
        from .customer_sources.base import CustomerSource

        class _StubCustomerSource(CustomerSource):
            def list_customers(self): return []
            def get_by_codcli(self, codcli): return None
            def health(self): return {"backend": "stub", "error": str(exc)}

        customer_source = _StubCustomerSource()

    app.extensions["domarc_storage"] = storage
    app.extensions["domarc_customer_source"] = customer_source
    app.extensions["domarc_config"] = cfg
    app.extensions["domarc_version"] = __version__

    # Register blueprints
    from .auth import auth_bp
    from .routes import dashboard_bp, health_bp
    from .routes.rules import rules_bp
    from .routes.templates import templates_bp
    from .routes.service_hours import service_hours_bp
    from .routes.auth_codes import auth_codes_bp
    from .routes.events import events_bp
    from .routes.aggregations import aggregations_bp
    from .routes.customers import customers_bp
    from .routes.profiles import profiles_bp
    from .routes.users import users_bp
    from .routes.api import api_bp
    from .routes.infrastructure import (routes_bp, domains_bp, addresses_bp,
                                          settings_bp, connection_bp)
    from .routes.privacy_bypass import privacy_bp
    from .routes.ai import ai_bp
    from .routes.secrets_modules import secrets_modules_bp
    from .routes.manual import manual_bp
    from .routes.activity import activity_bp
    from .routes.queue import queue_bp
    from .routes.customer_groups import customer_groups_bp
    from .tenants import tenants_bp, register_tenant_middleware
    app.register_blueprint(auth_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(health_bp)
    app.register_blueprint(rules_bp)
    app.register_blueprint(templates_bp)
    app.register_blueprint(service_hours_bp)
    app.register_blueprint(auth_codes_bp)
    app.register_blueprint(events_bp)
    app.register_blueprint(aggregations_bp)
    app.register_blueprint(customers_bp)
    app.register_blueprint(profiles_bp)
    app.register_blueprint(users_bp)
    app.register_blueprint(api_bp)
    app.register_blueprint(routes_bp)
    app.register_blueprint(domains_bp)
    app.register_blueprint(addresses_bp)
    app.register_blueprint(settings_bp)
    app.register_blueprint(connection_bp)
    app.register_blueprint(privacy_bp)
    app.register_blueprint(ai_bp)
    app.register_blueprint(secrets_modules_bp)
    app.register_blueprint(manual_bp)
    app.register_blueprint(activity_bp)
    app.register_blueprint(queue_bp)
    app.register_blueprint(customer_groups_bp)
    app.register_blueprint(tenants_bp)

    # Manual auto-generato: rigenera all'avvio (best-effort, ignora errori).
    try:
        from .manual_generator import write_manual
        write_manual(app)
    except Exception as exc:  # noqa: BLE001
        logging.getLogger(__name__).warning("Auto-rigenerazione manual.md fallita: %s", exc)

    # Carica le API key cifrate da DB → os.environ (migration 013).
    # Effettuato DOPO la registrazione dei blueprint per non bloccare l'avvio
    # se il modulo cryptography manca o la master.key è invalida.
    try:
        from .secrets_manager import load_secrets_into_env
        load_secrets_into_env(storage)
    except Exception as exc:  # noqa: BLE001
        logging.getLogger(__name__).warning(
            "load_secrets_into_env fallito (continuo senza): %s", exc,
        )
    register_tenant_middleware(app)

    # First-time admin user: se non esiste alcun utente, crea 'admin' con password = DOMARC_RELAY_BOOTSTRAP_PASSWORD
    # (env var) oppure 'admin123' come fallback con WARNING ben visibile.
    try:
        if init_db and not storage.list_users():
            bootstrap_pwd = os.environ.get("DOMARC_RELAY_BOOTSTRAP_PASSWORD", "admin123")
            storage.upsert_user({
                "username": "admin",
                "password": bootstrap_pwd,
                "role": "admin",
                "full_name": "Bootstrap admin",
                "enabled": True,
            })
            logging.warning(
                "BOOTSTRAP: creato utente 'admin' con password '%s'. "
                "CAMBIARE SUBITO al primo login (TODO: page change-password v1.0).",
                bootstrap_pwd if bootstrap_pwd != "admin123" else "admin123 (DEFAULT INSICURO)",
            )
    except Exception as exc:  # noqa: BLE001
        logging.error("Bootstrap admin user fallito: %s", exc)

    @app.context_processor
    def _inject_globals() -> dict[str, Any]:
        passthrough = False
        try:
            v = (storage.get_setting("relay_passthrough_only") or "false").strip().lower()
            passthrough = v in ("true", "1", "yes", "on")
        except Exception:  # noqa: BLE001
            pass
        return {
            "DOMARC_RELAY_VERSION": __version__,
            "RELAY_PASSTHROUGH_ONLY": passthrough,
        }

    return app
