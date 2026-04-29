# Operations — procedure operative Domarc SMTP Relay Admin

Manuale operativo per chi gestisce l'infrastruttura. Per la documentazione
funzionale vedi [guida_funzionamento.md](guida_funzionamento.md), per il
modulo IA vedi [ai_assistant.md](ai_assistant.md).

## Indice

1. [Backup e restore](#backup-e-restore)
2. [Master key rotation](#master-key-rotation)
3. [Rollback migration](#rollback-migration)
4. [Path di sistema](#path-di-sistema)
5. [Log e troubleshooting](#log-e-troubleshooting)
6. [Troubleshooting delivery](#troubleshooting-delivery)
7. [Permission matrix per ruolo](#permission-matrix-per-ruolo)
8. [Procedura aggiornamento versione](#procedura-aggiornamento-versione)

---

## 1. Backup e restore

### Cosa va in backup (obbligatorio)

| Risorsa | Path | Note |
|---|---|---|
| **DB SQLite** | `/var/lib/domarc-smtp-relay-admin/admin.db` | Tutti i dati: tenants, regole, decisioni IA, ecc. |
| **Master key Fernet** | `/var/lib/domarc-smtp-relay-admin/master.key` | **Senza la master key le API key cifrate diventano illegibili.** |
| **Secrets env** | `/etc/domarc-smtp-relay-admin/secrets.env` | Configurazione runtime (NON committare in git). |
| **Systemd unit** | `/etc/systemd/system/domarc-smtp-relay-admin.service` | Solo se modificata. |

### Cosa NON va in backup (rigenerabile)

- `.venv/` — ricreabile con `pip install -e .`
- `docs/manual.md` — rigenerato all'avvio dell'admin
- `backups/` — non backup-are i backup
- `__pycache__/`, `*.pyc`

### Script backup giornaliero

```bash
#!/bin/bash
# /usr/local/sbin/domarc-relay-backup.sh
set -e
DEST=/var/backups/domarc-smtp-relay-admin
DATE=$(date +%Y-%m-%d)
mkdir -p "$DEST/$DATE"

# DB con .backup (lock-aware, evita corruzione su DB attivo)
sqlite3 /var/lib/domarc-smtp-relay-admin/admin.db ".backup '$DEST/$DATE/admin.db'"

# Master key (DEVE essere insieme al DB)
cp /var/lib/domarc-smtp-relay-admin/master.key "$DEST/$DATE/master.key"
chmod 600 "$DEST/$DATE/master.key"

# Secrets env
cp /etc/domarc-smtp-relay-admin/secrets.env "$DEST/$DATE/secrets.env"
chmod 600 "$DEST/$DATE/secrets.env"

# Systemd unit
cp /etc/systemd/system/domarc-smtp-relay-admin.service "$DEST/$DATE/"

# Tar + cleanup vecchi backup (>30gg)
tar czf "$DEST/admin-$DATE.tgz" -C "$DEST" "$DATE"
rm -rf "$DEST/$DATE"
find "$DEST" -name "admin-*.tgz" -mtime +30 -delete
```

Schedula in cron: `0 2 * * * /usr/local/sbin/domarc-relay-backup.sh`.

### Restore

```bash
# 1. Stop servizio
systemctl stop domarc-smtp-relay-admin

# 2. Restore (assumendo tar in /tmp)
tar xzf /tmp/admin-2026-04-29.tgz -C /tmp
cp /tmp/2026-04-29/admin.db /var/lib/domarc-smtp-relay-admin/
cp /tmp/2026-04-29/master.key /var/lib/domarc-smtp-relay-admin/
chmod 600 /var/lib/domarc-smtp-relay-admin/master.key
chown -R domarc-relay:domarc-relay /var/lib/domarc-smtp-relay-admin/

# 3. Start
systemctl start domarc-smtp-relay-admin
journalctl -u domarc-smtp-relay-admin -f
# Cerca: "SecretsManager: caricate N API key in env (failed=0)"
# Se failed > 0 → master.key non corrispondente al DB
```

---

## 2. Master key rotation

### Quando ruotare

- Sospetto compromissione del file `master.key`
- Cambio operatore con accesso filesystem
- Policy aziendale (es. ogni 12 mesi)

### Procedura

⚠️ **Operazione delicata**. Pianificare downtime breve (< 5 min).

```bash
# 1. Backup completo prima della rotazione
/usr/local/sbin/domarc-relay-backup.sh

# 2. Stop servizio
systemctl stop domarc-smtp-relay-admin

# 3. Genera nuova master key
NEW_KEY=$(/opt/domarc-smtp-relay-admin/.venv/bin/python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")

# 4. Decifra TUTTE le api_keys con la vecchia chiave + ricifra con la nuova
/opt/domarc-smtp-relay-admin/.venv/bin/python <<EOF
import sqlite3
from cryptography.fernet import Fernet
OLD = open('/var/lib/domarc-smtp-relay-admin/master.key', 'rb').read().strip()
NEW = b'${NEW_KEY}'
f_old = Fernet(OLD); f_new = Fernet(NEW)
c = sqlite3.connect('/var/lib/domarc-smtp-relay-admin/admin.db')
for row in c.execute('SELECT id, value_encrypted FROM api_keys').fetchall():
    plain = f_old.decrypt(row[1])
    c.execute('UPDATE api_keys SET value_encrypted = ? WHERE id = ?',
              (f_new.encrypt(plain), row[0]))
c.commit()
print(f'Re-cifrate {c.total_changes} chiavi.')
EOF

# 5. Sostituisci master.key
echo "$NEW_KEY" > /var/lib/domarc-smtp-relay-admin/master.key
chmod 600 /var/lib/domarc-smtp-relay-admin/master.key
chown domarc-relay:domarc-relay /var/lib/domarc-smtp-relay-admin/master.key

# 6. Start + verifica
systemctl start domarc-smtp-relay-admin
journalctl -u domarc-smtp-relay-admin -n 5 | grep secrets
# Atteso: "SecretsManager: caricate N API key in env (failed=0)"
```

Se `failed > 0`: ripristinare la **vecchia** master.key dal backup, non perdere mai entrambe.

---

## 3. Rollback migration

### Quando

- Migration ha rotto qualcosa in produzione
- Test di una nuova feature da annullare

### Procedura

```bash
# 1. Stop
systemctl stop domarc-smtp-relay-admin

# 2. Restore DB pre-migration (i backup sono in /opt/domarc-smtp-relay-admin/backups/)
ls -lt /opt/domarc-smtp-relay-admin/backups/admin.db.pre-* | head -3
cp /opt/domarc-smtp-relay-admin/backups/admin.db.pre-XXX-YYYYMMDD-HHMMSS /var/lib/domarc-smtp-relay-admin/admin.db
chown domarc-relay:domarc-relay /var/lib/domarc-smtp-relay-admin/admin.db

# 3. Se serve rollback codice
cd /opt/domarc-smtp-relay-admin && git log --oneline | head -5
git checkout <commit-precedente>

# 4. Start
systemctl start domarc-smtp-relay-admin
```

I backup pre-migration sono creati automaticamente con nome
`admin.db.pre-<feature>-YYYYMMDD-HHMMSS`.

---

## 4. Path di sistema

| Risorsa | Path | Permessi |
|---|---|---|
| Codice admin | `/opt/domarc-smtp-relay-admin/` | root:root 755 |
| Venv | `/opt/domarc-smtp-relay-admin/.venv/` | domarc-relay:domarc-relay (writable per `pip install`) |
| DB SQLite | `/var/lib/domarc-smtp-relay-admin/admin.db` | domarc-relay:domarc-relay 600 |
| Master key | `/var/lib/domarc-smtp-relay-admin/master.key` | domarc-relay:domarc-relay 600 |
| Manual auto | `/var/lib/domarc-smtp-relay-admin/manual.md` | domarc-relay:domarc-relay 644 |
| Secrets env | `/etc/domarc-smtp-relay-admin/secrets.env` | root:domarc-relay 640 |
| Backups | `/opt/domarc-smtp-relay-admin/backups/` | domarc-relay:domarc-relay 644 |
| Systemd unit | `/etc/systemd/system/domarc-smtp-relay-admin.service` | root:root 644 |
| Listener (separato) | `/opt/stormshield-smtp-relay/` | stormshield-relay |

---

## 5. Log e troubleshooting

### Log primari

```bash
# Admin web (Flask + storage + AI + ecc.)
journalctl -u domarc-smtp-relay-admin -f

# Listener SMTP (ricezione mail su :25)
journalctl -u stormshield-smtp-relay-listener -f

# Scheduler (sync periodico + flush eventi + flush outbound)
journalctl -u stormshield-smtp-relay-scheduler -f
```

### Problemi tipici

| Sintomo | Causa probabile | Fix |
|---|---|---|
| Login fallisce con errore generico | bcrypt non installato o DB corrotto | Verifica `pip list \| grep bcrypt` + `sqlite3 admin.db ".schema users"` |
| API key non funzionano post-restart | Master.key persa o sostituita | Restore master.key dal backup, oppure re-inserisci API keys da UI |
| Pip install da UI fallisce con "Read-only file system" | Systemd `ProtectSystem=strict` blocca venv | Verifica `systemctl cat domarc-smtp-relay-admin \| grep ReadWritePaths` includa `/opt/domarc-smtp-relay-admin/.venv` |
| Test stack mostra Claude FAIL | API key invalida o rate limit | Settings → Chiavi API → verifica/ruota la chiave + test connettività |
| Health check segna disk = error | < 5% spazio libero su `/var/lib/` | Pulisci backup vecchi, ruota log, espandi disco |

---

## 6. Troubleshooting delivery

### Sintomo: la mail non arriva al destinatario

**Workflow di debug**:

```bash
# 1. La mail è arrivata al listener?
sqlite3 /var/lib/stormshield-smtp-relay/relay.db \
  "SELECT received_at, from_address, to_address, action_taken \
   FROM events_log WHERE to_address LIKE '%destinatario%' \
   ORDER BY received_at DESC LIMIT 5;"

# 2. È stata accodata in outbound?
sqlite3 /var/lib/stormshield-smtp-relay/relay.db \
  "SELECT id, state, attempts, last_error, smarthost, delivered_at \
   FROM outbound_queue WHERE rcpt_to_json LIKE '%destinatario%' \
   ORDER BY id DESC LIMIT 5;"

# 3. Cosa ha risposto lo smarthost?
journalctl -u stormshield-smtp-relay-scheduler --since "1 hour ago" | \
  grep -E "smtp|deliver|550|554"
```

### Casi tipici

| `state` | `last_error` | Causa | Azione |
|---|---|---|---|
| `sent` | None | ✅ Mail consegnata allo smarthost. Se non in inbox: **filtri lato destinatario** (vedi sotto). | Cerca in spam/quarantine del destinatario. |
| `failed` | `5xx` SMTP | Smarthost ha rigettato (mittente blocked, dominio sconosciuto, ecc.) | Verifica reputazione mittente, SPF, DKIM. |
| `pending` | None | Outbound queue non drenata | Verifica `stormshield-smtp-relay-scheduler` attivo |
| `failed` | timeout/connection | Smarthost irraggiungibile | Verifica connettività rete + DNS MX |

### Filtri lato destinatario (causa #1 di "non arriva")

Se la mail è in `state=sent` ma non arriva in inbox:

1. **Spam folder** del destinatario (Outlook, Gmail, ecc.)
2. **Quarantena Microsoft 365** — Security & Compliance → Threat → Quarantine
3. **Anti-spoofing** rule che blocca mittenti esterni
4. **SPF/DKIM/DMARC** del dominio mittente non valido → soft bounce o spam

Per **test affidabili** in inbox usare:
- Mittenti di **domini autorizzati** (es. `qualcuno@datia.it` per delivery a `*@datia.it`)
- Mittenti **reali** propri (es. la tua casella personale Gmail con SPF valido)
- Evitare `@example.com`, `@example.org`, `monitoring@*` — sono filtrati automaticamente.

---

## 7. Permission matrix per ruolo

| Endpoint | login | operator | admin | superadmin |
|---|---|---|---|---|
| Dashboard | ✓ | ✓ | ✓ | ✓ |
| Eventi (lettura) | ✓ | ✓ | ✓ | ✓ |
| Regole (lettura) | ✓ | ✓ | ✓ | ✓ |
| Regole (CRUD) | ✗ | ✓ | ✓ | ✓ |
| Promote regola → gruppo | ✗ | ✗ | ✓ | ✓ |
| Eliminazione gruppo (cascade) | ✗ | ✗ | ✗ | ✓ |
| AI Assistant (tutta UI) | ✗ | ✗ | ✓ | ✓ |
| AI shadow → live switch | ✗ | ✗ | ✓ | ✓ |
| AI proposals accept/reject | ✗ | ✗ | ✓ | ✓ |
| Privacy bypass (CRUD email) | ✗ | ✗ | ✓ | ✓ |
| Privacy bypass (delete dominio) | ✗ | ✗ | ✗ | ✓ |
| Chiavi API (CRUD) | ✗ | ✗ | ✓ | ✓ |
| Chiavi API (delete) | ✗ | ✗ | ✗ | ✓ |
| Moduli Python (install) | ✗ | ✗ | ✗ | ✓ |
| Moduli Python (uninstall) | ✗ | ✗ | ✗ | ✓ |
| Tenant (CRUD) | ✗ | ✗ | ✗ | ✓ |
| Health system | ✗ | ✗ | ✓ | ✓ |
| Settings (CRUD) | ✗ | ✗ | ✓ | ✓ |

---

## 8. Procedura aggiornamento versione

```bash
# 1. Backup completo
/usr/local/sbin/domarc-relay-backup.sh

# 2. Pull nuova versione
cd /opt/domarc-smtp-relay-admin
git fetch origin
git log HEAD..origin/main --oneline   # vedi cosa cambia
git pull origin main

# 3. Aggiorna dipendenze (se cambiate)
.venv/bin/pip install -e .

# 4. Migrate (auto-eseguito al boot, ma puoi forzare manualmente)
.venv/bin/domarc-smtp-relay-admin migrate

# 5. Restart
systemctl restart domarc-smtp-relay-admin

# 6. Verifica
journalctl -u domarc-smtp-relay-admin -n 20
curl http://localhost:5443/health   # status_code 200
```

Se qualcosa va storto: rollback con `git checkout <previous-commit>` + restore backup.

---

_Ultimo aggiornamento: v1.0.0 — 2026-04-29._
