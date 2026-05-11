"""Wrapper sicuro per `ufw` (Uncomplicated Firewall).

Permette all'admin web Flask (che gira come utente `domarc-relay` non root)
di leggere/modificare regole UFW via `sudo` con regole sudoers ristrette.

Sicurezza:
- `subprocess.run(..., shell=False)` — niente shell parsing, niente injection.
- Argomenti validati Python-side PRIMA di chiamare ufw (whitelist regex per
  port, proto, IP/CIDR, comment).
- Solo ruolo `superadmin` può chiamare le funzioni mutative dalla UI.
- Ogni modifica logga audit (chi, cosa, quando) via logger.

Comandi consentiti (vedi `/etc/sudoers.d/domarc-relay-ufw`):
- `ufw status numbered`
- `ufw status verbose`
- `ufw allow ...`     (con argomenti validati)
- `ufw --force delete N`
- `ufw reload`
"""
from __future__ import annotations

import ipaddress
import logging
import re
import shutil
import subprocess
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


UFW_BIN = "/usr/sbin/ufw"
SUDO_BIN = "/usr/bin/sudo"
TIMEOUT_SEC = 8


# ============================================================ VALIDATION ===

_PORT_RE = re.compile(r"^\d{1,5}$")
_PROTO_RE = re.compile(r"^(tcp|udp)$")
_COMMENT_RE = re.compile(r"^[\w\s\-\.\,\/:()@]{0,80}$")


def _validate_port(port: str) -> int:
    if not _PORT_RE.match(port):
        raise ValueError(f"porta non valida: {port!r}")
    p = int(port)
    if not (1 <= p <= 65535):
        raise ValueError(f"porta fuori range 1-65535: {p}")
    return p


def _validate_proto(proto: str) -> str:
    proto = proto.lower().strip()
    if not _PROTO_RE.match(proto):
        raise ValueError(f"protocollo non supportato (solo tcp/udp): {proto!r}")
    return proto


def _validate_source(source: str | None) -> str | None:
    """Source = IP singolo, CIDR, oppure 'any'/None."""
    if not source or source.strip().lower() in ("any", "anywhere"):
        return None
    s = source.strip()
    try:
        ipaddress.ip_network(s, strict=False)
    except ValueError as exc:
        raise ValueError(f"IP/CIDR sorgente non valido: {s} ({exc})") from exc
    return s


def _validate_comment(comment: str | None) -> str | None:
    if not comment:
        return None
    comment = comment.strip()
    if not _COMMENT_RE.match(comment):
        raise ValueError(
            "commento contiene caratteri non ammessi. "
            "Solo lettere, cifre, spazi e .-,/:()@_"
        )
    return comment[:80]


def _validate_rule_number(num: int) -> int:
    if not isinstance(num, int) or num < 1 or num > 999:
        raise ValueError(f"numero regola non valido: {num}")
    return num


# ============================================================== RUNNER ====

