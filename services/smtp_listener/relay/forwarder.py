"""SmtpForwarder: relay outbound preservando il MIME originale.

Le firme DKIM richiedono che il body NON venga ricostruito (`.as_string()` modificherebbe
encoding/whitespace). Qui prependiamo header nuovi al blob bytes-level, preservando il
resto del messaggio.

Modalità TLS:
- 'none'         : nessun TLS
- 'opportunistic': prova STARTTLS, ma se non disponibile invia comunque in chiaro
- 'starttls'     : STARTTLS obbligatorio (errore se non offerto)
- 'ssl'          : connessione SMTPS dall'inizio (porta 465)
"""
from __future__ import annotations

import logging
import smtplib
import socket
import ssl
import time
from dataclasses import dataclass
from email.utils import formatdate

logger = logging.getLogger(__name__)


@dataclass
class RelayResult:
    ok: bool
    smtp_code: int
    smtp_message: str
    duration_ms: int
    smarthost: str
    error: str | None = None


def _prepend_headers(raw: bytes, headers: list[tuple[str, str]]) -> bytes:
    if not headers:
        return raw
    delim_idx = raw.find(b"\r\n\r\n")
    if delim_idx < 0:
        delim_idx = raw.find(b"\n\n")
        if delim_idx < 0:
            return b"".join(f"{k}: {v}\r\n".encode("utf-8") for k, v in headers) + b"\r\n" + raw
        line_sep = b"\n"
    else:
        line_sep = b"\r\n"
    inserted = b"".join(f"{k}: {v}{line_sep.decode()}".encode("utf-8") for k, v in headers)
    return raw[:delim_idx] + line_sep + inserted.rstrip(line_sep) + raw[delim_idx:]


def _build_received_header(helo_hostname: str, sender_ip: str | None = None) -> str:
    return (
        f"from {helo_hostname} "
        f"by {helo_hostname} "
        f"with stormshield-smtp-relay; {formatdate(localtime=True)}"
    )


class SmtpForwarder:
    def __init__(self, helo_hostname: str = "localhost", timeout_sec: int = 20):
        self._helo = helo_hostname
        self._timeout = timeout_sec

    def relay(
        self,
        *,
        mime_bytes: bytes,
        mail_from: str,
        rcpt_to: list[str],
        smarthost: str,
        smarthost_port: int = 25,
        tls_mode: str = "opportunistic",
        username: str | None = None,
        password: str | None = None,
        extra_headers: list[tuple[str, str]] | None = None,
    ) -> RelayResult:
        if not rcpt_to:
            return RelayResult(False, 0, "", 0, smarthost, error="nessun destinatario")

        prepended = list(extra_headers or [])
        prepended.insert(0, ("Received", _build_received_header(self._helo)))
        if not any(k.lower() == "x-domarc-forwarded-by" for k, _ in prepended):
            prepended.append(("X-Domarc-Forwarded-By", "stormshield-smtp-relay"))
        body = _prepend_headers(mime_bytes, prepended)

        start = time.monotonic()
        try:
            if tls_mode == "ssl":
                # SSL strict: cert verification attiva (rischio fallimento su cert self-signed)
                ctx = ssl.create_default_context()
                conn = smtplib.SMTP_SSL(
                    smarthost, smarthost_port, timeout=self._timeout, local_hostname=self._helo, context=ctx
                )
            else:
                conn = smtplib.SMTP(smarthost, smarthost_port, timeout=self._timeout, local_hostname=self._helo)

            try:
                conn.ehlo(self._helo)
                if tls_mode in ("starttls", "opportunistic"):
                    if conn.has_extn("starttls"):
                        # opportunistic: best-effort, NO cert verify (server interni con self-signed)
                        # starttls strict: cert verify attivo, fallisce se cert non valido
                        if tls_mode == "opportunistic":
                            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                            ctx.check_hostname = False
                            ctx.verify_mode = ssl.CERT_NONE
                        else:
                            ctx = ssl.create_default_context()
                        try:
                            conn.starttls(context=ctx)
                            conn.ehlo(self._helo)
                        except (ssl.SSLError, smtplib.SMTPException) as exc:
                            if tls_mode == "starttls":
                                raise
                            # opportunistic: cert error → fallback a chiaro
                            logger.warning("STARTTLS fallito (opportunistic, fallback in chiaro) verso %s:%d: %s",
                                           smarthost, smarthost_port, exc)
                            # Riconnetto in chiaro
                            try:
                                conn.quit()
                            except smtplib.SMTPException:
                                conn.close()
                            conn = smtplib.SMTP(smarthost, smarthost_port, timeout=self._timeout, local_hostname=self._helo)
                            conn.ehlo(self._helo)
                    elif tls_mode == "starttls":
                        raise smtplib.SMTPException("STARTTLS richiesto ma non offerto dal server")

                if username and password:
                    conn.login(username, password)

                refused = conn.sendmail(mail_from, rcpt_to, body)
                duration = int((time.monotonic() - start) * 1000)
                if refused:
                    msg = "; ".join(f"{r}: {refused[r]}" for r in refused)
                    return RelayResult(False, 550, msg, duration, smarthost, error=f"refused: {msg}")
                return RelayResult(True, 250, "delivered", duration, smarthost)
            finally:
                try:
                    conn.quit()
                except smtplib.SMTPException:
                    conn.close()
        except smtplib.SMTPResponseException as exc:
            duration = int((time.monotonic() - start) * 1000)
            return RelayResult(
                False, exc.smtp_code or 0, exc.smtp_error.decode("utf-8", errors="replace") if isinstance(exc.smtp_error, bytes) else str(exc.smtp_error),
                duration, smarthost, error=str(exc),
            )
        except (smtplib.SMTPException, socket.error, ssl.SSLError, OSError) as exc:
            duration = int((time.monotonic() - start) * 1000)
            return RelayResult(False, 0, "", duration, smarthost, error=f"{type(exc).__name__}: {exc}")
