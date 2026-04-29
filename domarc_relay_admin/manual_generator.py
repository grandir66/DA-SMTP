"""Generatore automatico di ``docs/manual.md``.

Ispeziona il codice (migrations, blueprint Flask, settings, jobs IA, action regole)
e produce un manuale tecnico/funzionale aggiornato. Eseguito:

- All'avvio dell'app (in :func:`create_app` con ``init_db=True``).
- Su comando CLI ``domarc-smtp-relay-admin manual``.
- Quando si applica una migration nuova.

Il file generato è in ``docs/manual.md`` (rinominato per chiarezza:
``manual.md`` è il manuale auto, le altre guide narrative restano in
``docs/guida_funzionamento.md`` ecc.).
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from flask import Flask

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
# Il manual è un file runtime (auto-generato): lo scriviamo in una directory
# write-able dal servizio (override via env DOMARC_RELAY_MANUAL_PATH per test).
import os as _os
MANUAL_PATH = Path(
    _os.environ.get(
        "DOMARC_RELAY_MANUAL_PATH",
        "/var/lib/domarc-smtp-relay-admin/manual.md",
    )
)
MIGRATIONS_DIR = REPO_ROOT / "domarc_relay_admin" / "migrations"


def _read_version() -> str:
    from . import __version__
    return __version__


def _list_migrations() -> list[dict]:
    """Estrae versione + descrizione (prima riga commento) da ogni migration."""
    out: list[dict] = []
    for f in sorted(MIGRATIONS_DIR.glob("*.sqlite.sql")):
        try:
            ver = int(f.name.split("_", 1)[0])
        except ValueError:
            continue
        text = f.read_text(encoding="utf-8")
        # Cerca prima riga di descrizione (-- xxx — descrizione...)
        m = re.search(r"^--\s*Migration\s+\d+\s*[—\-]\s*(.+?)\.?\s*$", text, re.MULTILINE)
        desc = m.group(1).strip() if m else f.name
        # Estrae tabelle CREATE / ALTER TABLE
        tables = sorted(set(
            re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", text)
            + re.findall(r"ALTER TABLE (\w+) ADD COLUMN", text)
        ))
        out.append({
            "version": ver, "filename": f.name,
            "description": desc, "tables": tables,
        })
    return out


def _list_blueprints(app: "Flask") -> list[dict]:
    """Per ogni blueprint Flask: nome, prefix, lista endpoint con doc."""
    bps: list[dict] = []
    seen = set()
    for endpoint, view_func in app.view_functions.items():
        if "." in endpoint:
            bp_name = endpoint.split(".", 1)[0]
        else:
            bp_name = "(root)"
        if bp_name in seen:
            continue
        seen.add(bp_name)
        endpoints = []
        for ep, vf in app.view_functions.items():
            if ep.split(".")[0] != bp_name:
                continue
            doc = (vf.__doc__ or "").strip().split("\n")[0]
            # Trova rule(s) per questo endpoint
            rules = [
                {"path": str(r), "methods": sorted(r.methods - {"HEAD", "OPTIONS"})}
                for r in app.url_map.iter_rules() if r.endpoint == ep
            ]
            endpoints.append({
                "endpoint": ep, "doc": doc[:200], "rules": rules,
            })
        endpoints.sort(key=lambda e: e["endpoint"])
        bps.append({
            "name": bp_name,
            "endpoint_count": len(endpoints),
            "endpoints": endpoints,
        })
    bps.sort(key=lambda b: b["name"])
    return bps


def _list_settings(app: "Flask") -> list[dict]:
    """Settings table: chiavi + descrizioni."""
    storage = app.extensions.get("domarc_storage")
    if not storage:
        return []
    try:
        rows = storage.list_settings()
    except Exception:  # noqa: BLE001
        return []
    return sorted(rows, key=lambda r: r.get("key", ""))


def _list_ai_jobs(app: "Flask") -> list[dict]:
    storage = app.extensions.get("domarc_storage")
    if not storage:
        return []
    try:
        return sorted(storage.list_ai_jobs(), key=lambda r: r.get("job_code", ""))
    except (AttributeError, NotImplementedError):
        return []


def _list_module_catalog() -> list[dict]:
    """Catalogo whitelist moduli installabili."""
    try:
        from .module_manager import MODULE_CATALOG
        return [{
            "code": m["code"], "package": m["package"],
            "description": m["description"],
            "size_mb": m["size_mb"], "optional": m.get("optional", False),
        } for m in MODULE_CATALOG]
    except Exception:  # noqa: BLE001
        return []


def _supported_actions() -> list[dict]:
    """Action supportate dalle regole (cablate nel listener pipeline)."""
    return [
        {"code": "ignore", "desc": "Scarta la mail, niente recapito né ticket."},
        {"code": "flag_only", "desc": "Logga + flag, no azione attiva."},
        {"code": "auto_reply", "desc": "Invia auto-reply al mittente con template."},
        {"code": "create_ticket", "desc": "Crea ticket in coda assistenza."},
        {"code": "forward", "desc": "Forward verso smarthost custom."},
        {"code": "redirect", "desc": "Redirect a indirizzo alternativo."},
        {"code": "quarantine", "desc": "Mette la mail in quarantena per review."},
        {"code": "ai_classify", "desc": "Invoca IA per classificazione semantica + suggested_action."},
        {"code": "ai_critical_check", "desc": "Pre-check IA con fail-safe forward su outage."},
    ]


def _validators_summary() -> list[dict]:
    """Validatori rule engine v2 (V001-V008 + warnings)."""
    return [
        {"id": "V001", "msg": "Un gruppo non può avere padre."},
        {"id": "V002", "msg": "Il padre referenziato deve essere un gruppo."},
        {"id": "V003", "msg": "I gruppi non eseguono azioni dirette."},
        {"id": "V004", "msg": "Un gruppo deve avere almeno un match_*."},
        {"id": "V005", "msg": "Niente riferimenti circolari."},
        {"id": "V006", "msg": "Match incompatibili padre/figlio."},
        {"id": "V007", "msg": "Priority fuori range 1..999999."},
        {"id": "V008", "msg": "Un gruppo non può essere figlio."},
        {"id": "V_PRI_RANGE", "msg": "Priority figlio strettamente fra padre e prossimo top-level."},
        {"id": "W001", "msg": "Gruppo senza figli."},
        {"id": "W002", "msg": "Figlio senza match_* propri."},
        {"id": "W004", "msg": "Match ridondante padre/figlio."},
        {"id": "W005", "msg": "Gruppo non-exclusive con ultimo figlio STOP totale."},
        {"id": "W_PRI_GAP", "msg": "Distanza minima fra fratelli < 5."},
    ]


def render_manual(app: "Flask") -> str:
    """Costruisce il contenuto Markdown del manuale."""
    version = _read_version()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    migrations = _list_migrations()
    blueprints = _list_blueprints(app)
    settings = _list_settings(app)
    ai_jobs = _list_ai_jobs(app)
    modules = _list_module_catalog()
    actions = _supported_actions()
    validators = _validators_summary()

    out: list[str] = []
    out.append(f"# Domarc SMTP Relay — Manuale tecnico v{version}")
    out.append("")
    out.append(f"_Generato automaticamente il {now} dal modulo "
               f"[`manual_generator`](../domarc_relay_admin/manual_generator.py)._  ")
    out.append(f"_Aggiornato ad ogni avvio dell'admin web e ad ogni nuova migration. NON modificare a mano: le edit verranno sovrascritte._")
    out.append("")
    out.append("---")
    out.append("")

    # =================================================================
    # 1. Architettura
    # =================================================================
    out.append("## 1. Architettura sintetica")
    out.append("")
    out.append("Il sistema è composto da **due processi** indipendenti:")
    out.append("")
    out.append("1. **Listener SMTP** (`/opt/stormshield-smtp-relay/`, servizio `stormshield-smtp-relay-listener.service`, porta 25): riceve le mail, applica il rule engine deterministico (Rule Engine v2 con gerarchia padre/figlio), esegue le azioni concrete o invoca l'admin per inferenze IA.")
    out.append("2. **Admin Web** (questo progetto, servizio `domarc-smtp-relay-admin.service`, porta 5443 dietro nginx :8443): UI di configurazione + DAO + API verso il listener (sync regole/customers/privacy bypass/ai-bindings + endpoint inferenza IA inline).")
    out.append("")
    out.append("Il listener cache-a tutto in locale tramite `relay/sync.py` (sync periodico). Le decisioni IA vengono richieste sync via HTTP `POST /api/v1/relay/ai/classify`.")
    out.append("")

    # =================================================================
    # 2. Migrations DB
    # =================================================================
    out.append("## 2. Schema database — storia migrations")
    out.append("")
    out.append("| # | File | Descrizione | Tabelle toccate |")
    out.append("|---|---|---|---|")
    for m in migrations:
        tables = ", ".join(f"`{t}`" for t in m["tables"][:6])
        if len(m["tables"]) > 6:
            tables += ", …"
        out.append(f"| {m['version']:03d} | `{m['filename']}` | {m['description']} | {tables} |")
    out.append("")
    out.append(f"_Schema version corrente: **{max((m['version'] for m in migrations), default=0)}**_")
    out.append("")

    # =================================================================
    # 3. UI routes
    # =================================================================
    out.append("## 3. Blueprint UI / route admin")
    out.append("")
    for bp in blueprints:
        if bp["name"] in ("static",):
            continue
        out.append(f"### `{bp['name']}` ({bp['endpoint_count']} endpoint)")
        out.append("")
        out.append("| Path | Methods | Endpoint | Doc |")
        out.append("|---|---|---|---|")
        for ep in bp["endpoints"]:
            for r in ep["rules"]:
                methods = ", ".join(r["methods"]) or "GET"
                doc = (ep["doc"] or "").replace("|", "\\|")[:120]
                out.append(f"| `{r['path']}` | {methods} | `{ep['endpoint']}` | {doc} |")
        out.append("")

    # =================================================================
    # 4. Settings
    # =================================================================
    out.append("## 4. Settings runtime")
    out.append("")
    if settings:
        out.append("| Chiave | Valore corrente | Descrizione |")
        out.append("|---|---|---|")
        for s in settings:
            v = str(s.get("value", "") or "")
            if len(v) > 60:
                v = v[:60] + "…"
            desc = (s.get("description") or "—").replace("|", "\\|")[:200]
            out.append(f"| `{s['key']}` | `{v}` | {desc} |")
    else:
        out.append("_(nessun setting rilevato)_")
    out.append("")

    # =================================================================
    # 5. Action regole
    # =================================================================
    out.append("## 5. Action regole supportate")
    out.append("")
    out.append("| Action | Descrizione |")
    out.append("|---|---|")
    for a in actions:
        out.append(f"| `{a['code']}` | {a['desc']} |")
    out.append("")

    # =================================================================
    # 6. Validatori regole
    # =================================================================
    out.append("## 6. Validatori Rule Engine v2")
    out.append("")
    out.append("**Errori bloccanti**:")
    out.append("")
    for v in validators:
        if v["id"].startswith("V"):
            out.append(f"- **{v['id']}** — {v['msg']}")
    out.append("")
    out.append("**Warning soft**:")
    out.append("")
    for v in validators:
        if v["id"].startswith("W"):
            out.append(f"- **{v['id']}** — {v['msg']}")
    out.append("")

    # =================================================================
    # 7. AI Assistant
    # =================================================================
    out.append("## 7. AI Assistant — job catalog")
    out.append("")
    if ai_jobs:
        out.append("| Job code | Descrizione | Modality | Timeout default |")
        out.append("|---|---|---|---|")
        for j in ai_jobs:
            out.append(f"| `{j['job_code']}` | {j['description']} | {j['modality']} | {j['default_timeout_ms']}ms |")
    else:
        out.append("_(catalogo AI non disponibile — schema < v12)_")
    out.append("")

    # =================================================================
    # 8. Moduli Python installabili
    # =================================================================
    out.append("## 8. Moduli Python installabili dall'UI")
    out.append("")
    if modules:
        out.append("| Code | Pacchetto | Descrizione | Size (~MB) | Opzionale |")
        out.append("|---|---|---|---|---|")
        for m in modules:
            opt = "✓" if m.get("optional") else "—"
            out.append(f"| `{m['code']}` | `{m['package']}` | {m['description']} | {m['size_mb']} | {opt} |")
    else:
        out.append("_(catalogo moduli non disponibile)_")
    out.append("")

    # =================================================================
    # 9. Path di sistema
    # =================================================================
    out.append("## 9. Path di sistema")
    out.append("")
    out.append("| Risorsa | Path |")
    out.append("|---|---|")
    out.append("| Codice admin | `/opt/domarc-smtp-relay-admin/` |")
    out.append("| Codice listener | `/opt/stormshield-smtp-relay/` |")
    out.append("| DB admin (SQLite) | `/var/lib/domarc-smtp-relay-admin/admin.db` |")
    out.append("| Master key Fernet | `/var/lib/domarc-smtp-relay-admin/master.key` (600 owner-only) |")
    out.append("| Backup automatici | `/opt/domarc-smtp-relay-admin/backups/` |")
    out.append("| Log servizio | `journalctl -u domarc-smtp-relay-admin` |")
    out.append("| Config systemd | `/etc/systemd/system/domarc-smtp-relay-admin.service` |")
    out.append("| Env vars (secrets) | `/etc/domarc-smtp-relay-admin/secrets.env` |")
    out.append("")
    out.append("---")
    out.append("")
    out.append(f"_Fine manuale auto-generato — versione documento allineata al software v{version}._")
    out.append("")
    return "\n".join(out)


def write_manual(app: "Flask") -> Path:
    """Scrive il manuale su disco e ritorna il path."""
    content = render_manual(app)
    MANUAL_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        MANUAL_PATH.write_text(content, encoding="utf-8")
        logger.info("Manual.md rigenerato (%d bytes) in %s", len(content), MANUAL_PATH)
    except OSError as exc:
        # In ambienti read-only (systemd ProtectSystem=strict senza .venv writable
        # esteso a docs/) il write può fallire. Non bloccare l'avvio.
        logger.warning("Impossibile scrivere manual.md: %s — ignorato", exc)
    return MANUAL_PATH


def read_manual() -> str:
    """Legge il manuale dal disco. Se non esiste, ritorna placeholder."""
    if MANUAL_PATH.exists():
        return MANUAL_PATH.read_text(encoding="utf-8")
    return (
        "# Manuale\n\n"
        "Il manuale non è stato ancora generato. "
        "Riavvia l'admin web oppure esegui `domarc-smtp-relay-admin manual`.\n"
    )
