"""Parser MIME minimale per Fase 1.

Estrae i campi essenziali da un messaggio RFC822: header chiave, dominio mittente,
local part destinatario, body text/html (cap dimensionali), elenco allegati con metadata.
NON ricostruisce il MIME — il blob raw resta a disposizione del forwarder per il relay
fedele preservando le firme DKIM.
"""
from __future__ import annotations

import email
import email.policy
import logging
from dataclasses import dataclass, field
from email.header import decode_header, make_header
from email.message import Message
from email.utils import getaddresses, parseaddr
from typing import Any


def _decode_mime_header(raw: str | None) -> str:
    """Decodifica RFC 2047 (=?UTF-8?B?...?=) → Unicode reale.

    Preserva il valore originale se la decodifica fallisce. Da chiamare UNA SOLA
    volta sui header di mail in entrata (Subject, From-display-name, ...) per
    evitare che concatenazioni successive lascino encoded blob nei nuovi mail
    in uscita (bug doppio encoding).
    """
    if not raw:
        return ""
    try:
        return str(make_header(decode_header(raw))).strip()
    except Exception:  # noqa: BLE001
        return raw.strip()

logger = logging.getLogger(__name__)

_MAX_BODY_TEXT = 64 * 1024
_MAX_BODY_HTML = 256 * 1024
_MAX_ATTACHMENTS = 50

_AUTO_HEADERS = (
    "auto-submitted",
    "x-auto-response-suppress",
    "list-id",
    "list-unsubscribe",
    "precedence",
)


@dataclass
class ParsedAttachment:
    filename: str | None
    content_type: str
    size_bytes: int


@dataclass
class ParsedMessage:
    raw: bytes
    headers: dict[str, str]
    from_address: str
    from_domain: str
    to_addresses: list[str]
    primary_to: str | None
    primary_to_local: str | None
    primary_to_domain: str | None
    subject: str
    message_id: str | None
    in_reply_to: str | None
    references: list[str]
    body_text: str
    body_html: str
    attachments: list[ParsedAttachment] = field(default_factory=list)
    received_count: int = 0
    is_auto_or_bulk: bool = False
    has_loop_marker: bool = False
    loop_marker_value: str | None = None


def _split_address_local_domain(addr: str) -> tuple[str | None, str | None]:
    if not addr or "@" not in addr:
        return None, None
    local, _, domain = addr.rpartition("@")
    if not local or not domain:
        return None, None
    return local.lower(), domain.lower()


def _decode_text(part: Message, max_bytes: int) -> str:
    payload = part.get_payload(decode=True) or b""
    if not payload:
        return ""
    if len(payload) > max_bytes:
        payload = payload[:max_bytes]
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except (LookupError, UnicodeDecodeError):
        return payload.decode("utf-8", errors="replace")


def parse_rfc822(raw: bytes, *, loop_marker_header: str = "X-Domarc-Forwarded-By", loop_marker_value: str = "stormshield-smtp-relay") -> ParsedMessage:
    msg: Message = email.message_from_bytes(raw, policy=email.policy.compat32)

    headers: dict[str, str] = {}
    for name, value in msg.items():
        headers.setdefault(name, str(value))

    from_addr_raw = msg.get("From", "")
    _, from_addr = parseaddr(from_addr_raw)
    from_addr = (from_addr or "").strip()
    _, from_domain = _split_address_local_domain(from_addr)

    to_pairs = getaddresses(msg.get_all("To", []) + msg.get_all("Cc", []))
    to_addresses = [addr for _, addr in to_pairs if addr and "@" in addr]
    primary_to = to_addresses[0] if to_addresses else None
    primary_to_local, primary_to_domain = _split_address_local_domain(primary_to or "")

    # Decodifica RFC 2047 una sola volta (Unicode pulito):
    # evita doppio-encoding quando il subject viene poi concatenato in template
    # auto-reply (es. "AUTH-XXX – {{ subject }}" → vecchio bug =?UTF-8?B?...?=).
    subject = _decode_mime_header(msg.get("Subject", ""))
    message_id = (msg.get("Message-ID") or msg.get("Message-Id") or "").strip() or None
    in_reply_to = (msg.get("In-Reply-To") or "").strip() or None
    references_raw = msg.get("References", "") or ""
    references = [r for r in references_raw.split() if r]

    received_count = len(msg.get_all("Received") or [])

    auto_signal = any((msg.get(h) or "").strip() for h in _AUTO_HEADERS)
    precedence = (msg.get("Precedence") or "").strip().lower()
    if precedence in ("bulk", "list", "junk"):
        auto_signal = True

    loop_value = (msg.get(loop_marker_header) or "").strip() or None
    has_loop = loop_value is not None and loop_marker_value.lower() in loop_value.lower()

    body_text = ""
    body_html = ""
    attachments: list[ParsedAttachment] = []

    if msg.is_multipart():
        for part in msg.walk():
            if part.is_multipart():
                continue
            ctype = (part.get_content_type() or "").lower()
            disp = (part.get("Content-Disposition") or "").lower()
            filename = part.get_filename()
            if filename or "attachment" in disp:
                if len(attachments) < _MAX_ATTACHMENTS:
                    payload = part.get_payload(decode=True) or b""
                    attachments.append(
                        ParsedAttachment(
                            filename=filename,
                            content_type=ctype or "application/octet-stream",
                            size_bytes=len(payload),
                        )
                    )
                continue
            if ctype == "text/plain" and not body_text:
                body_text = _decode_text(part, _MAX_BODY_TEXT)
            elif ctype == "text/html" and not body_html:
                body_html = _decode_text(part, _MAX_BODY_HTML)
    else:
        ctype = (msg.get_content_type() or "").lower()
        if ctype == "text/html":
            body_html = _decode_text(msg, _MAX_BODY_HTML)
        else:
            body_text = _decode_text(msg, _MAX_BODY_TEXT)

    return ParsedMessage(
        raw=raw,
        headers=headers,
        from_address=from_addr,
        from_domain=from_domain or "",
        to_addresses=to_addresses,
        primary_to=primary_to,
        primary_to_local=primary_to_local,
        primary_to_domain=primary_to_domain,
        subject=subject,
        message_id=message_id,
        in_reply_to=in_reply_to,
        references=references,
        body_text=body_text,
        body_html=body_html,
        attachments=attachments,
        received_count=received_count,
        is_auto_or_bulk=auto_signal,
        has_loop_marker=has_loop,
        loop_marker_value=loop_value,
    )


def parsed_summary(p: ParsedMessage) -> dict[str, Any]:
    return {
        "from": p.from_address,
        "to": p.primary_to,
        "subject": p.subject,
        "message_id": p.message_id,
        "in_reply_to": p.in_reply_to,
        "received_count": p.received_count,
        "attachments": len(p.attachments),
        "is_auto_or_bulk": p.is_auto_or_bulk,
        "has_loop_marker": p.has_loop_marker,
    }
