"""Render auto-reply via Jinja2 sandboxed.

I template di default sono in `relay/templates/`. L'utente può fornire template aggiuntivi
posizionandoli in una directory esterna e referenziandoli per nome file (`auto_reply_template`
nel `action_map` della regola).

L'invio effettivo è demandato al `SmtpForwarder` (Fase 3): qui costruiamo solo il MIME.
"""
from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape
from jinja2.sandbox import SandboxedEnvironment

logger = logging.getLogger(__name__)

_DEFAULT_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"


def _build_env(extra_dirs: list[str] | None = None) -> SandboxedEnvironment:
    dirs: list[str] = [str(_DEFAULT_TEMPLATES_DIR)]
    if extra_dirs:
        dirs.extend(d for d in extra_dirs if d)
    env = SandboxedEnvironment(
        loader=FileSystemLoader(dirs, encoding="utf-8"),
        autoescape=select_autoescape(disabled_extensions=("txt",)),
        undefined=StrictUndefined,
        keep_trailing_newline=True,
    )
    return env


def render_template(name: str, context: dict[str, Any], extra_dirs: list[str] | None = None) -> str:
    env = _build_env(extra_dirs)
    if not name.endswith((".txt", ".html")):
        name = f"{name}.txt"
    template = env.get_template(name)
    return template.render(**context)


def build_auto_reply(
    *,
    template_name: str,
    sender: str,
    recipient: str,
    in_reply_to: str | None,
    references: list[str] | None,
    subject_original: str,
    context: dict[str, Any],
    extra_dirs: list[str] | None = None,
) -> EmailMessage:
    body = render_template(template_name, context, extra_dirs)
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = recipient
    subj = subject_original or ""
    if not subj.lower().startswith("re:"):
        subj = f"Re: {subj}"
    msg["Subject"] = subj
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=sender.split("@", 1)[-1] if "@" in sender else "localhost")
    msg["Auto-Submitted"] = "auto-replied"
    msg["X-Domarc-Forwarded-By"] = "stormshield-smtp-relay"
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references or in_reply_to:
        ref_list = list(references or [])
        if in_reply_to and in_reply_to not in ref_list:
            ref_list.append(in_reply_to)
        if ref_list:
            msg["References"] = " ".join(ref_list)
    msg.set_content(body)
    return msg


def _format_from(name: str | None, email: str) -> str:
    """Costruisce 'Nome Visualizzato <email@dom>' o solo email se name vuoto."""
    n = (name or "").strip()
    if n:
        if any(c in n for c in ',;"'):
            n = '"' + n.replace('"', '\\"') + '"'
        return f"{n} <{email}>"
    return email


def build_auto_reply_db(
    *,
    tpl_row: Any,  # sqlite3.Row con campi del DB template
    sender_email_override: str | None,
    recipient: str,
    in_reply_to: str | None,
    references: list[str] | None,
    subject_original: str,
    context: dict[str, Any],
    subject_prefix: str | None = None,
    reply_to: str | None = None,
    quote_original: bool = False,
    attach_original: bool = False,
    original_mime: bytes | None = None,
    original_body_text: str | None = None,
    original_body_html: str | None = None,
) -> EmailMessage:
    """Costruisce l'auto-reply usando un template dal DB del manager (tabella auto_reply_templates).

    Il template ha 4 campi Jinja2 renderizzati con `context`:
    - subject_tmpl       (testo, obbligatorio)
    - body_html_tmpl     (HTML, opzionale)
    - body_text_tmpl     (testo, opzionale)
    Almeno uno tra body_html_tmpl e body_text_tmpl deve essere valorizzato.

    Mittente:
    - Se `sender_email_override` valorizzato → usato come email
    - Altrimenti → tpl_row.reply_from_email
    Display name preso da tpl_row.reply_from_name se presente.
    """
    env = SandboxedEnvironment(
        autoescape=False,
        undefined=StrictUndefined,
        keep_trailing_newline=True,
    )
    full_context = dict(context)
    full_context.setdefault("subject_originale", subject_original)
    full_context.setdefault("subject", subject_original)

    def _render(tmpl_text: str | None) -> str | None:
        if not tmpl_text:
            return None
        try:
            return env.from_string(tmpl_text).render(**full_context)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Render template DB fallito (id=%s): %s", tpl_row["id"], exc)
            return tmpl_text  # fallback al raw template

    subject = _render(tpl_row["subject_tmpl"]) or "Risposta automatica"
    body_html = _render(tpl_row["body_html_tmpl"])
    body_text = _render(tpl_row["body_text_tmpl"])

    # Prefisso oggetto (es. "Re: ") configurato a livello di regola.
    if subject_prefix:
        sp = subject_prefix.rstrip() + " "
        if not subject.lower().startswith(sp.lower().rstrip()):
            subject = sp + subject

    # Quote del testo originale in coda al corpo.
    if quote_original:
        quoted_text = (original_body_text or "").strip()
        if quoted_text:
            sep = "\n\n--- Messaggio originale ---\n"
            quoted_lines = "\n".join(f"> {ln}" for ln in quoted_text.splitlines())
            if body_text:
                body_text = body_text + sep + quoted_lines + "\n"
            else:
                body_text = sep + quoted_lines + "\n"
            if body_html:
                # Append HTML semplice (escape minimale)
                escaped = (quoted_text.replace("&", "&amp;")
                                       .replace("<", "&lt;")
                                       .replace(">", "&gt;")
                                       .replace("\n", "<br>"))
                body_html = (body_html +
                             '<hr><blockquote style="color:#666; '
                             'border-left:3px solid #ccc; padding-left:10px; '
                             'margin-left:0;">' + escaped + '</blockquote>')

    sender_email = (sender_email_override or tpl_row["reply_from_email"] or "").strip()
    if not sender_email:
        sender_email = "noreply@localhost"
    sender_display = _format_from(tpl_row["reply_from_name"], sender_email)

    msg = EmailMessage()
    msg["From"] = sender_display
    msg["To"] = recipient
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=sender_email.split("@", 1)[-1] if "@" in sender_email else "localhost")
    msg["Auto-Submitted"] = "auto-replied"
    msg["X-Domarc-Forwarded-By"] = "stormshield-smtp-relay"
    msg["X-Domarc-Auto-Reply-Template-Id"] = str(tpl_row["id"])
    if reply_to:
        msg["Reply-To"] = reply_to
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references or in_reply_to:
        ref_list = list(references or [])
        if in_reply_to and in_reply_to not in ref_list:
            ref_list.append(in_reply_to)
        if ref_list:
            msg["References"] = " ".join(ref_list)

    # Multipart text+html se entrambi presenti
    if body_text and body_html:
        msg.set_content(body_text)
        msg.add_alternative(body_html, subtype="html")
    elif body_html:
        msg.set_content(body_html, subtype="html")
    elif body_text:
        msg.set_content(body_text)
    else:
        msg.set_content("(template senza body)")

    # Allegato del messaggio originale come .eml (RFC 2046).
    if attach_original and original_mime:
        msg.add_attachment(
            original_mime,
            maintype="message",
            subtype="rfc822",
            filename="messaggio_originale.eml",
        )

    return msg


def send_auto_reply(
    msg: EmailMessage,
    *,
    smarthost: str,
    smarthost_port: int = 25,
    timeout: int = 20,
    helo_hostname: str | None = None,
) -> None:
    with smtplib.SMTP(smarthost, smarthost_port, timeout=timeout, local_hostname=helo_hostname) as s:
        s.send_message(msg)
    logger.info("Auto-reply inviato a %s tramite %s:%d", msg["To"], smarthost, smarthost_port)
