#!/usr/bin/env python3
"""Domarc SMTP Relay — Restore da bundle cifrato.

Decifra un bundle creato da domarc-backup.py e ripristina i file nei loro
percorsi originali. Ferma i servizi prima del restore, riavvia dopo.

USAGE:
    sudo ./domarc-restore.py --input /root/backup.tar.gz.enc
    # Verrà chiesta la passphrase via prompt.

    sudo ./domarc-restore.py --input /root/backup.tar.gz.enc --dry-run
    # Mostra cosa verrebbe ripristinato senza scrivere.

    sudo ./domarc-restore.py --input /root/backup.tar.gz.enc --skip-services
    # Non riavvia i servizi (utile in pre-cutover).
"""
from __future__ import annotations

import argparse
import base64
import getpass
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from datetime import datetime
from pathlib import Path

try:
    from cryptography.fernet import Fernet, InvalidToken
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
except ImportError:
    print("ERROR: pip install cryptography", file=sys.stderr); sys.exit(1)


PBKDF2_ITERATIONS = 600_000
SALT_BYTES = 16

RESTORE_PATHS = {
    # arc → dest
    "data/admin.db":             ("/var/lib/domarc-smtp-relay-admin/admin.db",       "domarc-relay:domarc-relay"),
    "data/master.key":           ("/var/lib/domarc-smtp-relay-admin/master.key",     "domarc-relay:domarc-relay"),
    "data/relay.db":             ("/var/lib/stormshield-smtp-relay/relay.db",        "stormshield-relay:stormshield-relay"),
    "etc/admin-secrets.env":     ("/etc/domarc-smtp-relay-admin/secrets.env",        "root:domarc-relay"),
    "etc/listener-secrets.env":  ("/etc/stormshield-smtp-relay/secrets.env",         "root:stormshield-relay"),
    "etc/relay.yaml":            ("/etc/stormshield-smtp-relay/relay.yaml",          "root:root"),
    "etc/nginx-domarc-relay.conf": ("/etc/nginx/sites-available/domarc-relay",       "root:root"),
    "etc/ssl-fullchain.pem":     ("/etc/ssl/domarc-relay/fullchain.pem",             "root:root"),
    "etc/ssl-privkey.pem":       ("/etc/ssl/domarc-relay/privkey.pem",               "root:root"),
    "systemd/admin.service":     ("/etc/systemd/system/domarc-smtp-relay-admin.service",          "root:root"),
    "systemd/listener.service":  ("/etc/systemd/system/stormshield-smtp-relay-listener.service", "root:root"),
    "systemd/scheduler.service": ("/etc/systemd/system/stormshield-smtp-relay-scheduler.service","root:root"),
}


def _derive_key(passphrase: str, salt: bytes) -> bytes:
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt,
                     iterations=PBKDF2_ITERATIONS)
    return base64.urlsafe_b64encode(kdf.derive(passphrase.encode("utf-8")))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True, help="Path bundle cifrato")
    ap.add_argument("--passphrase", help="Sconsigliato in CLI")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--skip-services", action="store_true",
                     help="Non fermare/riavviare i servizi systemd")
    ap.add_argument("--force", action="store_true",
                     help="Sovrascrivi senza chiedere conferma")
    args = ap.parse_args()

    if os.geteuid() != 0 and not args.dry_run:
        print("ERROR: esegui come root per scrivere /etc/, /var/lib/", file=sys.stderr)
        sys.exit(1)

    in_path = Path(args.input)
    if not in_path.exists():
        print(f"ERROR: {in_path} non esiste", file=sys.stderr); sys.exit(1)

    # === 1. Lettura header + ciphertext ===
    with open(in_path, "rb") as f:
        magic = f.readline().strip()
        if magic != b"DOMARC1":
            print(f"ERROR: magic non riconosciuto: {magic!r}", file=sys.stderr); sys.exit(1)
        version = f.readline().strip().decode()
        salt = f.read(SALT_BYTES)
        ciphertext = f.read()
    print(f"[restore] bundle version={version}, salt={salt.hex()[:16]}..., ciphertext {len(ciphertext):,} byte")

    # === 2. Passphrase + decrypt ===
    passphrase = args.passphrase or getpass.getpass("Passphrase: ")
    key = _derive_key(passphrase, salt)
    try:
        plaintext = Fernet(key).decrypt(ciphertext)
    except InvalidToken:
        print("ERROR: passphrase errata o file corrotto", file=sys.stderr); sys.exit(1)
    print(f"[restore] decifrato OK ({len(plaintext):,} byte)")

    # === 3. Estrai tar in tmpdir ===
    tmpdir = Path(tempfile.mkdtemp(prefix="domarc-restore-"))
    try:
        with tarfile.open(fileobj=io.BytesIO(plaintext), mode="r:gz") as tar:
            tar.extractall(tmpdir)

        # Manifest
        manifest = json.loads((tmpdir / "manifest.json").read_text())
        print(f"[restore] manifest: {manifest['hostname']} @ {manifest['created_at']}")
        print(f"[restore] files inclusi: {len(manifest['files'])}")

        if not args.force and not args.dry_run:
            print("\n=== I file seguenti verranno SOVRASCRITTI ===")
            for arc, (dest, owner) in RESTORE_PATHS.items():
                src = tmpdir / arc
                if src.exists() and Path(dest).exists():
                    print(f"  ⚠ {dest} (esistente)")
                elif src.exists():
                    print(f"  + {dest} (nuovo)")
            ans = input("\nProcedere? [y/N]: ").strip().lower()
            if ans not in ("y", "yes", "s", "si"):
                print("Annullato."); sys.exit(0)

        # === 4. Stop servizi ===
        services = ["domarc-smtp-relay-admin",
                    "stormshield-smtp-relay-scheduler",
                    "stormshield-smtp-relay-listener"]
        if not args.skip_services and not args.dry_run:
            for s in services:
                subprocess.run(["systemctl", "stop", s], check=False)
                print(f"[restore] stopped: {s}")

        # === 5. Restore files ===
        for arc, (dest, owner) in RESTORE_PATHS.items():
            src = tmpdir / arc
            if not src.exists():
                continue
            dest_path = Path(dest)
            if args.dry_run:
                print(f"  [DRY] {arc} → {dest} (chown {owner})")
                continue
            # Backup file precedente con suffix .bak.timestamp
            if dest_path.exists():
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                bak = dest_path.with_suffix(dest_path.suffix + f".bak.{ts}")
                shutil.move(dest_path, bak)
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest_path)
            try:
                subprocess.run(["chown", owner, str(dest_path)], check=False)
            except Exception:
                pass
            print(f"  ✓ {arc} → {dest}")

        # === 6. systemd reload + nginx test + restart ===
        if not args.dry_run and not args.skip_services:
            subprocess.run(["systemctl", "daemon-reload"], check=False)
            r = subprocess.run(["nginx", "-t"], capture_output=True)
            if r.returncode != 0:
                print(f"[restore] WARN: nginx -t failed: {r.stderr.decode()}", file=sys.stderr)
            else:
                subprocess.run(["systemctl", "reload", "nginx"], check=False)
            for s in services:
                subprocess.run(["systemctl", "start", s], check=False)
                print(f"[restore] started: {s}")

        print(f"\n[restore] ✓ Restore completato.")
        if args.dry_run:
            print("[restore] (dry-run: nessuna scrittura effettuata)")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    main()
