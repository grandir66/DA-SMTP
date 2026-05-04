# Runbook operativo H24 — intervento urgente a pagamento

Guida pratica per configurare, testare e fare troubleshooting della feature
"intervento urgente a pagamento via codice in oggetto mail" sul SMTP relay
Domarc.

> Per architettura e contesto vedi `CHANGELOG.md` (sezione H24 Fasi A-F)
> e i commit `2192501`..`57a7823` su `grandir66/DA-SMTP`.

---

## Concetti chiave

| Concetto | Cosa è |
|---|---|
| **Codice MONOUSO** | Generato dall'auto-reply fuori orario, formato `XXXXXX` (6 char alfabeto leggibile). Subject mailto `AUTH-XXXXXX`. TTL ≤ 24h. Usabile UNA SOLA VOLTA. Tabella `authorization_codes`. |
| **Codice PERMANENTE** | Codice fisso anagrafico per cliente con contratto H24. Es. `DOMARC-ACME-H24` o auto-generato `H24-XYZ12345ABCD`. Riusabile, revocabile solo da admin. Tabella `customer_h24_codes`. |
| **Mailbox di rientro** | Es. `h24@domarc.it` o `h24@datia.it`. Cliente clicca mailto e arriva qui. Una per brand. Tabella `smtp_relay_h24_targets`. |
| **Cascade validation** | Listener riceve mail → estrae codice → `POST /auth-codes/validate` → admin tenta prima oneshot (consume atomico), poi permanente. Restituisce kind. |
| **Multi-brand** | `source_domain → h24_alias` mappabile da UI. Cliente Datia → `h24@datia.it`, cliente Domarc → `h24@domarc.it`. Cascade fallback al setting globale. |

---

## Setup iniziale (passo per passo)

### Step 1 — Settings H24

Vai su `https://<relay>/h24-settings` (admin only) e configura:

```
h24.default_inbound_alias    = h24@domarc.it    # fallback globale multi-brand
h24.default_urgent_fee_eur   = 250              # importo intervento
h24.code_one_shot_ttl_hours  = 24               # cap TTL difensivo
h24.permanent_code_prefix    = H24-             # prefisso codici auto-generati
h24.subject_extract_regex    = (vuoto)          # default hardcoded sicuro
```

### Step 2 — Mappatura mailbox brand

Vai su `/h24-targets` e aggiungi una mappatura per ogni brand attivo:

| Dominio sorgente | Alias H24 | Override fee |
|---|---|---|
| `datia.it` | `h24@datia.it` | (default 250) |
| `domarc.it` | `h24@domarc.it` | (default 250) |

### Step 3 — Codici permanenti

Per ogni cliente con contratto H24, vai su `/h24-codes` e crea il codice:
- **Codice cliente**: il codcli del cliente (es. `73053`)
- **Etichetta**: descrizione es. "Direzione IT", "Reperibilità tecnica"
- **Codice custom**: opzionale (es. `DOMARC-ACME-H24` per leggibilità) o auto-generato
- **Note**: contratto di riferimento, data attivazione, ecc.

Il codice va comunicato al cliente via canale separato (mail manuale,
chat, telefono).

### Step 4 — Regole pipeline

Servono 3 regole nel rule engine:

#### 4a) Auto-reply fuori orario con codice (sulla casella sorgente)

```yaml
name: "Auto-reply fuori orario H24"
scope_type: global  # o mailbox-specifica
priority: 50  # numerica bassa = alta prio
match_to_regex: "^(supporto|assistenza|helpdesk)@(datia|domarc)\\.it$"
only_outside_service_hours: true
action: auto_reply
action_map:
  auto_reply_template: "out_of_hours_with_paid_option"
  generate_auth_code: true
  auth_code_ttl_hours: 24
  auto_reply_from: "noreply@domarc.it"
  urgent_fee: 250  # opzionale, override default
```

