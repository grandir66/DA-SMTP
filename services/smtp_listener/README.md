# SMTP Listener — source

Source del listener `stormshield-smtp-relay-listener.service` + `stormshield-smtp-relay-scheduler.service`
(deployment runtime in `/opt/stormshield-smtp-relay/` sulla VM 4.25).

Storicamente il listener era un orphan deployment senza repo git: snapshot
copiato qui il 2026-05-04 per versionare le modifiche fatte in sessione
(timer mode aggregations, routing ticket verso manager-dev, payload
mapping descrizione/oggetto/canale, sync tk_key in occurrences).

## Struttura
- `relay/` — package Python del listener (pipeline, rules, storage, scheduler, ...)
- `conf/` — file di config esempio
- `pyproject.toml` — dipendenze + entry point CLI

## Deploy
Il deployment runtime resta in `/opt/stormshield-smtp-relay/` con `.venv`
dedicato. Per aggiornare:
\`\`\`bash
rsync -a --exclude=__pycache__ services/smtp_listener/relay/ /opt/stormshield-smtp-relay/relay/
systemctl restart stormshield-smtp-relay-listener stormshield-smtp-relay-scheduler
\`\`\`

## TODO
Unificare deployment + source come sottoprocesso/monorepo proper, oggi è un
mirror manuale.
