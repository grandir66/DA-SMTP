#!/usr/bin/env python3
"""Domarc SMTP Relay — Backup completo cifrato.

Crea un bundle .tar.gz cifrato AES-256 (Fernet con passphrase derivata via PBKDF2)
contenente:
  - admin.db (regole, gruppi, utenti, secrets cifrati, audit, eventi recenti)
  - relay.db (cache listener: events_log, outbound_queue, quarantine, dispatch)
  - master.key (Fernet key — SENZA QUESTA i secrets cifrati nel DB sono illegibili)
  - secrets.env (admin + listener)
  - file *.yaml in /etc/{domarc-,stormshield-}smtp-relay-*
  - nginx config + cert (self-signed only — Let's Encrypt si rigenera)
  - systemd unit files
  - manifest.json (metadata: hostname sorgente, data, versione, hash files)

USAGE:
    sudo ./domarc-backup.py --output /root/backup.tar.gz.enc --passphrase "..."
    # Oppure interattivo:
    sudo ./domarc-backup.py --output /root/backup.tar.gz.enc --interactive
"""
from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import io
import json
import os
import socket
import sys
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path

try:
    from cryptography.fernet import Fernet
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
except ImportError:
    print("ERROR: pip install cryptography", file=sys.stderr); sys.exit(1)


BACKUP_VERSION = "1"
PBKDF2_ITERATIONS = 600_000  # OWASP 2023+
SALT_BYTES = 16

# === File da includere nel bundle ===
INCLUDE_PATHS = [
    # Admin DB + master key
    ("/var/lib/domarc-smtp-relay-admin/admin.db",       "data/admin.db"),
    ("/var/lib/domarc-smtp-relay-admin/master.key",     "data/master.key"),
    # Listener DB
    ("/var/lib/stormshield-smtp-relay/relay.db",        "data/relay.db"),
    # Config
    ("/etc/domarc-smtp-relay-admin/secrets.env",        "etc/admin-secrets.env"),
    ("/etc/stormshield-smtp-relay/secrets.env",         "etc/listener-secrets.env"),
    ("/etc/stormshield-smtp-relay/relay.yaml",          "etc/relay.yaml"),
    # Nginx
    ("/etc/nginx/sites-available/domarc-relay",         "etc/nginx-domarc-relay.conf"),
    ("/etc/ssl/domarc-relay/fullchain.pem",             "etc/ssl-fullchain.pem"),
    ("/etc/ssl/domarc-relay/privkey.pem",               "etc/ssl-privkey.pem"),
    # Systemd units
    ("/etc/systemd/system/domarc-smtp-relay-admin.service",         "systemd/admin.service"),
    ("/etc/systemd/system/stormshield-smtp-relay-listener.service", "systemd/listener.service"),
    ("/etc/systemd/system/stormshield-smtp-relay-scheduler.service","systemd/scheduler.service"),
]


def _derive_key(passphrase: str, salt: bytes) -> bytes:
    """PBKDF2 → Fernet key (32 byte url-safe base64)."""
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=PBKDF2_ITERATIONS,
    )
    return base64.urlsafe_b64encode(kdf.derive(passphrase.encode("utf-8")))