#### 4b) Codice valido sulla mailbox di rientro (priorità alta)

```yaml
name: "H24 inbound — autorizzazione codice"
scope_type: global
priority: 5
match_to_regex: "^h24@(datia|domarc)\\.it$"
match_subject_regex: "AUTH-|H24-|DOMARC-|[A-Z][A-Z0-9-]{6,}"
action: create_authorized_ticket
action_map:
  settore: "S"
  urgenza: "URGENTE"
  ack_template_id: 9     # h24_ack (verifica ID corrente)
  reject_template_id: 11 # h24_reject
```

#### 4c) Catch-all reject sulla stessa mailbox (priorità più bassa)

```yaml
name: "H24 inbound — reject senza codice"
scope_type: global
priority: 100
match_to_regex: "^h24@(datia|domarc)\\.it$"
action: auto_reply
action_map:
  auto_reply_template: "h24_reject"
  auto_reply_from: "noreply@domarc.it"
```

> **Importante**: la priorità della 4b deve essere **inferiore** (numericamente)
> alla 4c. Se la 4c precede la 4b, la 4c matcha prima e la 4b non viene mai eseguita.

---

## Test end-to-end

### Test 1 — Codice permanente (happy path)

```bash
# Sulla VM relay, simula cliente che usa codice permanente
ssh root@192.168.4.25 'python3 -c "
import smtplib
from email.message import EmailMessage
msg = EmailMessage()
msg[\"From\"] = \"r.grandi@datia.it\"
msg[\"To\"] = \"h24@datia.it\"
msg[\"Subject\"] = \"DOMARC-ACME-H24 - Server giù\"
msg.set_content(\"Test happy path permanente\")
with smtplib.SMTP(\"127.0.0.1\", 25) as s: s.send_message(msg)
"'

# Atteso:
# - log listener: H24 ticket enqueued kind=permanent code=DOMARC-ACME-H24
# - dispatch_queue.state=sent (HTTP 201 da manager-dev)
# - usage row in customer_h24_codes_usage
# - ack mail in outbound_queue
```

### Test 2 — Codice monouso (ciclo completo)

```bash
# Source mail (genera codice via auto-reply)
ssh root@192.168.4.25 'python3 -c "
import smtplib
from email.message import EmailMessage
msg = EmailMessage()
msg[\"From\"] = \"cliente@example.com\"
msg[\"To\"] = \"r.grandi@datia.it\"
msg[\"Subject\"] = \"Aiuto urgente\"
msg.set_content(\"Server prod down\")
with smtplib.SMTP(\"127.0.0.1\", 25) as s: s.send_message(msg)
"'

# Recupera il codice generato:
ssh root@192.168.4.25 'sqlite3 /var/lib/domarc-smtp-relay-admin/admin.db \
  "SELECT code FROM authorization_codes ORDER BY id DESC LIMIT 1"'

# Simula click mailto:
CODE=...
ssh root@192.168.4.25 "python3 -c \"
import smtplib
from email.message import EmailMessage
msg = EmailMessage()
msg['From'] = 'cliente@example.com'
msg['To'] = 'h24@datia.it'
msg['Subject'] = 'AUTH-${CODE} - Aiuto urgente'
msg.set_content('Confermo intervento a pagamento')
with smtplib.SMTP('127.0.0.1', 25) as s: s.send_message(msg)
\""

# Atteso: ticket creato (kind=oneshot), codice consumato (used_at, used_by=h24:*)
```

### Test 3 — Reject (codice mancante o farlocco)

```bash
ssh root@192.168.4.25 'python3 -c "
import smtplib
from email.message import EmailMessage
msg = EmailMessage()
msg[\"From\"] = \"cliente@example.com\"
msg[\"To\"] = \"h24@datia.it\"
msg[\"Subject\"] = \"Aiuto generico\"  # nessun codice
msg.set_content(\"\")
with smtplib.SMTP(\"127.0.0.1\", 25) as s: s.send_message(msg)
"'

# Atteso: action=auto_reply (template h24_reject), nessun ticket
```

