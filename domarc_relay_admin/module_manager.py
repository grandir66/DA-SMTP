"""Gestione moduli Python installabili da UI.

Whitelist hard-coded (NO arbitrary input dall'utente). Esecuzione tramite
subprocess `pip install <pkg>` con timeout, output capture, audit log.

Casi speciali (es. modello spaCy `it_core_news_sm`) gestiti via comandi
dedicati ``post_install_cmd``.
"""
from __future__ import annotations

import importlib.util
import logging
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .storage.base import Storage

logger = logging.getLogger(__name__)


# Whitelist dei moduli installabili dall'UI. Modificabile solo via codice.
# Ogni entry: code (chiave), package_name (per pip), import_check (per
# verificare se installato), description, optional, post_install_cmd (per
# casi tipo spaCy che richiedono download del modello dopo pip install),
# size_mb (stima per UI), required_for (lista di feature che lo richiedono).
MODULE_CATALOG: list[dict] = [
    {
        "code": "anthropic",
        "package": "anthropic",
        "import_check": "anthropic",
        "description": "SDK Anthropic Claude — provider IA principale.",
        "size_mb": 2,
        "required_for": ["AI Assistant — Claude provider"],
        "optional": False,
    },
    {
        "code": "spacy",
        "package": "spacy",
        "import_check": "spacy",
        "description": "Pipeline NLP italiano per PII redactor (NER nomi propri).",
        "size_mb": 50,
        "required_for": ["AI Assistant — PII redactor (NER)"],
        "optional": True,
    },
    {
        "code": "spacy_it_core_news_sm",
        "package": "it_core_news_sm",
        "import_check": "it_core_news_sm",
        "description": "Modello spaCy italiano (necessita 'spacy' già installato).",
        "size_mb": 13,
        "required_for": ["AI Assistant — PII redactor (NER nomi italiani)"],
        "optional": True,
        "depends_on": "spacy",
        "install_cmd": [sys.executable, "-m", "spacy", "download", "it_core_news_sm"],
    },
    {
        "code": "sentence_transformers",
        "package": "sentence-transformers",
        "import_check": "sentence_transformers",
        "description": "Embedding semantici multilingua per error clustering (F2).",
        "size_mb": 200,
        "required_for": ["AI Assistant — Error aggregator (F2)"],
        "optional": True,
    },
    {
        "code": "cryptography",
        "package": "cryptography",
        "import_check": "cryptography",
        "description": "Fernet encryption per API keys cifrate nel DB.",
        "size_mb": 5,
        "required_for": ["Settings — gestione API key cifrate"],
        "optional": False,
    },
]


def _is_installed(import_check: str) -> tuple[bool, str | None]:
    """Verifica se un modulo è importabile + ne legge la versione."""
    spec = importlib.util.find_spec(import_check)
    if spec is None:
        return False, None
    # Tenta di leggere la versione
    try:
        mod = importlib.import_module(import_check)
        version = getattr(mod, "__version__", None) or "unknown"
        return True, str(version)
    except Exception as exc:  # noqa: BLE001
        logger.debug("import_module(%s) failed: %s", import_check, exc)
        return True, "unknown"


def _resolve_pip_path() -> str:
    """Path al pip dell'venv corrente."""
    venv_pip = Path(sys.executable).parent / "pip"
    if venv_pip.exists():
        return str(venv_pip)
    pip_path = shutil.which("pip")
    if not pip_path:
        raise RuntimeError("pip non trovato nel PATH e nel venv")
    return pip_path


def list_modules_status() -> list[dict]:
    """Stato di tutti i moduli del catalogo."""
    out = []
    for entry in MODULE_CATALOG:
        installed, version = _is_installed(entry["import_check"])
        depends_on = entry.get("depends_on")
        depends_ok = True
        if depends_on:
            dep_entry = next((e for e in MODULE_CATALOG if e["code"] == depends_on), None)
            if dep_entry:
                depends_ok = _is_installed(dep_entry["import_check"])[0]
        out.append({
            **entry,
            "installed": installed,
            "version": version,
            "depends_satisfied": depends_ok,
        })
    return out


