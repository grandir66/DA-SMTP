# Rule Engine v2 — Gerarchia padre/figlio

Documento operatore per la gestione delle regole SMTP nell'admin Domarc Relay
dopo l'introduzione del modello padre/figlio (migration 010).

## Concetti

### Tre tipi di record

| Tipo | `is_group` | `parent_id` | Esegue azioni? | Quando usarlo |
|------|------------|-------------|----------------|---------------|
| **Orfana** | 0 | NULL | Sì (action propria) | Regola standalone (catch-all, skip, ignore notifiche). |
| **Gruppo** (padre) | 1 | NULL | No (solo defaults) | Raccoglie regole correlate sotto match_* condivisi e action_map ereditata. |
| **Figlio** | 0 | id_gruppo | Sì (action propria) | Variante d'azione all'interno di un gruppo (es. auto_reply + create_ticket sotto "Fuori orario"). |

### Priority globale unica (1..999999)

Niente moltiplicazione `padre*1000+figlio`: ogni record ha una priority assoluta sull'asse globale.

- Le regole vengono valutate in ordine `priority ASC`.
- Vincolo (V_PRI_RANGE): la priority di un figlio deve essere
  **strettamente maggiore** della priority del padre e **strettamente minore**
  della priority del prossimo top-level (gruppo o orfana successiva).
- Suggerito (W_PRI_GAP): lasciare gap di **almeno 10** fra figli, per
  consentire inserimenti futuri senza rinumerare il blocco.

Esempio:

```
prio  10  • orfana "Skip mailer-daemon"
prio 100  ▾ gruppo "Fuori orario contratto"
prio 110     ├ child "Auto-reply"
prio 120     └ child "Crea ticket"
prio 200  • orfana "Catch-all log"
```

### Ereditarietà action_map

Il gruppo padre fornisce **defaults** ereditabili dai figli (whitelist
`PARENT_ACTION_MAP_DEFAULTS`):

- `keep_original_delivery`, `also_deliver_to`, `apply_rules`
- `reply_mode`, `reply_subject_prefix`, `reply_quote_original`,
  `reply_attach_original`, `reply_to`
- `generate_auth_code`, `auth_code_ttl_hours`

Le chiavi figlio-only (`template_id`, `settore`, `urgenza`,
`addetto_gestione`, `forward_target`, `redirect_to`, `reason`, ecc.) NON
sono ereditabili e vengono respinte se messe sul gruppo (V003).

Il figlio può **sovrascrivere** qualsiasi default ereditato. I valori `null`
del figlio NON sono override (significano "non specificato").

### Flag di flusso (sui figli)

| Combinazione | Comportamento |
|--------------|---------------|
| `continue_in_group=False, exit_group_continue=False` | **STOP** dopo il match (rispetta `exclusive_match`). |
| `continue_in_group=True` | Dopo il match, valuta i fratelli successivi nel gruppo. |
| `exit_group_continue=True` | Come `continue_in_group=True`, ma in più forza l'ultimo figlio del gruppo a propagare al top-level (utile per "esci dal blocco"). |

### `exclusive_match` (sul gruppo)

- Default `True`: dopo che un figlio matcha, i top-level successivi (orfane
  e gruppi) NON vengono valutati.
- `False`: l'ultimo figlio matchato propaga `continue_after_match=True`,
  permettendo a gruppi/orfane successive di essere valutate.

## Workflow tipici

### Creare un gruppo

1. Andare su **Regole** → **Nuovo gruppo**.
2. Compilare etichetta (es. "Fuori orario contratto"), priority, scope.
3. Definire i `match_*` condivisi (`to_domain`, `in_service=fuori`, `contract_active=sì`, ecc.).
4. Compilare i `action_map_defaults` ereditabili (es. `keep_original_delivery=on`,
   `reply_mode=to_sender_only`, `also_deliver_to=ticket@domarc.it`).
5. Salvare. Si apre la pagina del gruppo con la tabella figli vuota.
6. **Aggiungi figlio** → crea il primo figlio (es. `auto_reply` con
   `template_id=1` e `continue_in_group=on`).
7. **Aggiungi figlio** → secondo figlio (es. `create_ticket` con `settore=assistenza`,
   `urgenza=NORMALE`).

### Promuovere un'orfana esistente a gruppo

1. Dalla **lista regole**, cliccare l'icona "Promuovi a gruppo" sull'orfana.
2. Inserire l'etichetta del nuovo gruppo nel prompt.
3. Il sistema crea il gruppo, sposta i match_* e i defaults action_map sul
   padre, e lascia il resto sul figlio.
4. Aggiungere altri figli al gruppo come desiderato.

### Convertire più regole in un gruppo (wizard)