### Test 4 — Codice riusato (already_used)

Spedisci due volte la stessa mail con `AUTH-XXX`. La seconda volta il
codice è già consumato → reject con template `h24_already_used`.

---

## Troubleshooting

### Sintomo: il cliente clicca il pulsante ma non riceve ticket

**Verifica 1**: il codice è valido?
```bash
ssh root@192.168.4.25 'sqlite3 /var/lib/domarc-smtp-relay-admin/admin.db \
  "SELECT code, used_at, used_by, valid_until FROM authorization_codes \
   WHERE code = \"XXXXXX\""'
# Se used_at popolato → consumato
# Se valid_until < now → scaduto
```

**Verifica 2**: il subject contiene il codice riconoscibile?
```bash
ssh root@192.168.4.25 '/opt/domarc-smtp-relay-admin/.venv/bin/python3 -c "
from domarc_relay_admin.h24_code_extractor import extract_auth_code
print(extract_auth_code(\"AUTH-XXXXXX - Server giù\"))
"'
# Atteso: "AUTH-XXXXXX"
```

**Verifica 3**: il listener ha ricevuto la mail?
```bash
ssh root@192.168.4.25 'journalctl -u stormshield-smtp-relay-listener \
  --since "10 minutes ago" --no-pager | grep -i "h24\|create_authorized"'
```

**Verifica 4**: il ticket è in dispatch_queue?
```bash
ssh root@192.168.4.25 'sqlite3 /var/lib/stormshield-smtp-relay/relay.db \
  "SELECT id, state, last_error FROM dispatch_queue WHERE state IN (\"pending\",\"error\",\"dead\") ORDER BY id DESC LIMIT 5"'
```

### Sintomo: regola non matcha sulla mailbox di rientro

- Verifica `match_to_regex` esatto (es. `^h24@datia\\.it$` con escape).
- Verifica priorità: la regola create_authorized_ticket deve avere priorità
  numerica MINORE della catch-all reject.
- Forza re-sync: `systemctl restart stormshield-smtp-relay-scheduler`.

### Sintomo: `h24_inbound_alias` vuoto nei template

Cascade fallita su tutti i livelli:
1. `action_map.h24_inbound_alias` non settato nella regola
2. `smtp_relay_h24_targets` non ha mappatura per il dominio mittente
3. `h24.default_inbound_alias` setting vuoto

```bash
# Verifica cache listener
ssh root@192.168.4.25 'sqlite3 /var/lib/stormshield-smtp-relay/relay.db \
  "SELECT * FROM h24_targets_cache"'
ssh root@192.168.4.25 'sqlite3 /var/lib/stormshield-smtp-relay/relay.db \
  "SELECT key, value_json FROM settings_cache WHERE key LIKE \"h24.%\""'
```

Sistemato lato admin → forza sync: `systemctl restart stormshield-smtp-relay-scheduler`.

### Sintomo: codice permanente non riconosciuto come tale

L'extractor estrae il codice ma la cascade va in fallback "non valido":

```bash
# Verifica esistenza codice nel DB
ssh root@192.168.4.25 'sqlite3 /var/lib/domarc-smtp-relay-admin/admin.db \
  "SELECT code, codice_cliente, enabled, revoked_at FROM customer_h24_codes \
   WHERE code = \"DOMARC-...\""'
# Se enabled=0 o revoked_at popolato → codice disabilitato
```

### Sintomo: ticket H24 non arrivano a manager-dev

```bash
# Verifica routing ticket_api
ssh root@192.168.4.25 'sqlite3 /var/lib/domarc-smtp-relay-admin/admin.db \
  "SELECT key, value FROM settings WHERE key LIKE \"ticket_api.%\""'
# Verifica response su dispatch
ssh root@192.168.4.25 'sqlite3 /var/lib/stormshield-smtp-relay/relay.db \
  "SELECT id, state, manager_response FROM dispatch_queue \
   WHERE state IN (\"sent\",\"dead\") ORDER BY id DESC LIMIT 3"'
```

