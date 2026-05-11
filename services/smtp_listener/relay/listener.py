"""Listener SMTP con aiosmtpd.

In Fase 1 il listener fa: validazione anti-relay (RCPT bianca-list su routes_cache +
accepted_domains), validazione anti-loop (X-Domarc-Forwarded-By, Received chain depth),
parse MIME, INSERT in events_log, 250 OK. NESSUNA azione di rete (forward/redirect) e
NESSUNA chiamata al manager: tutto è sincrono e veloce per non bloccare la sessione SMTP.

Pipeline pesante (rule engine, AI, forward, dispatch ticket) è delegata allo scheduler.
"""
from __future__ import annotations

import asyncio
import logging
import ssl
from typing import Any

from aiosmtpd.controller import Controller
from aiosmtpd.smtp import SMTP, AuthResult, Envelope, Session

from relay import pipeline
from relay.config import RelayConfig
from relay.manager_client import ManagerBackend, build_backend
from relay.parser import parse_rfc822
from relay.storage import Storage

logger = logging.getLogger(__name__)

_LOOP_HEADER = "X-Domarc-Forwarded-By"
_LOOP_VALUE = "stormshield-smtp-relay"
_MAX_RECEIVED_HOPS = 25


class RelayHandler:
    def __init__(self, cfg: RelayConfig, storage: Storage, backend: ManagerBackend | None = None):
        self._cfg = cfg
        self._storage = storage
        self._backend = backend

    async def handle_EHLO(self, server: SMTP, session: Session, envelope: Envelope, hostname: str, responses: list[str]) -> list[str]:
        session.host_name = hostname
        return responses

    async def handle_MAIL(
        self,
        server: SMTP,
        session: Session,
        envelope: Envelope,
        address: str,
        mail_options: list[str],
    ) -> str:
        # M040: enforcement Relay client ACL.
        # session.peer = (ip, port). Se ACL ha entries, IP non whitelistati
        # vengono rifiutati con 550 5.7.1. Lista vuota = no enforcement.
        try:
            peer = getattr(session, "peer", None)
            if peer:
                client_ip = peer[0]
                enforce, allowed = self._storage.is_client_allowed(client_ip)
                if enforce and not allowed:
                    logger.warning(
                        "Relay ACL: rifiutata connessione da %s (mail_from=%s) "
                        "— IP non in whitelist relay_client_acl_cache",
                        client_ip, address,
                    )
                    return "550 5.7.1 Relaying denied (client not authorized)"
        except Exception as exc:  # noqa: BLE001
            # Fail-open: errore nel check ACL non blocca consegne legittime;
            # in pratica si torna al comportamento legacy.
            logger.exception("Relay ACL check failed (fail-open): %s", exc)
        envelope.mail_from = address
        envelope.mail_options.extend(mail_options)
        return "250 OK"

    async def handle_RCPT(
        self,
        server: SMTP,
        session: Session,
        envelope: Envelope,
        address: str,
        rcpt_options: list[str],
    ) -> str:
        local, _, domain = address.rpartition("@")
        if not local or not domain:
            return "550 5.1.3 Indirizzo destinatario invalido"

        domain_low = domain.lower()
        # Priorità domini: domain_routing_cache (configurato in UI) > settings > YAML
        accepted_set = set(self._storage.list_accepted_domains())
        if not accepted_set:
            accepted_dynamic = self._storage.get_setting("listener.accepted_domains")
            if isinstance(accepted_dynamic, list) and accepted_dynamic:
                accepted_set = {str(d).lower() for d in accepted_dynamic}
            else:
                accepted_set = {d.lower() for d in self._cfg.listener.accepted_domains}
        if accepted_set and domain_low not in accepted_set:
            return "550 5.7.1 Relaying denied (dominio non in whitelist)"

        # Le mail per un dominio gestito sono accettate anche se non c'è un alias specifico:
        # in quel caso la pipeline farà default delivery allo smarthost del dominio.

        envelope.rcpt_tos.append(address)
        envelope.rcpt_options.extend(rcpt_options)
        return "250 OK"

    async def handle_DATA(self, server: SMTP, session: Session, envelope: Envelope) -> str:
        raw: bytes = envelope.content if isinstance(envelope.content, bytes) else bytes(envelope.content or b"")
        if not raw:
            return "550 5.6.0 Messaggio vuoto"

        try:
            parsed = parse_rfc822(raw, loop_marker_header=_LOOP_HEADER, loop_marker_value=_LOOP_VALUE)
        except Exception as exc:
            logger.exception("Parse fallito da %s: %s", envelope.mail_from, exc)
            return "554 5.6.0 Messaggio non parsabile"

        pre_action: str | None = None
        pre_reason: str | None = None
        if parsed.has_loop_marker:
            pre_action, pre_reason = "quarantine", "loop_marker_detected"
        elif parsed.received_count > _MAX_RECEIVED_HOPS:
            pre_action, pre_reason = "quarantine", "too_many_hops"

        try:
            result = pipeline.process(
                parsed=parsed,
                cfg=self._cfg,
                storage=self._storage,
                backend=self._backend,
                pre_action=pre_action,
                pre_action_reason=pre_reason,
                envelope_rcpt_to=list(envelope.rcpt_tos or []),
            )
        except Exception as exc:
            logger.exception("Pipeline fallita per messaggio da %s: %s", envelope.mail_from, exc)
            # DLQ: salva il messaggio in quarantine cosi' non si perde traccia
            # di mail droppate per bug applicativi. Best-effort: se anche la
            # quarantine fallisce, log + 451.
            try:
                import uuid as _uuid
                self._storage.add_quarantine(
                    event_uuid=str(_uuid.uuid4()),
                    mime_blob=raw,
                    reason="pipeline_exception",
                    from_address=parsed.from_address or envelope.mail_from,
                    to_address=parsed.primary_to,
                )
            except Exception:  # noqa: BLE001
                logger.exception("DLQ quarantine fallita per messaggio da %s",
                                  envelope.mail_from)
            return "451 4.3.0 Errore di elaborazione temporaneo"

        logger.info(
            "Email accepted: uuid=%s from=%s to=%s action=%s codcli=%s rule=%s detail=%s",
            result.event_uuid,
            parsed.from_address or envelope.mail_from,
            parsed.primary_to,
            result.action_taken,
            result.codcli or "-",
            result.rule_id or "-",
            result.detail,
        )
        return "250 Message accepted for delivery"


