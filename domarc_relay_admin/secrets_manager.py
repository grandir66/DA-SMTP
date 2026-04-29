"""Gestione chiavi API cifrate (Fernet) + injection in os.environ al boot.

Pattern:
- Master key Fernet salvata in ``/etc/domarc-smtp-relay-admin/master.key``
  (permessi 600, owner del servizio). Auto-generata al primo avvio se manca.
- Chiavi API cifrate in tabella ``api_keys`` (migration 013).
- All'avvio dell'app: :func:`load_secrets_into_env` decifra le chiavi
  abilitate e le inietta in ``os.environ`` PRIMA che i provider partano.
- Il pattern esistente ``os.environ.get(api_key_env, '')`` continua a
  funzionare invariato.

Sicurezza: se ``master.key`` viene perso/sostituito, le chiavi cifrate nel
DB diventano illegibili (fail-safe). L'operatore deve re-inserirle in UI.
"""
from __future__ import annotations

import logging
import os
import stat
from pathlib import Path
from typing import TYPE_CHECKING

from cryptography.fernet import Fernet, InvalidToken

if TYPE_CHECKING:
    from .storage.base import Storage

logger = logging.getLogger(__name__)

DEFAULT_MASTER_KEY_PATH = Path(
    os.environ.get("DOMARC_RELAY_MASTER_KEY_PATH",
                   "/var/lib/domarc-smtp-relay-admin/master.key")
)


class SecretsManager:
    """Gestisce cifratura/decifratura delle API key con Fernet."""

    def __init__(self, master_key_path: Path = DEFAULT_MASTER_KEY_PATH):
        self._path = master_key_path
        self._fernet: Fernet | None = None

    def _ensure_master_key(self) -> bytes:
        """Carica la master key da disco; se non esiste, la genera."""
        if self._path.exists():
            return self._path.read_bytes().strip()
        # Genera nuova chiave
        key = Fernet.generate_key()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_bytes(key)
        # Permessi 600 (owner only)
        try:
            self._path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        except OSError as exc:
            logger.warning("Impossibile chmod 600 su %s: %s", self._path, exc)
        logger.warning("Master key Fernet auto-generata in %s — proteggila e includila nei backup",
                       self._path)
        return key

    def _get_fernet(self) -> Fernet:
        if self._fernet is None:
            self._fernet = Fernet(self._ensure_master_key())
        return self._fernet

    def encrypt(self, plaintext: str) -> bytes:
        return self._get_fernet().encrypt(plaintext.encode("utf-8"))

    def decrypt(self, token: bytes) -> str:
        try:
            return self._get_fernet().decrypt(token).decode("utf-8")
        except InvalidToken as exc:
            raise ValueError(
                "Decifratura fallita: master.key non corrispondente. "
                "L'API key dovrà essere re-inserita."
            ) from exc

    def mask(self, plaintext: str) -> str:
        """Restituisce una versione mascherata per UI (es. ``sk-ant-...abcd``)."""
        s = (plaintext or "").strip()
        if len(s) <= 10:
            return "***"
        return f"{s[:8]}...{s[-4:]}"


_singleton: SecretsManager | None = None


def get_secrets_manager() -> SecretsManager:
    global _singleton
    if _singleton is None:
        _singleton = SecretsManager()
    return _singleton


def load_secrets_into_env(storage: "Storage") -> dict[str, int]:
    """Decifra tutte le ``api_keys`` abilitate e le inietta in ``os.environ``.

    Returns:
        dict ``{"loaded": N, "failed": M}`` per logging diagnostico.
    """
    sm = get_secrets_manager()
    loaded = 0
    failed = 0
    for row in storage.list_api_keys(only_enabled=True):
        env_var = row.get("env_var_name")
        if not env_var:
            continue
        try:
            value = sm.decrypt(row["value_encrypted"])
            os.environ[env_var] = value
            loaded += 1
        except ValueError as exc:
            logger.warning("API key '%s' (env=%s) non decifrabile: %s",
                           row.get("name"), env_var, exc)
            failed += 1
    if loaded:
        logger.info("SecretsManager: caricate %d API key in env (failed=%d)", loaded, failed)
    return {"loaded": loaded, "failed": failed}
