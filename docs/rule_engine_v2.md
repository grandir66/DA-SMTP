# Rule Engine v2 — Gerarchia padre/figlio

Documento operatore per la gestione delle regole SMTP nell'admin Domarc Relay
dopo l'introduzione del modello padre/figlio (migration 010).

> **Aggiornato 2026-05-06** con Form regole UX v3 + M028-M036:
> - **UX v3** Toggle Modalità Base/Avanzata sui 3 form (orfana/gruppo/figlio)
>   con persistenza localStorage, validazione live regex, mini-simulatore
>   inline, anteprima impatto. Vedi sezione "UX dei form" più sotto.
> - **V001-V008/V_PRI_RANGE** finalmente wired in `upsert_rule` via helper
>   `_run_full_validators()` nei 3 route handler (era dead code).
> - **M029** rule_sets per profilo orario (organizzazione UI; **non** gating runtime dopo M035)
> - **M033** shadow mode per regola singola
> - **M034** gruppi cliente self-contained (auto-assignment via mapping rules)
> - **M035** filtro contratto **solo** via `match_customer_groups` (no più
>   `active_rule_set_ids` runtime)
> - **M036** thread continuation RFC 2822 (no duplicate ticket su risposte)

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

## Filtro per contratto/profilo cliente — solo via gruppi cliente (M035)

Dopo la semplificazione M035, il filtro di una regola sul tipo di contratto
(STD/EXT/H24) o sul profilo orario del cliente avviene **esclusivamente**
tramite `match_customer_groups` (CSV).

I gruppi cliente sono **self-contained** (M034): in
`/customer-groups/<id>/membership-rules` si definiscono regole di
auto-assegnamento basate sui campi del cliente (`contract_type`,
`tipologia_servizio`, JSON custom). Il sistema ricalcola i membri ogni 5min
o on-demand.

I rule_sets (M029, `globali` / `std_window` / `ext_window` / `h24_window`)
restano per **organizzazione UI** delle regole per profilo orario, ma NON
sono più gating runtime: una regola in `h24_window` non viene saltata se
il cliente è STD — la sua attivazione dipende solo dal `match_*` (gruppi
cliente, fasce orarie, ecc.).

## Thread continuation (M036)

Le risposte a una mail già tracciata (`In-Reply-To` o `References` che
match-ano un `message_id` registrato in `events_log`) attivano la regola
seed priority=5 in rule_set `globali`:

- `match_is_thread_continuation = 1`
- `action = default_delivery`

L'evento risposta NON apre un nuovo ticket (il `ticket_id` è ereditato
dall'evento parent). Il match `match_is_thread_continuation` è tristate
(NULL/0/1) e disponibile su tutti i form (orfana, gruppo padre, figlio).

## Shadow mode (M030/M031/M033)

Regole/gruppi/domini possono essere messi in **shadow mode**: il rule
engine valuta tutto e registra `shadow_action` / `shadow_rule_id`
nell'evento, ma l'azione effettiva è `default_delivery`. Audit completo
permette di confrontare "cosa sarebbe successo se non shadow".

Cascata:
1. **Domain shadow** (M031): copre tutto l'evento.
2. **Recipient group shadow** (M030): solo destinatari del gruppo.
3. **Rule shadow** (M033): solo quella regola.

Use case tipico: portare in produzione una nuova regola a basso rischio,
osservare per N giorni in shadow, poi disattivare il flag quando si è
sicuri.

## UX dei form regola (v3, 2026-05-06)

I 3 form (`/rules/new`, `/rules/groups/new`,
`/rules/groups/<id>/children/new`) condividono **stessa struttura a 5
sezioni numerate** + **toggle globale Base/Avanzata** in cima.

### Filosofia

- **Default = gestione per gruppo cliente**: il singolo cliente è
  un'eccezione. Il filtro principale visibile in Base è
  `match_customer_groups` (multi-select con i gruppi built-in
  auto-popolati `contract_standard`/`contract_extended`/
  `contract_h24`/`vip`/`secondary`/`do_not_follow`).
- **Default destinatari = gruppo destinatari** (`match_to_group_id`),
  no singolo destinatario. Stessa cosa per il forward target.
- **Tristate `match_known_customer` / `match_contract_active`** in
  Avanzata, etichettati "(deriva dal gruppo)": scegliendo un
  gruppo cliente built-in l'informazione è già implicita.
