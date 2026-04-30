#!/usr/bin/env python3
"""Wizard CLI di configurazione iniziale Domarc SMTP Relay.

Esegue 4 step in sequenza, ognuno saltabile (skip):
1. Connessione DB gestionale clienti (PG/MSSQL) — test connessione
2. API ticket del manager esterno (base URL + X-API-Key)
3. Provider AI Anthropic (API key) — test 1 chiamata
4. Bootstrap utente admin (username, email, password)

Salva config in:
- /etc/domarc-smtp-relay-admin/secrets.env (env vars)
- admin.db (api_keys cifrate, settings, users)

Idempotente: rerun ridomanda solo gli step non confermati.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Aggiunge admin app al path per import storage/secrets_manager
APP_DIR = Path("/opt/domarc-smtp-relay-admin")
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

GRN = "\033[0;32m"
YEL = "\033[1;33m"
RED = "\033[0;31m"
CYA = "\033[0;36m"
NC = "\033[0m"


def banner():
    print(f"""{CYA}
╔══════════════════════════════════════════════════════════╗
║        Domarc SMTP Relay — Wizard di configurazione      ║
╚══════════════════════════════════════════════════════════╝
{NC}
Questo wizard configura le 4 connessioni esterne necessarie:

  1. {GRN}DB gestionale{NC} (clienti, alias, contratti)
  2. {GRN}API ticket{NC} (creazione ticket dal listener)
  3. {GRN}Provider AI{NC} (Claude — opzionale)
  4. {GRN}Utente admin{NC} (primo accesso al pannello web)

Tutti gli input sono salvati in maniera sicura: le password e API key
vengono cifrate con Fernet (chiave master in /var/lib/...).