1. **Regole** → **Suggerisci gruppi**.
2. Il wizard mostra cluster di regole orfane con match_* identici.
3. Per ciascun cluster: rivedere etichetta, lista regole, defaults condivisi.
4. Cliccare **Promuovi a gruppo** → la prima regola del cluster diventa
   "modello", le altre vengono agganciate come figli del nuovo gruppo.

### Simulare un evento

1. **Regole** → **Simulazione**.
2. Compilare `from`, `to`, `to_domain`, `subject`, `body` + contesto
   (`in_service`, `sector`).
3. **Esegui simulazione** mostra:
   - chain di valutazione step-by-step (gruppo + figli + orfane);
   - quali regole hanno matchato (✓) o sono state saltate (✗);
   - action_map effettiva di ogni figlio (con chiavi ereditate);
   - lista azioni eseguite e default delivery.

### Anteprima flatten verso il listener

**Regole** → **Anteprima flatten**: mostra esattamente le regole flat servite
all'endpoint `GET /api/v1/relay/rules/active` (lo stesso che il listener
legge). Ogni riga indica se proviene da un gruppo (`_source_group_id`) o
è orfana, con i match_* mergiati e l'action_map ereditata.

## Validatori (regole hard)

Errori bloccanti:

- **V001** Un gruppo non può avere padre.
- **V002** Il padre referenziato deve essere un gruppo.
- **V003** I gruppi non eseguono azioni (action vuota o `group`); l'action_map
  del gruppo accetta solo chiavi `PARENT_ACTION_MAP_DEFAULTS`.
- **V004** Un gruppo deve avere almeno un `match_*` (catch-all gerarchico vietato).
- **V005** No riferimenti circolari (`parent_id == id`).
- **V006** Match incompatibili padre/figlio (es. `match_to_domain` diverso).
- **V007** Priority fuori range 1..999999.
- **V008** Un gruppo non può essere figlio (max 1 livello).
- **V_PRI_RANGE** Priority figlio deve stare strettamente fra padre e prossimo top-level.

Warning soft (non bloccanti):

- **W001** Gruppo senza figli.
- **W002** Figlio senza match_* propri (eredita solo dal padre — è ok se intenzionale).
- **W004** Match ridondante padre/figlio.
- **W005** Gruppo `exclusive_match=False` con ultimo figlio STOP totale (comportamento ambiguo).
- **W_PRI_GAP** Gap fra fratelli minore di 10.

## Esempio end-to-end: "Fuori orario contratto"

### Configurazione gerarchica

**Gruppo** (`is_group=1`, `priority=500`):
- `group_label = "Fuori orario contratto"`
- `match_to_domain = "domarc.it"`, `match_in_service = 0`, `match_contract_active = 1`
- `action_map = {keep_original_delivery: true, also_deliver_to: "ticket@domarc.it",
   reply_mode: "to_sender_only", generate_auth_code: true, auth_code_ttl_hours: 12}`
- `exclusive_match = True`

**Figlio #1** (`parent_id=gruppo`, `priority=510`):
- `name = "Auto-reply out_of_hours"`
- `action = auto_reply`, `action_map = {template_id: 1, reply_subject_prefix: "Re: "}`
- `continue_in_group = True`

**Figlio #2** (`parent_id=gruppo`, `priority=520`):
- `name = "Crea ticket NORMALE"`
- `action = create_ticket`, `action_map = {settore: "assistenza", urgenza: "NORMALE"}`
- `continue_in_group = False`, `exit_group_continue = False`  → STOP

### Cosa succede a runtime

Mail da `mario@cliente.com` a `assistenza@domarc.it` alle 23:00:

1. Listener riceve 3 regole flat dall'admin (TEST ACME prio 99 ignore + i due
   figli del gruppo prio 510 e 520).
2. TEST ACME non matcha (subject regex specifica).
3. Figlio prio 510 → match → `auto_reply` con action_map mergiata; cont=True.
4. Figlio prio 520 → match → `create_ticket`; cont=False, STOP.

Comportamento identico al modello flat duplicato precedente, ma con un solo
punto di edit per i match condivisi.

## API listener (retro-compatibile)

`GET /api/v1/relay/rules/active` ritorna sempre regole flat con i campi
classici. Aggiunge metadata opzionali `_source_group_id` e
`_source_child_id` quando la regola flat proviene da un gruppo (utile per
audit; il listener legacy li ignora).

Per il futuro listener "v2-aware" si potranno usare questi metadata per
arricchire l'audit log con `flow_path = "group:{X} → rule:{Y}"`.

## Riferimenti

- Codice: [domarc_relay_admin/rules/](../domarc_relay_admin/rules/)
- Test parità: [tests/test_rule_engine_parity.py](../tests/test_rule_engine_parity.py)
- Migration: [domarc_relay_admin/migrations/010_rule_groups.sqlite.sql](../domarc_relay_admin/migrations/010_rule_groups.sqlite.sql)
- Listener: `/opt/stormshield-smtp-relay/relay/rules.py` (NON modificato)
