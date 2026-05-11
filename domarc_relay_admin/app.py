"""Flask app factory Domarc SMTP Relay Admin."""
from __future__ import annotations

import logging
import os
from datetime import timedelta
from pathlib import Path
from typing import Any

from flask import Flask, render_template
from werkzeug.middleware.proxy_fix import ProxyFix

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
    # ProxyFix: dietro nginx (HTTPS termination). Senza, Flask vede sempre
    # scheme=http e SESSION_COOKIE_SECURE non funziona, CSRF_SSL_STRICT fallisce.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    app.config["SECRET_KEY"] = cfg.secret_key
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    # Secure cookie + CSRF SSL strict abilitati in prod (non-debug). In dev locale
    # rimangono off altrimenti la sessione non funziona via http://localhost.
    app.config["SESSION_COOKIE_SECURE"] = not cfg.debug
    app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(hours=8)
    app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024  # 10 MB upload limit (allegati template)
    # CSRF Protection (Flask-WTF). Token validato su ogni POST/PUT/DELETE delle
    # blueprint UI (form HTML), esentiamo solo le route API protette da X-API-Key
    # (vedi /api/v1/relay/*). I form HTML devono includere `{{ csrf_token() }}`.
    app.config["WTF_CSRF_TIME_LIMIT"] = 8 * 3600  # 8h, allineato a session lifetime
    app.config["WTF_CSRF_SSL_STRICT"] = not cfg.debug  # con ProxyFix è ora corretto
    try:
        from flask_wtf.csrf import CSRFProtect
        csrf = CSRFProtect(app)
        # API endpoints sono autenticati via X-API-Key, non hanno cookie session.
        # Esenzione totale del blueprint api.
        from .routes.api import api_bp as _api_bp
        csrf.exempt(_api_bp)
        # Esento il singolo endpoint preview_render: idempotente, solo render
        # Jinja in memoria, niente scrittura DB. Il fetch JS dal browser non
        # passa naturalmente il token CSRF — esentarlo è la soluzione più pulita.
        try:
            from .routes.templates import preview_render as _tpl_preview
            csrf.exempt(_tpl_preview)
        except Exception:  # noqa: BLE001
            pass
        # Esento anche /rules/preview-impact e /rules/test-regex: idempotenti,
        # solo lettura events_log + valutazione regex in memoria. Il fetch JS
        # dei form regola passa il CSRF come header X-CSRFToken ma con
        # FormData boundary multipart e SameSite cookie talvolta non viene
        # validato correttamente — esentarli è coerente con preview_render.
        try:
            from .routes.rules import preview_impact as _r_impact, test_regex as _r_test
            csrf.exempt(_r_impact)
            csrf.exempt(_r_test)
        except Exception:  # noqa: BLE001
            pass
        app.extensions["domarc_csrf"] = csrf
    except Exception as exc:  # noqa: BLE001
        logging.warning("Flask-WTF CSRF non disponibile: %s — running senza protection", exc)

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
        customer_source = get_customer_source(cfg, storage=storage)
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
    from .routes.h24_codes import h24_codes_bp
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
    from .routes.recipients import recipient_groups_bp
    from .routes.codes_h24 import codes_h24_bp
    from .routes.integrations import integrations_bp
    from .routes.customer_sync import customer_sync_bp
    from .routes.rule_sets import rule_sets_bp
    from .routes.shadow import shadow_bp
    from .routes.group_mapping import group_mapping_bp
    from .routes.domains import domain_strategy_bp
    from .routes.ai_rule_wizard import ai_rule_wizard_bp
    from .routes.relay_acl import relay_acl_bp
    from .routes.firewall import firewall_bp
    from .routes.metrics import metrics_bp
    from .tenants import tenants_bp, register_tenant_middleware
    app.register_blueprint(auth_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(health_bp)
    app.register_blueprint(rules_bp)
    app.register_blueprint(templates_bp)
    app.register_blueprint(service_hours_bp)
    app.register_blueprint(auth_codes_bp)
    app.register_blueprint(h24_codes_bp)
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
    app.register_blueprint(recipient_groups_bp)
    app.register_blueprint(codes_h24_bp)
    app.register_blueprint(integrations_bp)
    app.register_blueprint(customer_sync_bp)
    app.register_blueprint(rule_sets_bp)
    app.register_blueprint(shadow_bp)
    app.register_blueprint(group_mapping_bp)
    app.register_blueprint(domain_strategy_bp)
    app.register_blueprint(ai_rule_wizard_bp)
    app.register_blueprint(relay_acl_bp)
    app.register_blueprint(firewall_bp)
    app.register_blueprint(metrics_bp)
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

    # Customer sync scheduler (M028): thread daemon che esegue le sorgenti
    # configurate in customer_sync_sources secondo schedule_hours. Idempotente.
    # Sostituisce il vecchio start_sync_thread() del backend `postgres`
    # (la sorgente legacy "Postgres solution Domarc" ora vive come row in
    # customer_sync_sources con sentinel _use_legacy_pgconfig=true).
    if init_db:
        try:
            from .customer_sync.scheduler import start_sync_scheduler
            start_sync_scheduler(storage, check_interval_sec=60)
        except Exception as exc:  # noqa: BLE001
            logging.getLogger(__name__).warning(
                "customer_sync scheduler non avviato: %s", exc,
            )
        # Retention thread: purge body (ogni 10min) + cleanup notturno (DELETE
        # log/audit vecchi). Evita crescita illimitata events.body_text/html.
        try:
            from .retention import start_retention_thread
            start_retention_thread(storage)
        except Exception as exc:  # noqa: BLE001
            logging.getLogger(__name__).warning(
                "retention thread non avviato: %s", exc,
            )

    # First-time admin user: crea 'admin' con password = DOMARC_RELAY_BOOTSTRAP_PASSWORD.
    # In prod (cfg.debug=False) e' OBBLIGATORIA: niente fallback insicuro.
    try:
        if init_db and not storage.list_users():
            bootstrap_pwd = os.environ.get("DOMARC_RELAY_BOOTSTRAP_PASSWORD", "")
            if not bootstrap_pwd:
                if not cfg.debug:
                    raise RuntimeError(
                        "DOMARC_RELAY_BOOTSTRAP_PASSWORD non impostata. "
                        "In produzione e' obbligatoria per il primo utente admin. "
                        "Aggiungere a /etc/domarc-smtp-relay-admin/secrets.env."
                    )
                bootstrap_pwd = "admin123"  # solo dev locale
                logging.warning("BOOTSTRAP DEV: usata password fallback 'admin123' (cfg.debug=True)")
            if len(bootstrap_pwd) < 10 and not cfg.debug:
                raise RuntimeError(
                    "DOMARC_RELAY_BOOTSTRAP_PASSWORD troppo corta (<10 caratteri). "
                    "Usare almeno 16 caratteri random."
                )
            storage.upsert_user({
                "username": "admin",
                "password": bootstrap_pwd,
                "role": "admin",
                "full_name": "Bootstrap admin",
                "enabled": True,
            })
            logging.warning(
                "BOOTSTRAP: creato utente 'admin'. "
                "Cambiare la password al primo login (UI: /users/me/password)."
            )
    except RuntimeError:
        raise  # fail-fast in prod
    except Exception as exc:  # noqa: BLE001
        logging.error("Bootstrap admin user fallito: %s", exc)

    # Error handler: 404/403/500 con pagina utente generica + log strutturato.
    @app.errorhandler(404)
    def _not_found(exc):  # noqa: ANN001
        try:
            return render_template("admin/error.html", code=404,
                                    message="Pagina non trovata"), 404
        except Exception:  # noqa: BLE001
            return "404 — Pagina non trovata", 404

    @app.errorhandler(403)
    def _forbidden(exc):  # noqa: ANN001
        try:
            return render_template("admin/error.html", code=403,
                                    message="Accesso negato"), 403
        except Exception:  # noqa: BLE001
            return "403 — Accesso negato", 403

    @app.errorhandler(500)
    def _internal(exc):  # noqa: ANN001
        logging.getLogger(__name__).exception("500 Internal Server Error")
        try:
            return render_template("admin/error.html", code=500,
                                    message="Errore interno. L'incidente e' stato registrato."), 500
        except Exception:  # noqa: BLE001
            return "500 — Errore interno", 500

    @app.errorhandler(413)
    def _too_large(exc):  # noqa: ANN001
        max_mb = app.config.get("MAX_CONTENT_LENGTH", 10*1024*1024) // (1024*1024)
        try:
            return render_template("admin/error.html", code=413,
                                    message=f"File troppo grande (massimo {max_mb} MB). Riprova con un file piu' piccolo."), 413
        except Exception:  # noqa: BLE001
            return f"413 — File troppo grande (max {max_mb} MB)", 413

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