def _build_ssl_context(cert_path: str, key_path: str) -> ssl.SSLContext:
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(certfile=cert_path, keyfile=key_path)
    return ctx


def build_controller(cfg: RelayConfig, storage: Storage, backend: ManagerBackend | None = None) -> Controller:
    handler = RelayHandler(cfg, storage, backend)
    data_size = cfg.listener.data_size_limit_mb * 1024 * 1024
    tls_context: ssl.SSLContext | None = None
    if cfg.listener.starttls.enabled:
        if not cfg.listener.starttls.cert_path or not cfg.listener.starttls.key_path:
            raise ValueError("STARTTLS abilitato ma cert/key path non configurati")
        tls_context = _build_ssl_context(cfg.listener.starttls.cert_path, cfg.listener.starttls.key_path)

    controller = Controller(
        handler=handler,
        hostname=cfg.listener.bind_host,
        port=cfg.listener.bind_port,
        ready_timeout=10.0,
        server_hostname=cfg.listener.hostname,
    )
    controller.SMTP_kwargs.update(
        {
            "hostname": cfg.listener.hostname,
            "data_size_limit": data_size,
            "enable_SMTPUTF8": True,
            "timeout": cfg.listener.session_timeout_sec,
        }
    )
    if tls_context is not None:
        controller.SMTP_kwargs["tls_context"] = tls_context
        controller.SMTP_kwargs["require_starttls"] = False
    return controller


async def run_listener(cfg: RelayConfig, storage: Storage) -> None:
    # Backend instanziato qui: serve in alcune azioni (es. auto_reply con generate_auth_code)
    # per chiamare endpoint manager (issue_auth_code). best-effort: se il manager è giù il
    # listener continua a funzionare, le azioni che richiedono backend logano warning.
    try:
        backend = build_backend(cfg.manager, storage=storage)
        logger.info("Backend manager inizializzato (%s)", cfg.manager.backend)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Impossibile inizializzare backend manager: %s — auto_reply senza auth_code", exc)
        backend = None
    controller = build_controller(cfg, storage, backend)
    controller.start()
    logger.info(
        "Listener SMTP avviato su %s:%d (hostname=%s, accepted_domains=%s)",
        cfg.listener.bind_host,
        cfg.listener.bind_port,
        cfg.listener.hostname,
        ",".join(cfg.listener.accepted_domains) or "<vuoto>",
    )
    stop_event = asyncio.Event()
    try:
        await stop_event.wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        controller.stop()
        if backend is not None and hasattr(backend, "close"):
            backend.close()
        logger.info("Listener SMTP fermato")