def _hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def main():
    ap = argparse.ArgumentParser(description="Backup cifrato Domarc SMTP Relay")
    ap.add_argument("--output", required=True, help="Path bundle output (es. /root/backup.tar.gz.enc)")
    ap.add_argument("--passphrase", help="Passphrase (sconsigliato in CLI; usa --interactive)")
    ap.add_argument("--interactive", action="store_true", help="Chiedi passphrase via prompt")
    ap.add_argument("--include-bodies", action="store_true",
                     help="Includi body_text/html nel backup (default: ESCLUDI per ridurre size + privacy)")
    args = ap.parse_args()

    if os.geteuid() != 0:
        print("WARN: esegui come root per leggere tutti i file", file=sys.stderr)

    if args.interactive or not args.passphrase:
        passphrase = getpass.getpass("Passphrase per cifratura backup: ")
        if len(passphrase) < 12:
            print("ERROR: passphrase troppo corta (min 12 caratteri)", file=sys.stderr); sys.exit(1)
        confirm = getpass.getpass("Conferma passphrase: ")
        if passphrase != confirm:
            print("ERROR: passphrase non corrispondono", file=sys.stderr); sys.exit(1)
    else:
        passphrase = args.passphrase

    # === 1. Crea tar.gz in memoria con i files ===
    print("[backup] creazione bundle...")
    manifest = {
        "version": BACKUP_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "hostname": socket.getfqdn(),
        "include_bodies": args.include_bodies,
        "files": [],
    }

    tar_buf = io.BytesIO()
    with tarfile.open(fileobj=tar_buf, mode="w:gz") as tar:
        for src, arcname in INCLUDE_PATHS:
            p = Path(src)
            if not p.exists():
                print(f"  [skip] {src} (non esistente)")
                continue
            # Cap admin.db: opzionalmente svuota body_text/html prima del backup
            if not args.include_bodies and src.endswith("admin.db"):
                tmp = _strip_bodies(p)
                tar.add(tmp, arcname=arcname)
                manifest["files"].append({
                    "src": src, "arc": arcname,
                    "size": Path(tmp).stat().st_size,
                    "sha256": _hash_file(Path(tmp)),
                    "stripped_bodies": True,
                })
                Path(tmp).unlink()
            else:
                tar.add(p, arcname=arcname)
                manifest["files"].append({
                    "src": src, "arc": arcname,
                    "size": p.stat().st_size,
                    "sha256": _hash_file(p),
                    "stripped_bodies": False,
                })
                print(f"  [add ] {arcname} ({p.stat().st_size:,} byte)")

        # Manifest JSON
        manifest_data = json.dumps(manifest, indent=2).encode("utf-8")
        info = tarfile.TarInfo(name="manifest.json")
        info.size = len(manifest_data)
        info.mtime = int(datetime.now().timestamp())
        tar.addfile(info, io.BytesIO(manifest_data))

    plaintext = tar_buf.getvalue()
    print(f"[backup] bundle plaintext: {len(plaintext):,} byte ({len(plaintext)/1024/1024:.1f} MB)")

    # === 2. Cifra con Fernet ===
    salt = os.urandom(SALT_BYTES)
    key = _derive_key(passphrase, salt)
    f = Fernet(key)
    ciphertext = f.encrypt(plaintext)
    print(f"[backup] ciphertext: {len(ciphertext):,} byte")

    # === 3. Header file: magic + version + salt + ciphertext ===
    header = b"DOMARC1\n" + BACKUP_VERSION.encode() + b"\n" + salt
    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "wb") as f:
        f.write(header)
        f.write(ciphertext)
    out_path.chmod(0o600)

    print(f"\n[backup] ✓ Backup salvato in {out_path}")
    print(f"[backup]   size: {out_path.stat().st_size:,} byte")
    print(f"[backup]   files inclusi: {len(manifest['files'])}")
    print(f"[backup]   bodies inclusi: {args.include_bodies}")
    print("\n⚠ CUSTODIRE LA PASSPHRASE: senza, il backup è illeggibile.")


def _strip_bodies(admin_db: Path) -> str:
    """Crea copia di admin.db senza body_text/body_html negli eventi.

    Riduce dimensione + zero-PII nel backup. Il restore importa la copia.
    """
    import shutil, sqlite3
    fd, tmp = tempfile.mkstemp(suffix=".db", prefix="admin-backup-")
    os.close(fd)
    shutil.copy2(admin_db, tmp)
    conn = sqlite3.connect(tmp)
    try:
        conn.execute("UPDATE events SET body_text = NULL, body_html = NULL")
        conn.commit()
        conn.execute("VACUUM")
    finally:
        conn.close()
    return tmp


if __name__ == "__main__":
    main()