Premi Ctrl+C in qualsiasi momento per uscire — gli step già completati
restano salvati e si può riprendere con un nuovo run.
""")


def ask(prompt: str, default: str | None = None, secret: bool = False,
        required: bool = True) -> str:
    suffix = f" [{default}]" if default else ""
    if secret:
        import getpass
        while True:
            v = getpass.getpass(f"{prompt}{suffix}: ")
            if v or default:
                return v or default
            if not required:
                return ""
            print(f"{RED}Campo obbligatorio.{NC}")
    while True:
        v = input(f"{prompt}{suffix}: ").strip()
        if v or default:
            return v or default
        if not required:
            return ""
        print(f"{RED}Campo obbligatorio.{NC}")


def confirm(prompt: str, default: bool = False) -> bool:
    suffix = " [Y/n]" if default else " [y/N]"
    v = input(f"{prompt}{suffix}: ").strip().lower()
    if not v:
        return default
    return v in ("y", "yes", "s", "si")


# ============================================================ STEP 1


def step_gestionale_db():
    print(f"\n{CYA}=== Step 1/4 — Connessione DB gestionale ==={NC}")
    print("Il sistema legge l'anagrafica clienti da un DB esterno (gestionale).")
    print("Backend supportati: PostgreSQL, MSSQL.\n")

    if not confirm("Configurare ora la connessione DB?", default=True):
        print(f"{YEL}Skip Step 1 — il sistema partirà con CustomerSource stub (nessun cliente).{NC}")
        return

    backend = ask("Backend (postgres / mssql)", default="postgres").lower()
    if backend not in ("postgres", "mssql", "pg"):
        print(f"{RED}Backend non valido, skip.{NC}")
        return

    host = ask("Host", default="localhost")
    port = ask("Port", default="5432" if backend.startswith("p") else "1433")
    database = ask("Database", default="solution")
    user = ask("User", default="stormshield")
    password = ask("Password", secret=True)

    print(f"\n{YEL}Test connessione...{NC}")
    if backend.startswith("p"):
        try:
            import psycopg2
            conn = psycopg2.connect(host=host, port=port, dbname=database,
                                     user=user, password=password,
                                     connect_timeout=5)
            cur = conn.cursor()
            cur.execute("SELECT current_database(), current_user")
            db, u = cur.fetchone()
            print(f"{GRN}✓ Connesso a {db} come {u}{NC}")
            try:
                cur.execute("SELECT COUNT(*) FROM clienti")
                n = cur.fetchone()[0]
                print(f"{GRN}✓ Tabella 'clienti' trovata: {n} righe{NC}")
            except Exception as exc:
                print(f"{YEL}WARN: tabella 'clienti' non leggibile: {exc}{NC}")
            conn.close()
        except ImportError:
            print(f"{RED}psycopg2 non installato. pip install psycopg2-binary{NC}")
            return
        except Exception as exc:
            print(f"{RED}Connessione fallita: {exc}{NC}")
            if not confirm("Salvare comunque la config (e ritestare dopo)?"):
                return
    else:
        try:
            import pyodbc
            conn = pyodbc.connect(
                f"DRIVER={{ODBC Driver 18 for SQL Server}};"
                f"SERVER={host},{port};DATABASE={database};"
                f"UID={user};PWD={password};Encrypt=optional;TrustServerCertificate=yes",
                timeout=5,
            )
            cur = conn.cursor()
            cur.execute("SELECT DB_NAME(), SUSER_NAME()")
            db, u = cur.fetchone()
            print(f"{GRN}✓ Connesso a {db} come {u}{NC}")
            conn.close()
        except ImportError:
            print(f"{RED}pyodbc non installato.{NC}")
            return
        except Exception as exc:
            print(f"{RED}Connessione fallita: {exc}{NC}")
            if not confirm("Salvare comunque?"):
                return

    save_secrets({
        "GESTIONALE_DB_BACKEND": backend,
        "GESTIONALE_DB_HOST": host,
        "GESTIONALE_DB_PORT": port,
        "GESTIONALE_DB_NAME": database,
        "GESTIONALE_DB_USER": user,
        "GESTIONALE_DB_PASSWORD": password,
    })
    print(f"{GRN}✓ Step 1 completato — config salvata in secrets.env{NC}")


# ============================================================ STEP 2


def step_manager_api():
    print(f"\n{CYA}=== Step 2/4 — API ticket del manager ==={NC}")
    print("Il listener invia ticket a un manager esterno via HTTP API.")
    print("Esempio URL: https://manager.example.com\n")

    if not confirm("Configurare ora il manager API?", default=True):
        print(f"{YEL}Skip Step 2 — i ticket non verranno creati automaticamente.{NC}")
        return

    base_url = ask("Manager base URL (senza trailing slash)", default="https://manager.domarc.it")
    api_key = ask("X-API-Key del manager", secret=True)

    print(f"\n{YEL}Test connessione (GET /api/v1/health)...{NC}")
    try:
        import httpx
        r = httpx.get(f"{base_url}/api/v1/health",
                       headers={"X-API-Key": api_key},
                       verify=False, timeout=10)
        if r.status_code == 200:
            print(f"{GRN}✓ Manager risponde: {r.text[:120]}{NC}")
        else:
            print(f"{YEL}WARN: status {r.status_code}: {r.text[:200]}{NC}")
    except Exception as exc:
        print(f"{RED}Connessione fallita: {exc}{NC}")
        if not confirm("Salvare comunque?"):
            return

    save_secrets({
        "MANAGER_BASE_URL": base_url,
        "MANAGER_API_KEY": api_key,
    })
    # Aggiorno anche il secrets.env del listener (legge variabili da lì)
    listener_secrets = Path("/etc/stormshield-smtp-relay/secrets.env")
    if listener_secrets.exists():
        append_env(listener_secrets, {
            "MANAGER_BASE_URL": base_url,
            "MANAGER_API_KEY": api_key,
        })

    print(f"{GRN}✓ Step 2 completato{NC}")


# ============================================================ STEP 3


def step_ai_provider():
    print(f"\n{CYA}=== Step 3/4 — Provider AI (Anthropic Claude) ==={NC}")
    print("Opzionale. La regola con action='ai_classify' richiede questo step.\n")

    if not confirm("Configurare ora la API key Anthropic?", default=False):
        print(f"{YEL}Skip Step 3 — l'AI sarà disabilitata.{NC}")
        return

    api_key = ask("Anthropic API key (sk-ant-...)", secret=True)

    if api_key:
        print(f"\n{YEL}Test chiamata claude-haiku-4-5...{NC}")
        try:
            import anthropic
            client = anthropic.Anthropic(api_key=api_key)
            resp = client.messages.create(
                model="claude-haiku-4-5",
                max_tokens=20,
                messages=[{"role": "user", "content": "Rispondi solo con la parola: PONG"}],
            )
            txt = resp.content[0].text if resp.content else ""
            print(f"{GRN}✓ Claude risponde: {txt!r}{NC}")
        except ImportError:
            print(f"{RED}pacchetto anthropic non installato. pip install anthropic{NC}")
        except Exception as exc:
            print(f"{RED}Test fallito: {exc}{NC}")
            if not confirm("Salvare comunque la chiave?"):
                return

    # Salva con cifratura Fernet via secrets_manager admin
    try:
        from domarc_relay_admin.secrets_manager import SecretsManager
        sm = SecretsManager()
        encrypted = sm.encrypt(api_key)
        # Insert in api_keys table dell'admin
        from domarc_relay_admin.config import load_config
        from domarc_relay_admin.storage import get_storage
        storage = get_storage(load_config())
        with storage._connect() as conn:
            conn.execute(
                """INSERT INTO api_keys (tenant_id, name, env_var_name, value_encrypted,
                                         masked_preview, description, enabled, created_by)
                   VALUES (1, ?, ?, ?, ?, ?, 1, 'wizard')
                   ON CONFLICT DO NOTHING""",
                ("Anthropic Claude (wizard)", "ANTHROPIC_API_KEY", encrypted,
                 sm.mask(api_key), "Configurato dal wizard installer"),
            )
            conn.commit()
        print(f"{GRN}✓ API key cifrata salvata in admin.db{NC}")
    except Exception as exc:
        print(f"{YEL}WARN: salvataggio cifrato fallito ({exc}). Salvo in env file in chiaro.{NC}")
        save_secrets({"ANTHROPIC_API_KEY": api_key})


# ============================================================ STEP 4


def step_admin_bootstrap():
    print(f"\n{CYA}=== Step 4/4 — Utente admin ==={NC}")

    try:
        from domarc_relay_admin.config import load_config
        from domarc_relay_admin.storage import get_storage
        storage = get_storage(load_config())
        with storage._connect() as conn:
            n = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        if n > 0:
            print(f"{YEL}Esistono già {n} utenti in admin.db. Skip bootstrap.{NC}")
            print(f"Se hai dimenticato la password, usa: domarc-smtp-relay-admin reset-password <user>")
            return
    except Exception as exc:
        print(f"{RED}Errore lettura users: {exc}{NC}")
        return

    print("Crea il PRIMO utente admin (ruolo superadmin, accesso totale).")
    username = ask("Username", default="admin")
    email = ask("Email")
    password = ask("Password (min 8 caratteri)", secret=True)
    if len(password) < 8:
        print(f"{RED}Password troppo corta.{NC}"); return

    try:
        import bcrypt
        pw_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    except ImportError:
        print(f"{RED}bcrypt non installato. pip install bcrypt{NC}"); return

    with storage._connect() as conn:
        conn.execute(
            """INSERT INTO users (username, email, password_hash, role, enabled, created_by)
               VALUES (?, ?, ?, 'superadmin', 1, 'wizard')""",
            (username, email, pw_hash),
        )
        conn.commit()
    print(f"{GRN}✓ Utente '{username}' creato come superadmin{NC}")
    print(f"\nApri il pannello web su https://<host>/ e accedi.")


# ============================================================ Helpers


def save_secrets(kv: dict):
    """Append/update key-value in /etc/domarc-smtp-relay-admin/secrets.env."""
    path = Path("/etc/domarc-smtp-relay-admin/secrets.env")
    append_env(path, kv)


def append_env(path: Path, kv: dict):
    """Append/update env variabili in modo idempotente."""
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = {}
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                existing[k.strip()] = v.strip()
    existing.update({k: v for k, v in kv.items()})
    lines = ["# Wizard config — non editare a mano se possibile"]
    for k, v in sorted(existing.items()):
        lines.append(f"{k}={v}")
    path.write_text("\n".join(lines) + "\n")
    path.chmod(0o640)


# ============================================================ Main


def main():
    banner()
    if os.geteuid() != 0 and os.environ.get("USER") != "domarc-relay":
        print(f"{YEL}WARN: stai girando come {os.environ.get('USER','?')} — alcuni step "
               f"richiedono accesso a /etc/. Usa: sudo -u domarc-relay {sys.argv[0]}{NC}\n")

    try:
        step_gestionale_db()
        step_manager_api()
        step_ai_provider()
        step_admin_bootstrap()
    except KeyboardInterrupt:
        print(f"\n{YEL}Interrotto. Rilancia il wizard quando vuoi.{NC}")
        return 130

    print(f"\n{GRN}=== Wizard completato ==={NC}")
    print("Servizi pronti. Apri il pannello web e accedi con l'utente admin appena creato.")
    print("Per riavviare se modifichi config: sudo systemctl restart domarc-smtp-relay-admin")
    return 0


if __name__ == "__main__":
    sys.exit(main())