- `match_is_thread_continuation` in Avanzata: la regola seed
  priority=5 in `globali` la gestisce nel 99% dei casi.
- `rule_set_id` in Avanzata marcato `(legacy)`: post-M035 il filtro
  contratto è via gruppi cliente, il set rimane solo organizzazione UI.

### Le 5 sezioni

1. **Identificazione e stato** — name, description, priority+preset, enabled, shadow_mode (Avanzata: shadow_note, scope, severity, rule_set_id, flag flow, greyed cross-tipo)
2. **Origine, destinatari e forward** — `match_customer_groups`★, `match_to_group_id`, `forward_to_group_id` (Avanzata: from_*, to_domain/regex puntuali, forward_to_emails lista, contract/known)
3. **Contenuto del messaggio** — subject_regex, body_regex (Avanzata: match_tag)
4. **Contesto cliente e orario** — match_in_service, match_at_hours+preset (Avanzata: has_exception_today, is_thread_continuation)
5. **Azione e flusso** — action selector + parametri principali, keep_original_delivery (Avanzata: also_deliver_to, apply_rules, severity, varianti reply_*)

### Funzioni interattive

- **Preset priority** (4 button accanto al campo): ⚡ Critica (10),
  📋 Standard (200), 🐢 Bassa (500), 🪤 Catch-all (900). Per il figlio
  i preset sono offset dal padre (+10/+50/+100).
- **Validazione live regex** (debounce 350ms): mentre digiti in un
  campo `match_*_regex` il sistema fa `new RegExp()` client-side e
  mostra ✓ verde / ✗ rosso. Backend rifiuta comunque regex spezzata
  via `re.compile()` in `upsert_rule` (V_REGEX).
- **Mini-simulatore inline** (orfana e figlio): textarea per subject
  di prova in fondo al form, mostra ✓/✗ live contro il
  `match_subject_regex` corrente.
- **Anteprima impatto** (orfana e figlio): bottone che chiama `POST
  /rules/preview-impact` con tutti i match_* del form. Il backend
  scansiona `events_log` degli ultimi 7gg (cap 2000 eventi) e
  ritorna conteggio + sample + top domini. Utile per capire se la
  regola è troppo larga/stretta prima di abilitarla.

### Validazione server-side (V001-V008/V_PRI_RANGE wired)

`validate_rule()` di `rules/validators.py` è ora chiamato da
`form_view`/`group_form_view`/`child_form_view` via helper
`_run_full_validators()`. Errori bloccano save (flash `error`),
warnings (W004 redundant match, W_PRI_GAP gap minimo) flushed come
`warning` ma il save procede.

`upsert_rule` aggiunge:
- check "almeno un match_*" esteso a `match_customer_groups`,
  `match_to_group_id`, `match_at_hours`, `match_tag`, tutti i
  tristate (era solo regex/domain → rifiutava regole con SOLO
  filtro su gruppo cliente)
- `re.compile()` su tutti i regex prima del salvataggio
- mutex backend `match_to_regex`/`match_to_group_id` e
  `forward_to_emails`/`forward_to_group_id` (prima validato solo
  in form orfana)
- range priority hard 1..999_999 (V007 server-side)

`MATCH_FIELDS_TEXT/INT/TRISTATE` esteso con `match_customer_groups`,
`match_to_group_id`, `match_is_thread_continuation`.
`_matches_compatible` ora gestisce:
- `match_to_group_id`: uguaglianza esatta padre/figlio (V006 se
  diversi)
- `match_customer_groups`: intersezione CSV non vuota (V006 se
  disgiunti, es. padre h24_customers + figlio std_customers)

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
- Migration: [domarc_relay_admin/migrations/029_rule_sets.sqlite.sql](../domarc_relay_admin/migrations/029_rule_sets.sqlite.sql)
- Migration: [domarc_relay_admin/migrations/034_group_membership_rules.sqlite.sql](../domarc_relay_admin/migrations/034_group_membership_rules.sqlite.sql)
- Migration: [domarc_relay_admin/migrations/035_simplify_rules_to_group_based.sqlite.sql](../domarc_relay_admin/migrations/035_simplify_rules_to_group_based.sqlite.sql)
- Migration: [domarc_relay_admin/migrations/036_thread_tracking.sqlite.sql](../domarc_relay_admin/migrations/036_thread_tracking.sqlite.sql)
- Listener: `/opt/stormshield-smtp-relay/relay/rules.py` (esteso con `match_is_thread_continuation`)