---

## Operazioni periodiche

### Cleanup codici monouso scaduti (automatico)

Lo scheduler listener chiama nightly:
```
POST /api/v1/relay/maintenance/cleanup-oneshot-codes {retention_days: 7}
```

Cancella codici `valid_until < now - 7d AND used_at IS NULL`. I codici usati
restano per audit.

Forzare manualmente:
```bash
ssh root@192.168.4.25 'API_KEY=$(sqlite3 /var/lib/domarc-smtp-relay-admin/admin.db \
  "SELECT value FROM settings WHERE key=\"relay_api_key\"")
curl -sk -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
  -X POST https://localhost/api/v1/relay/maintenance/cleanup-oneshot-codes \
  -d "{\"retention_days\": 7}"'
```

### Rendicontazione manager (futura)

`customer_h24_codes_usage.reported_to_manager_at` predisposto. Quando il
manager esporrà l'endpoint H24 events, il loop `_h24_usage_flush_loop`
del listener farà flush batch. Per ora è stub: log debug ogni 5 min.

### Revoca codice permanente

UI: `/h24-codes/<id>/revoke` con motivo. Operazione idempotente, audit
in `revoked_at` / `revoked_by` / `revoked_reason`.

CLI emergency:
```bash
ssh root@192.168.4.25 'sqlite3 /var/lib/domarc-smtp-relay-admin/admin.db \
  "UPDATE customer_h24_codes SET enabled=0, revoked_at=datetime(\"now\"), \
   revoked_by=\"emergency\", revoked_reason=\"...\" WHERE code=\"DOMARC-...\""'
```

---

## Architettura riassuntiva

```
                       admin standalone (Flask + SQLite admin.db)
                       ┌──────────────────────────────────────────┐
                       │  /h24-codes        UI codici permanenti  │
                       │  /h24-targets      UI multi-brand        │
                       │  /h24-settings     UI 5 parametri H24    │
                       │  /h24-dashboard    KPI live              │
                       │                                          │
                       │  POST /api/v1/relay/auth-codes           │
                       │  POST /api/v1/relay/auth-codes/validate  │
                       │  GET  /api/v1/relay/h24-targets/active   │
                       │  POST /api/v1/relay/maintenance/...      │
                       └──────────────────────────────────────────┘
                                          ▲                 │
                                          │ POST            │ sync
                                          │ validate        │ targets
                                          │                 ▼
                       listener (aiosmtpd + scheduler + relay.db)
                       ┌──────────────────────────────────────────┐
                       │  pipeline.py → action create_authorized_ │
                       │     ticket → backend.validate_auth_code  │
                       │  scheduler → _h24_maintenance_loop (24h) │
                       │  scheduler → _h24_usage_flush_loop (5m)  │
                       │  storage.h24_targets_cache               │
                       └──────────────────────────────────────────┘

Storage layout:
- admin.db  customer_h24_codes (permanenti)
            customer_h24_codes_usage (audit + reported_to_manager_at)
            authorization_codes (monouso, ALTER event_uuid)
            smtp_relay_h24_targets (multi-brand)
            settings (h24.* keys)
            reply_templates (5 template H24)

- relay.db  h24_targets_cache (sync da admin)
            settings_cache (sync da admin, include h24.*)
            templates_cache (sync da admin, include h24_*)
```

---

## Riferimenti

- **CHANGELOG.md** — sezione H24 Fasi A-F.
- **Repo GitHub** — `grandir66/DA-SMTP` main.
- **CLAUDE.md** del relay — sezione "H24 — flusso autorizzazioni urgenti".
- Plan operativo originale — `/tmp/h24-feature-spec-for-relay-session.md`.
