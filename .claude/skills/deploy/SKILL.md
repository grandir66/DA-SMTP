---
name: deploy
description: Applica modifiche al codice sulla VM operativa 192.168.4.25 con restart dei servizi systemd
---

# Deploy (sulla VM operativa)

Il working directory `/opt/domarc-smtp-relay-admin/` è già la copia LIVE sul server. Una modifica al file è già su disco, basta ricaricare il processo. Non c'è "deploy remoto" da fare — siamo sul server.

## Comando standard

```bash
# 1. Verifica sintassi del file modificato
.venv/bin/python -c 'import py_compile; py_compile.compile("PATH/TO/FILE.py", doraise=True)'

# 2. Test suite (necessario se hai toccato storage, pipeline, rules, validators)
.venv/bin/pytest -x

# 3. Backup git (push remoto)
git add -A
git commit -m "feat(scope): descrizione concisa"
git push origin main

# 4. Restart del servizio interessato
#    - Modifiche admin web (routes/, templates/, static/, storage/, ai_assistant/)
systemctl restart domarc-smtp-relay-admin
#    - Modifiche listener/scheduler (services/smtp_listener/relay/)
systemctl restart stormshield-smtp-relay-listener stormshield-smtp-relay-scheduler

# 5. Verifica health (60s di errori dopo restart)
systemctl is-active domarc-smtp-relay-admin
journalctl -u domarc-smtp-relay-admin -p err --since "60s ago"
curl -sk -o /dev/null -w "HTTP %{http_code}\n" https://localhost/login
```

## Quando NON usare

- Modifica solo a `.md`, `CHANGELOG.md`, `docs/`: nessun restart richiesto, solo commit+push.
- Modifica a `migrations/NNN_*.sqlite.sql`: il restart non riapplica migration già eseguite. Per re-test su DB vuoto serve azione manuale separata.
- Modifica a `pyproject.toml` dipendenze: serve `pip install -e '.[postgres,prod,dev]'` PRIMA del restart.

## Anti-regressione

- **Mai** restartare l'admin senza prima aver verificato sintassi del file modificato (un SyntaxError porta il servizio in failed e blocca l'UI).
- Se restart del listener fallisce: `journalctl -u stormshield-smtp-relay-listener -n 50` per vedere lo stack. NON fare `rm relay.db` o azioni distruttive senza prima diagnosticare.
- Sync settings dall'admin verso il listener avviene ogni 5 min (`sync_interval_sec=300`). Per forzare immediato: restart dello scheduler.
- Dopo deploy in produzione con il kill switch ATTIVO, ricordare di disattivarlo (`relay_passthrough_only=false`) altrimenti rule engine + IA restano bypassati.