def install_module(code: str, *, storage: "Storage", actor: str | None = None,
                    timeout_sec: int = 600) -> dict:
    """Installa un modulo dalla whitelist.

    Returns:
        dict ``{ok, log_id, return_code, stdout_tail, stderr_tail, duration_ms}``.
    """
    entry = next((e for e in MODULE_CATALOG if e["code"] == code), None)
    if entry is None:
        return {"ok": False, "error": f"Modulo '{code}' non in whitelist"}

    log_id = storage.insert_module_install_log(
        module_code=code, operation="install", status="running",
        output=None, return_code=None, duration_ms=None, actor=actor,
    )

    # Costruisci comando: install_cmd se presente, altrimenti pip install package
    if "install_cmd" in entry:
        cmd = entry["install_cmd"]
    else:
        cmd = [_resolve_pip_path(), "install", entry["package"]]

    t0 = time.monotonic()
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout_sec,
        )
        duration_ms = int((time.monotonic() - t0) * 1000)
        # Tail dell'output (ultime ~100 righe per non saturare il DB)
        stdout_tail = "\n".join((proc.stdout or "").splitlines()[-100:])
        stderr_tail = "\n".join((proc.stderr or "").splitlines()[-100:])
        full_log = f"$ {' '.join(cmd)}\n\n--- STDOUT ---\n{stdout_tail}\n\n--- STDERR ---\n{stderr_tail}"
        ok = proc.returncode == 0
        storage.update_module_install_log(
            log_id, status="success" if ok else "failed",
            output=full_log[:10_000], return_code=proc.returncode,
            duration_ms=duration_ms,
        )
        return {
            "ok": ok, "log_id": log_id, "return_code": proc.returncode,
            "duration_ms": duration_ms,
            "stdout_tail": stdout_tail[-2000:],
            "stderr_tail": stderr_tail[-2000:],
        }
    except subprocess.TimeoutExpired as exc:
        duration_ms = int((time.monotonic() - t0) * 1000)
        storage.update_module_install_log(
            log_id, status="failed",
            output=f"TIMEOUT dopo {timeout_sec}s\n\n{exc}",
            return_code=-1, duration_ms=duration_ms,
        )
        return {"ok": False, "error": "timeout", "log_id": log_id,
                "duration_ms": duration_ms}
    except Exception as exc:  # noqa: BLE001
        duration_ms = int((time.monotonic() - t0) * 1000)
        storage.update_module_install_log(
            log_id, status="failed", output=f"Eccezione: {exc}",
            return_code=-1, duration_ms=duration_ms,
        )
        return {"ok": False, "error": str(exc), "log_id": log_id}


def uninstall_module(code: str, *, storage: "Storage",
                      actor: str | None = None,
                      timeout_sec: int = 120) -> dict:
    entry = next((e for e in MODULE_CATALOG if e["code"] == code), None)
    if entry is None:
        return {"ok": False, "error": f"Modulo '{code}' non in whitelist"}

    log_id = storage.insert_module_install_log(
        module_code=code, operation="uninstall", status="running",
        output=None, return_code=None, duration_ms=None, actor=actor,
    )
    cmd = [_resolve_pip_path(), "uninstall", "-y", entry["package"]]
    t0 = time.monotonic()
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_sec)
        duration_ms = int((time.monotonic() - t0) * 1000)
        full_log = f"$ {' '.join(cmd)}\n\n{proc.stdout}\n{proc.stderr}"
        ok = proc.returncode == 0
        storage.update_module_install_log(
            log_id, status="success" if ok else "failed",
            output=full_log[:10_000], return_code=proc.returncode,
            duration_ms=duration_ms,
        )
        return {"ok": ok, "log_id": log_id, "return_code": proc.returncode,
                "duration_ms": duration_ms}
    except Exception as exc:  # noqa: BLE001
        duration_ms = int((time.monotonic() - t0) * 1000)
        storage.update_module_install_log(
            log_id, status="failed", output=f"Eccezione: {exc}",
            return_code=-1, duration_ms=duration_ms,
        )
        return {"ok": False, "error": str(exc), "log_id": log_id}
