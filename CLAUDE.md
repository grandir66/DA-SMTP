# Domarc SMTP Relay — Admin

SMTP relay standalone (Flask + SQLite) con rule engine deterministico e AI assistant per triage mail. Multi-tenant, gira su VM dedicata `192.168.4.25` (Ubuntu 24.04). **Cosa NON è**: parte del manager gestionale Domarc (sistema diverso, repo diverso, 192.168.4.41 — usato SOLO come sorgente PG clienti read-only e destinazione ticket via HTTPS).

## Pointer

- [README.md](README.md) — overview prodotto, feature, stato release
- [CHANGELOG.md](CHANGELOG.md) — Keep a Changelog italiano, ISO date
- [docs/AGENT_BRIEFING.md](docs/AGENT_BRIEFING.md) — onboarding agenti completo
- [docs/H24_RUNBOOK.md](docs/H24_RUNBOOK.md) — autorizzazioni urgenti pagamento
- [docs/rule_engine_v2.md](docs/rule_engine_v2.md) — rule engine + validators
- [docs/guida_funzionamento.md](docs/guida_funzionamento.md) — flusso end-to-end
- [docs/CLAUDE_ARCHIVE.md](docs/CLAUDE_ARCHIVE.md) — versione precedente verbose (riferimento)
- [.claude/skills/](.claude/skills/) — workflow deploy / release / migration
- [.claude/rules/](.claude/rules/) — direttive scoped per area
- [docs/adr/](docs/adr/) — Architecture Decision Records

## Stack

Python 3.11+, Flask 3, Jinja2, SQLite (admin.db + relay.db), gunicorn :5443 dietro nginx, aiosmtpd listener :25. Package admin = `domarc_relay_admin`; listener separato in `services/smtp_listener/` (runtime `/opt/stormshield-smtp-relay/`). Tre servizi systemd: `domarc-smtp-relay-admin`, `stormshield-smtp-relay-listener`, `stormshield-smtp-relay-scheduler`. PG read-only `solution`+`stormshield` sul 192.168.4.41 per sync clienti.

**Topologia rete**: VM relay su `192.168.4.25` (rete admin locale). ESVA LibraESVA antispam su `192.168.20.x` (SAMNET) — invia mail al listener :25. UFW apre :25/:443/:80 solo a `192.168.4.0/24` + `192.168.20.0/24`.

## Comandi essenziali

```bash
# Sviluppo locale (live sulla VM operativa)
.venv/bin/pytest                                    # test suite (187+ casi)
.venv/bin/python -c 'import py_compile; py_compile.compile("FILE.py", doraise=True)'

# Deploy (edit qui = già live, restart per ricaricare)
systemctl restart domarc-smtp-relay-admin
systemctl restart stormshield-smtp-relay-listener stormshield-smtp-relay-scheduler

# Verifica post-modifica (OBBLIGATORIA)
.venv/bin/pytest && systemctl is-active domarc-smtp-relay-admin && \
  journalctl -u domarc-smtp-relay-admin -p err --since "30s ago"
```

## Regole anti-regressione (CRITICHE)

1. **Branding**: il prodotto si chiama **Domarc SMTP Relay**, mai "Stormshield Relay" (Stormshield è il vendor del firewall).
2. **Test mail SOLO** con `r.grandi@domarc.it` o `r.grandi@datia.it`. Mai `info@`, `noreply@`, `monitoring@`, `*@example.*`, indirizzi inventati `@domarc.it`.
3. **PG produzione `solution`/`stormshield` = read-only** dal relay. Mai scrivere. Customer source pluggable, default `local` (tabella autoritativa `customers` post-M028).
4. **SQL**: sempre placeholder (`?` o `%s`), mai f-string sui parametri. `BEGIN IMMEDIATE` per sync DELETE+INSERT massivi.
5. **Secrets fuori da git**: `secrets.env`, `master.key`, `*.db`, `.venv/` mai committati. API key cifrate Fernet in tabella `api_keys`.
6. **CSRF**: ogni form POST HTML ha `{{ csrf_token() }}` hidden. Blueprint API REST esenti via `csrf.exempt(bp)` + X-API-Key.
7. **Auth**: tutte le route admin con `@login_required(role="...")`; endpoint `/api/v1/relay/*` con `@require_api_key`. Validare permessi backend, non solo template.
8. **Migrations**: file `migrations/NNN_*.sqlite.sql` idempotenti (`CREATE TABLE IF NOT EXISTS`, ALTER con try/except). Applicate una volta via `apply_migrations()`. Mai DROP/ALTER ad-hoc fuori da migration.
9. **AI payload**: passare SEMPRE attraverso `ai_assistant/pii_redactor` prima di inviare al modello. Mai PII/secret in chiaro al provider.
10. **Privacy bypass pre-cutover**: prima di instradare un nuovo dominio, popolare la lista (`/privacy-bypass`) con DPO/legale/HR/sindacale.
11. **Kill switch**: setting `relay_passthrough_only` esiste come rollback 1-click. Non rimuoverlo né bypassarlo nelle modifiche al pipeline.
12. **Anti-loop debug**: se una nuova feature ROMPE codice esistente, NON modificare l'esistente per fixare — aggiungere wrapper / feature flag / modulo separato. Vedi [.claude/rules/db-access.md](.claude/rules/db-access.md) e altre.

## File critici (zona ROSSA — non rompere senza migration)

| File | Perché |
|---|---|
| `domarc_relay_admin/app.py` | factory `create_app`, registra blueprint+CSRF+migrations |
| `domarc_relay_admin/config.py` | AppConfig + load_config (env + secrets.env) |
| `domarc_relay_admin/storage/sqlite_impl.py` | DAO core + apply_migrations |
| `services/smtp_listener/relay/pipeline.py` | flusso `process_message` |
| `services/smtp_listener/relay/rules.py` | RuleEngine v2 |
| `domarc_relay_admin/migrations/*.sqlite.sql` | schema autoritativo (numerati progressivi) |

## Convenzioni

- **Lingua**: italiano per tutto (UI, doc, commit, log utente, errori user-facing).
- **Commit**: `type(scope): descrizione concisa` (es. `feat(rules): …`, `fix(pipeline): …`, `docs: …`).
- **Versioning**: bump manuale in `pyproject.toml` + tag git + voce in `CHANGELOG.md` (sezione Aggiunte/Modifiche/Correzioni).
- **Python**: type hints dove utile, no formatter automatico configurato (preserva stile esistente).

## Loop di apprendimento (fine sessione)

Prima di chiudere, valuta:

- **Pattern emerso e riutilizzabile?** → nuovo file in `.claude/rules/` (scope file-type) o `.claude/skills/` (workflow)
- **Errore commesso 2+ volte?** → nuova regola anti-regressione qui sopra
- **Decisione architetturale?** → ADR in `docs/adr/` (template `0000-template.md`)
- **Fatto nuovo sul progetto?** → aggiorna README o `docs/guida_funzionamento.md`
- **Dopo 2 correzioni fallite sullo stesso punto**: `/clear` e riformula il prompt invece di insistere

## Setup nuovo dev (sulla VM 192.168.4.25)

```bash
cd /opt/domarc-smtp-relay-admin
python3.11 -m venv .venv && source .venv/bin/activate
pip install -e '.[postgres,prod,dev]'
.venv/bin/pytest                                   # baseline test
systemctl status domarc-smtp-relay-admin           # verifica servizio live
```