def _run(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
    cmd = [SUDO_BIN, "-n", UFW_BIN] + args
    logger.info("UFW exec: %s", " ".join(cmd))
    try:
        cp = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SEC,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise UfwError(f"ufw timeout dopo {TIMEOUT_SEC}s") from exc
    if check and cp.returncode != 0:
        raise UfwError(
            f"ufw exit {cp.returncode}: {cp.stderr.strip() or cp.stdout.strip()}"
        )
    return cp


class UfwError(RuntimeError):
    pass


# ============================================================ INSPECTION ==

@dataclass
class UfwRule:
    number: int
    to: str
    action: str       # "ALLOW IN" / "DENY IN" / "REJECT IN"
    from_: str
    proto: str | None
    comment: str | None
    raw: str

    @property
    def is_allow(self) -> bool:
        return "ALLOW" in self.action.upper()


def _parse_status_numbered(output: str) -> tuple[bool, list[UfwRule]]:
    """Parser dell'output `ufw status numbered`.
    Ritorna (active, rules)."""
    active = False
    rules: list[UfwRule] = []
    for line in output.splitlines():
        line = line.rstrip()
        s = line.strip()
        if s.lower().startswith("status:"):
            active = "active" in s.lower()
            continue
        # Riga regola: [ N] To ... Action ... From ... [# comment]
        m = re.match(
            r"\[\s*(\d+)\]\s+(.+?)\s+(ALLOW\s+\w+|DENY\s+\w+|REJECT\s+\w+|LIMIT\s+\w+)\s+(.+)$",
            s,
        )
        if not m:
            continue
        number = int(m.group(1))
        to_field = m.group(2).strip()
        action = m.group(3).strip()
        rest = m.group(4).strip()
        # rest = "From ... # comment" oppure "From ..."
        comment = None
        if "#" in rest:
            from_field, _, comment = rest.partition("#")
            from_field = from_field.strip()
            comment = comment.strip() or None
        else:
            from_field = rest.strip()
        # proto su to_field: es. "25/tcp" → proto=tcp
        proto = None
        m2 = re.match(r"^(.+?)/(tcp|udp)$", to_field)
        if m2:
            to_field = m2.group(1)
            proto = m2.group(2)
        rules.append(UfwRule(
            number=number, to=to_field, action=action,
            from_=from_field, proto=proto, comment=comment, raw=s,
        ))
    return (active, rules)


def is_available() -> bool:
    available, _ = check_availability()
    return available


def check_availability() -> tuple[bool, str]:
    """Ritorna (available, diagnostic_message).
    Il messaggio diagnostico viene mostrato in UI se available=False."""
    import os
    # 1) ufw binary presente?
    if not os.path.exists(UFW_BIN):
        return (False, f"binario UFW non trovato in {UFW_BIN}. Esegui: apt install ufw")
    if not os.path.exists(SUDO_BIN):
        return (False, f"binario sudo non trovato in {SUDO_BIN}.")
    # 2) prova chiamata reale
    try:
        cp = _run(["status"], check=False)
    except UfwError as exc:
        return (False, f"errore esecuzione ufw: {exc}")
    except FileNotFoundError as exc:
        return (False, f"file non trovato: {exc}")
    if cp.returncode == 0:
        return (True, "")
    stderr = (cp.stderr or cp.stdout or "").strip()
    # Diagnose errore tipico
    low = stderr.lower()
    if "may not run sudo" in low or "is not allowed" in low or "a password is required" in low:
        return (False, f"sudoers non autorizza domarc-relay per ufw. "
                f"Verifica /etc/sudoers.d/domarc-relay-ufw. Errore: {stderr[:200]}")
    if "you need to be root" in low or "permission denied" in low:
        return (False, f"sudo non funziona (probabilmente NoNewPrivileges in systemd unit). "
                f"Errore: {stderr[:200]}")
    return (False, f"ufw exit={cp.returncode}: {stderr[:300]}")


def status_numbered() -> tuple[bool, list[UfwRule]]:
    cp = _run(["status", "numbered"])
    return _parse_status_numbered(cp.stdout)


def status_verbose() -> dict[str, Any]:
    cp = _run(["status", "verbose"], check=False)
    if cp.returncode != 0:
        return {"active": False, "raw": cp.stderr or cp.stdout}
    text = cp.stdout
    active = "active" in text.lower().split("\n")[0]
    logging_level = None
    default_policy = None
    for line in text.splitlines():
        s = line.strip().lower()
        if s.startswith("logging:"):
            logging_level = line.split(":", 1)[1].strip()
        elif s.startswith("default:"):
            default_policy = line.split(":", 1)[1].strip()
    return {
        "active": active,
        "logging": logging_level,
        "default": default_policy,
        "raw": text,
    }


# ============================================================== MUTATION ==

def add_rule(*, port: str | int, proto: str = "tcp",
              source: str | None = None, comment: str | None = None,
              actor: str = "ui") -> str:
    """Aggiunge una regola ALLOW. Ritorna l'output ufw."""
    p = _validate_port(str(port))
    pr = _validate_proto(proto)
    src = _validate_source(source)
    cm = _validate_comment(comment)
    args = ["allow"]
    if src:
        args += ["from", src, "to", "any", "port", str(p), "proto", pr]
    else:
        args += [f"{p}/{pr}"]
    if cm:
        args += ["comment", cm]
    cp = _run(args)
    logger.warning(
        "UFW add_rule by=%s port=%s/%s source=%s comment=%r → %s",
        actor, p, pr, src or "any", cm, cp.stdout.strip().splitlines()[-1] if cp.stdout else "",
    )
    return cp.stdout


def delete_rule_by_number(rule_number: int, *, actor: str = "ui") -> str:
    """Cancella regola per numero (più sicuro che parsare per match)."""
    n = _validate_rule_number(int(rule_number))
    cp = _run(["--force", "delete", str(n)])
    logger.warning("UFW delete_rule by=%s n=%d → %s",
                    actor, n, cp.stdout.strip().splitlines()[-1] if cp.stdout else "")
    return cp.stdout


def reload_ufw(*, actor: str = "ui") -> str:
    cp = _run(["reload"])
    logger.warning("UFW reload by=%s", actor)
    return cp.stdout


def enable_ufw(*, actor: str = "ui") -> str:
    cp = _run(["--force", "enable"])
    logger.warning("UFW enable by=%s", actor)
    return cp.stdout


def disable_ufw(*, actor: str = "ui") -> str:
    cp = _run(["disable"])
    logger.warning("UFW disable by=%s", actor)
    return cp.stdout
