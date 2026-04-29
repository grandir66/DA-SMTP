# Guida di funzionamento — Domarc SMTP Relay

Manuale operativo aggiornato post Rule Engine v2 (migration 010, gerarchia
padre/figlio). Pensato per chi configura le regole e per chi deve capire come
viene gestita una mail in arrivo.

---

## Indice

1. [Architettura in due righe](#1-architettura-in-due-righe)
2. [Vita di un'email — il flusso completo](#2-vita-di-unemail--il-flusso-completo)
3. [Il modello regole (gerarchia padre/figlio)](#3-il-modello-regole-gerarchia-padrefiglio)
4. [Gestione regole dalla UI](#4-gestione-regole-dalla-ui)
5. [Configurazioni che incidono sul matching](#5-configurazioni-che-incidono-sul-matching)
6. [Esempi end-to-end commentati](#6-esempi-end-to-end-commentati)
7. [Operatività e troubleshooting](#7-operatività-e-troubleshooting)
8. [Riferimenti rapidi](#8-riferimenti-rapidi)

---

## 1. Architettura in due righe

Tre processi su `192.168.4.41` collaborano:

| Componente | Path | Ruolo |
|---|---|---|
| **Listener SMTP** | `/opt/stormshield-smtp-relay/` (servizio `stormshield-smtp-relay.service`, porta 25) | Riceve le mail, applica le regole, esegue le azioni. |
| **Admin Web** | [/opt/domarc-smtp-relay-admin/](../) (servizio `domarc-smtp-relay-admin.service`, porta 5443 dietro nginx :8443) | UI gestione + DAO + API verso il listener. |
| **Customer Source** | adapter pluggabile (oggi `stormshield` → `https://manager-dev.domarc.it`) | Fornisce l'anagrafica clienti (codcli, domini, profili orari, contratto). |

Il listener **non legge il database SQLite dell'admin**. Ogni qualche minuto fa
`GET /api/v1/relay/rules/active` (e gli altri endpoint sotto `/api/v1/relay/...`)
e cache-a in locale tutto ciò che gli serve. Il file di stato del listener è
in `/var/lib/stormshield-smtp-relay/`. Il DB dell'admin è in
`/var/lib/domarc-smtp-relay-admin/admin.db`.

Conseguenza pratica: una modifica fatta in UI è effettiva sul listener al
prossimo sync (tipicamente entro 1-5 minuti). Per forzare il sync immediato
si può `systemctl restart stormshield-smtp-relay`.

---

## 2. Vita di un'email — il flusso completo

```
                         ┌─────────────────────────────────────────────┐
                         │ 1. Listener riceve la mail su :25           │
                         └────────────┬────────────────────────────────┘
                                      │
                         ┌────────────▼────────────────────────────────┐
                         │ 2. Parser estrae from / to / subject /      │
                         │    body / message-id / loop markers /       │
                         │    Auto-Submitted / Precedence              │
                         └────────────┬────────────────────────────────┘
                                      │
                         ┌────────────▼────────────────────────────────┐
                         │ 3. resolve_customer:                         │
                         │  a. cerca un alias attivo (route)            │
                         │  b. risolve codcli dal `to` o dal `from`     │
                         │  c. carica contract_active e schedule orari  │
                         │  d. calcola `in_service` (oggi / ora)        │
                         └────────────┬────────────────────────────────┘
                                      │
                         ┌────────────▼────────────────────────────────┐
                         │ 4. _should_skip_rules?                        │
                         │   (alias o dominio con apply_rules=false)    │
                         │   → SÌ: salta direttamente al passo 7        │
                         └────────────┬────────────────────────────────┘
                                      │
                         ┌────────────▼────────────────────────────────┐
                         │ 5. RuleEngine.evaluate()                     │
                         │    Le regole flat (post-flatten dell'admin)  │
                         │    vengono valutate in ordine `priority ASC`,│
                         │    grouping per scope_type. Per ciascuna     │
                         │    regola: scope, vincolo orario,            │
                         │    match_*_regex, match_to_domain.           │
                         │    Si ferma al primo match con               │
                         │    `continue_after_match=False`.             │
                         └────────────┬────────────────────────────────┘
                                      │
                         ┌────────────▼────────────────────────────────┐
                         │ 6. dispatch_action(action, action_map):     │
                         │    auto_reply / create_ticket / forward /    │
                         │    redirect / quarantine / flag_only /       │
                         │    ignore. action_map effettiva = merge      │
                         │    defaults_padre + override_figlio.         │
                         └────────────┬────────────────────────────────┘
                                      │
                         ┌────────────▼────────────────────────────────┐
                         │ 7. default_delivery (se nessuna match)       │
                         │    o keep_original_delivery=true:            │
                         │    accoda outbound verso destinatario        │
                         │    originale.                                │
                         └────────────┬────────────────────────────────┘
                                      │
                         ┌────────────▼────────────────────────────────┐
                         │ 8. process_aggregations:                     │
                         │    contatori errori per fingerprint, soglie, │
                         │    apertura ticket aggregati.                │
                         └────────────┬────────────────────────────────┘
                                      │
                         ┌────────────▼────────────────────────────────┐
                         │ 9. insert_event:                             │
                         │    salva l'evento in `events` con la chain   │
                         │    completa di valutazione (regole valutate, │
                         │    matched/skipped, ragioni) e i metadata    │
                         │    delle azioni eseguite.                    │
                         └─────────────────────────────────────────────┘
```

### Cosa significa ciascuno step in pratica

**1. Ricezione SMTP** — Il listener accetta connessioni TCP/TLS sulla porta
25. Verifica handshake, gestisce STARTTLS se richiesto, applica eventuali
limiti dimensione configurati.

**2. Parsing** — Estrae i campi che servono al matching e all'azione. Marca
la mail come "auto/bulk" se ha header `Auto-Submitted`, `Precedence: bulk`,
List-ID, ecc. (importante per evitare loop sugli auto-reply). Calcola
`has_loop_marker` se trova un marker custom inserito dal nostro stesso
auto-reply nel passato.

**3. Resolve customer** — Decide a chi appartiene la mail:

- Prima cerca un `route` (alias o smarthost dominio) attivo per `to`.
- Se nessun route ha `codcli`, prova `find_customer_by_domain(from_domain)`.
- Carica `contract_active` e `service_hours_json` dal customers_cache.
- Calcola `in_service` confrontando ora/giorno corrente con lo schedule
  del cliente (con eventuale `schedule_exceptions` per oggi).

Risultato: un `CustomerContext` con `codcli`, `contract_active`, `in_service`,
`service_hours`. È quello che entra nel matching delle regole.

**4. Skip rules?** — Alcuni alias o domini sono marcati come "non passare per
le regole" (`apply_rules=false`). Esempio: una mailbox di test che vuole il
recapito normale senza interazioni con il rule engine. Se così configurato,
si salta al default delivery.

**5. Rule engine** — Vedi sezione 3 per il modello completo. Il listener
oggi valuta:

- `match_from_regex`, `match_to_regex`, `match_subject_regex`, `match_body_regex` (regex AND case-insensitive)
- `match_to_domain` (uguaglianza esatta lower-case)
- `match_in_service` (vincolo tristate sul `CustomerContext`)
- `scope_type`/`scope_ref` (grouping per ambito)

Gli altri match_* introdotti dalle migration 008/009 (`match_from_domain`,
`match_contract_active`, `match_known_customer`,
`match_has_exception_today`, `match_at_hours`, `match_tag`) sono presenti
nel payload ma **non sono ancora consumati dal listener**. Vengono usati
dall'admin per la simulazione e per i validatori. Il listener li ignora
(forward-compat).

**6. Dispatch action** — La regola vincente esegue l'azione. Le azioni più
ricche prendono parametri da `action_map`, che a partire dalla migration 010
è il **merge dell'action_map_defaults del gruppo padre + l'action_map del
figlio**. Esempio concreto:

```yaml
gruppo "Fuori orario contratto" (padre):
  action_map_defaults:
    keep_original_delivery: true
    also_deliver_to: ticket@domarc.it
    reply_mode: to_sender_only
    generate_auth_code: true

figlio "Auto-reply out_of_hours":
  action: auto_reply
  action_map: { template_id: 1, reply_subject_prefix: "Re: " }

  → action_map effettiva al runtime:
     template_id: 1                       ← figlio
     reply_subject_prefix: "Re: "         ← figlio
     keep_original_delivery: true         ← ereditato
     also_deliver_to: ticket@domarc.it    ← ereditato
     reply_mode: to_sender_only           ← ereditato
     generate_auth_code: true             ← ereditato
```

**7. Default delivery** — Se nessuna regola ha matchato (o
`keep_original_delivery=true` post-azione), accoda la mail in
`outbound_queue` per il recapito al destinatario originale.

**8. Aggregazioni errori** — In parallelo al rule engine, valuta
`error_aggregations`: pattern di fingerprint con soglia e finestra
temporale. Quando un pattern raggiunge la soglia (es. "5 messaggi dallo
stesso From con subject `(?i)backup failed` in 1 ora") apre un ticket
aggregato. Indipendente dalle regole standard.

**9. Audit log** — Tutto finisce in `events` con `action_taken`, `rule_id`,
`payload_metadata` (chain completa: priority, rule_id, matched, reasons per
ogni step). Da `/events` in UI puoi rivedere il path di valutazione di
qualsiasi mail entrata e cliccare per simulare cosa farebbe oggi una regola
nuova.

---

## 3. Il modello regole (gerarchia padre/figlio)

### Tre tipi di record `rules`

| Tipo | `is_group` | `parent_id` | `action` | Quando usarlo |
|------|------------|-------------|----------|---------------|
| **Orfana** | 0 | NULL | qualsiasi | Regola standalone (catch-all, skip mailer-daemon, log generico). |
| **Gruppo** (padre) | 1 | NULL | `group` | Aggrega match_* condivisi e action_map_defaults ereditabili. Non esegue azioni proprie. |
| **Figlio** | 0 | id_gruppo | qualsiasi | Variante d'azione concreta nel gruppo (es. auto_reply + create_ticket sotto un padre comune). |

Vincolo strutturale: **massimo 1 livello** di nesting. Un gruppo non può
contenere un altro gruppo (V008).

### Priority globale unica (1..999999)

Niente moltiplicazioni `padre*1000+figlio`. Ogni record dichiara una priority
assoluta sull'asse globale. Regole orfane e figli convivono nello stesso
spazio.

Vincoli applicativi (`V_PRI_RANGE`):

- la priority del figlio deve essere **strettamente maggiore** della
  priority del padre;
- la priority del figlio deve essere **strettamente minore** della priority
  del prossimo top-level (orfana o gruppo successivo).

Suggerimento (`W_PRI_GAP`): lasciare un gap di **almeno 10** fra i fratelli,
così puoi inserire un nuovo figlio senza dover rinumerare il blocco.

Esempio di scaletta sana:

```
prio  10  • orfana "Skip mailer-daemon / no-reply"
prio  20  • orfana "Skip notifiche di sistema interne domarc.it"
prio 100  ▾ gruppo "Fuori orario contratto"
prio 110     ├ child "Auto-reply out_of_hours"          (continue_in_group)
prio 120     └ child "Crea ticket NORMALE"              (STOP)
prio 200  ▾ gruppo "Monitoring critico"
prio 210     └ child "Crea ticket urgenza ALTA"
prio 999  • orfana "Catch-all log info"
```

### Ereditarietà `action_map`

Il gruppo padre fornisce **defaults** ereditabili dai figli — solo le chiavi
nella whitelist `PARENT_ACTION_MAP_DEFAULTS`:

- delivery: `keep_original_delivery`, `also_deliver_to`, `apply_rules`
- auto_reply: `reply_mode`, `reply_subject_prefix`, `reply_quote_original`,
  `reply_attach_original`, `reply_to`, `generate_auth_code`,
  `auth_code_ttl_hours`

Le chiavi figlio-only — `template_id`, `settore`, `urgenza`,
`addetto_gestione`, `forward_target`, `forward_port`, `forward_tls`,
`redirect_to`, `reason` — non sono ereditabili e vengono respinte se messe
sul gruppo (V003).

Regole di merge (in `domarc_relay_admin/rules/inheritance.py`):

- il figlio sovrascrive qualsiasi default del padre;
- valori `null`/vuoti del figlio NON sono override (significano "non
  specificato": il default del padre rimane);
- liste/CSV non vengono concatenate: `also_deliver_to` del figlio
  sostituisce completamente quella del padre.

### Flag di flusso (sui figli)

Tabella di verità del comportamento dopo che un figlio matcha:

| `continue_in_group` | `exit_group_continue` | Effetto |
|---|---|---|
| `False` | `False` | **STOP** dopo il match. Rispetta `exclusive_match` del gruppo. |
| `True` | qualsiasi | Valuta i fratelli successivi del gruppo. |
| `False` | `True` | Come `continue_in_group=True`, MA forza l'ultimo figlio del gruppo a propagare al top-level (utile per "esci dal blocco"). |

### `exclusive_match` (sul gruppo)

- Default `True` (gruppo "geloso"): dopo che un figlio matcha, i top-level
  successivi (orfane, altri gruppi) non vengono valutati.
- `False`: l'ultimo figlio matchato del gruppo propaga
  `continue_after_match=True` ai top-level successivi.

### Validatori bloccanti

| ID | Vincolo |
|---|---|
| **V001** | Un gruppo non può avere padre. |
| **V002** | Il padre referenziato deve essere un gruppo. |
| **V003** | I gruppi non eseguono azioni dirette; action_map del gruppo accetta solo chiavi della whitelist. |
| **V004** | Un gruppo deve avere almeno un `match_*` (catch-all gerarchico vietato). |
| **V005** | Niente riferimenti circolari (`parent_id == id`). |
| **V006** | Match incompatibili padre/figlio (es. `match_to_domain` diverso). |
| **V007** | Priority fuori range 1..999999. |
| **V008** | Un gruppo non può essere figlio (max 1 livello). |
| **V_PRI_RANGE** | Priority figlio strettamente fra padre e prossimo top-level. |

### Warning soft (non bloccanti)

| ID | Avvertimento |
|---|---|
| **W001** | Gruppo senza figli — nessun effetto a runtime. |
| **W002** | Figlio senza match_* propri — eredita solo dal padre (ok se intenzionale). |
| **W004** | Match ridondante (figlio ripete un valore identico al padre). |
| **W005** | Gruppo `exclusive_match=False` ma ultimo figlio STOP totale — ambiguo. |
| **W_PRI_GAP** | Distanza fra fratelli minore di 5/10. |

---

## 4. Gestione regole dalla UI

### Vista lista `/rules`

Tree view collassabile. Ciascuna riga è un'orfana (•) o l'header di un
gruppo (▾📁) con i suoi figli annidati. Sull'header del gruppo trovi:

- conteggio figli (`2 figli`),
- badge `esclusivo` se `exclusive_match=True`,
- riassunto match_* condivisi,
- pulsanti: modifica gruppo, aggiungi figlio, toggle attivo, elimina (cascade).

Sui figli e sulle orfane: pulsanti modifica, simula evento, toggle, elimina.
Sulle orfane in più: **Promuovi a gruppo** (con prompt etichetta).

In testa alla pagina i quick-link:

- **Nuova regola** — apre il form orfana standard.
- **Nuovo gruppo** — apre il form gruppo (vedi sotto).
- **Anteprima flatten** — mostra le regole flat che il listener riceverà.
- **Simulazione** — esegue una valutazione gerarchica su un evento sintetico.
- **Suggerisci gruppi** — wizard di clustering automatico.

### Form "Nuovo gruppo"

Tre sezioni:

1. **Identificazione**: etichetta visibile in UI, nome interno, priority globale,
   stato attivo, flag `exclusive_match`, scope.
2. **Match condivisi**: tutti i `match_*` che verranno ereditati dai figli. Sono
   l'unico punto di edit per filtri condivisi (DRY).
3. **Defaults action_map ereditabili**: solo le chiavi della whitelist.
   Sezioni "Delivery" e "Auto-reply defaults" con campi tipizzati.

Sotto, se il gruppo è già esistente, c'è la **lista figli** con CRUD inline.

### Form "Nuovo figlio"

Aperto da `/rules/groups/<id>/children/new` (o dal pulsante "+ Aggiungi
figlio" nel form gruppo). Quattro sezioni:

1. **Banner ereditarietà** read-only in alto: ricapitola match_* e action_map
   del padre. Aiuta a non duplicare campi.
2. **Identificazione**: nome, priority globale (default = `padre.priority + 10`),
   stato attivo, flag `continue_in_group` ed `exit_group_continue`.
3. **Match aggiuntivi** (AND col padre): tutti i `match_*`. Vuoti = eredita
   solo dal padre.
4. **Azione** + action_map specifica (action card visuali per i 7 tipi).
5. **Anteprima action_map effettiva**: blocco scuro con il merge finale; le
   chiavi ereditate dal padre sono evidenziate in azzurro.

### Promozione orfana → gruppo

Sulla riga di un'orfana, click su **Promuovi a gruppo**, inserisci
l'etichetta. Il sistema atomicamente:

1. Crea un nuovo record gruppo con i `match_*` dell'orfana e i defaults
   action_map (solo whitelist).
2. Sposta l'orfana sotto il nuovo gruppo come figlio (`parent_id` settato).
3. Lascia sull'orfana solo le action_map keys non ereditabili.

Operazione idempotente: se chiamata su una regola già con `parent_id`, ritorna
il `parent_id` esistente senza fare nulla.

### Wizard "Suggerisci gruppi"

Pagina `/rules/groupable-suggestions`. Il sistema cerca cluster di orfane con
**fingerprint match_* identici**. Per ciascun cluster mostra:

- match_* comuni,
- defaults action_map condivisi tra tutte le regole del cluster,
- etichetta proposta (heuristic: "Fuori orario clienti con contratto", ecc.),
- elenco regole nel cluster.

Click "Promuovi a gruppo" → la prima regola del cluster diventa "modello",
le altre vengono agganciate come figli del nuovo gruppo (operazione atomica).

### Simulazione evento

Pagina `/rules/simulate`. Compili un evento sintetico (from, to, subject,
body, to_domain) + contesto (`in_service`, `sector`). Il sistema chiama
`evaluate_v2` (engine gerarchico) e mostra:

- la chain di valutazione step-by-step (ogni gruppo, figlio, orfana — ✓
  matched o ✗ no);
- per ogni figlio matchato: l'action_map effettiva con merge padre+figlio;
- la lista delle azioni eseguite e l'eventuale "default delivery" se
  nessuna regola ha matchato.

### Anteprima flatten

Pagina `/rules/flatten-preview`. Tabella read-only con il payload esatto
servito a `GET /api/v1/relay/rules/active`. Ogni riga mostra:

- priority globale,
- origine (orfana o "da gruppo #X"),
- match_* mergiati,
- action_map ereditata + own (tutte le chiavi finali),
- flag `continue_after_match`.

Utile per debug pre-deploy: confrontare il "what the listener will see" con
quello che hai configurato in UI.

---

## 5. Configurazioni che incidono sul matching

Il rule engine non lavora isolato. Diverse anagrafiche del relay incidono
sull'esito di un match.

### Anagrafica clienti (Customer Source)

Origine: oggi `stormshield` (Manager Domarc). Cache locale in
`customers_cache` SQLite del listener. Per ciascun cliente abbiamo:

- `codice_cliente` (CODCLI),
- elenco `domains` (mittenti riconosciuti),
- `contract_active` (sì/no),
- `tipologia_servizio` → uno dei 4 profili canonici (standard / esteso /
  h24 / no servizio),
- `service_hours_json` (override schedule per cliente, se presente).

Nel `_resolve_customer` la pipeline:

1. Risolve `codcli` dall'alias di destinazione (route) o dal dominio del
   `from`.
2. Carica `contract_active`.
3. Calcola `in_service` confrontando ora/giorno/eccezioni con lo schedule.

Quindi se metti `match_in_service=fuori orario` su un gruppo, il match
dipende dall'anagrafica + ora. Cambiando il profilo orari su un cliente in
UI Manager, dopo il sync il listener vedrà `in_service` diverso per quel
cliente. **Le regole non cambiano**, cambia il contesto.

### Profili orari (Service Hours)

4 profili built-in:

| Codice | Nome | Orari (lavorativi) | Sabato | Domenica |
|---|---|---|---|---|
| `STD` | Standard | lun-ven 09:00–13:00, 14:00–18:00 | chiuso | chiuso |
| `EXT` | Esteso | lun-ven 08:00–20:00 | 09:00–13:00 | chiuso |
| `H24` | H24 | sempre attivo | sempre | sempre |
| `NO` | Senza servizio | mai | mai | mai |

Sono modificabili dalla UI **Profili orari**. Vengono ereditati dai clienti
senza override; quando un cliente ha `service_hours_json` valorizzato, quello
prevale (override per-cliente).

`schedule_exceptions` permette di gestire chiusure straordinarie (festività
locali, ponti) o aperture eccezionali (assistenza programmata).

### Indirizzi (`addresses_from`, `addresses_to`)

Inventario degli indirizzi visti dal relay. Permettono:

- pre-classificazione di un mittente come cliente noto (`codcli` linkato),
- block list manuale (`blocked=1`) per spam evidenti,
- contatori `seen_count` per analytics.

Il rule engine usa indirettamente `addresses_from` quando la pipeline
risolve `codcli` da un alias o dominio noto.

### Routes (alias / smarthost dominio)

Tabella `routes` + `domain_routing`. Permettono di:

- intercettare un alias (`info@cliente.it`) e farlo finire altrove
  (forward, redirect),
- forzare uno smarthost dedicato per un dominio specifico,
- attivare/disattivare il rule engine per quel route (`apply_rules`).

Se un route ha `apply_rules=false`, la pipeline al passo 4 salta direttamente
al default delivery (passo 7), senza valutare le regole.

### Templates (`reply_templates`)

I template per `auto_reply` sono editabili da UI **Templates**. Variabili
disponibili in `body_html_tmpl` / `body_text_tmpl`:

- `{from_name}`, `{from_address}`, `{subject}`, `{message_id}`
- `{customer_name}`, `{codcli}`
- `{auth_code}` (se generato), `{auth_code_url}`
- `{service_hours_summary}`, `{next_in_service_at}`

`attachment_paths` permette di allegare file fissi (logo, PDF brochure).

### Tenant

Il sistema è multi-tenant. Tutto è scopato per `tenant_id`: regole, route,
template, indirizzi, profili. Il superadmin può switchare tenant via header
(query string `?tenant_id=N` mantenuto in sessione). Gli operator/admin sono
bloccati al loro `users.tenant_id`.

---

## 6. Esempi end-to-end commentati

### Esempio A — "Fuori orario clienti con contratto"

Use case: ogni mail in arrivo a `*@domarc.it` da un cliente noto con contratto
attivo e fuori dall'orario di servizio deve:

1. ricevere un auto-reply "siamo chiusi, abbiamo aperto un ticket";
2. aprire un ticket a urgenza NORMALE in coda assistenza;
3. essere comunque recapitata al destinatario originale per visibilità;
4. essere copiata a `ticket@domarc.it` per audit.

#### Configurazione gerarchica

**Gruppo** "Fuori orario contratto" (`is_group=1`, `priority=500`):
- match_to_domain = `domarc.it`
- match_in_service = `0` (fuori orario)
- match_contract_active = `1`
- match_known_customer = `1`
- exclusive_match = `True`
- action_map_defaults = `{ keep_original_delivery: true, also_deliver_to: "ticket@domarc.it", reply_mode: "to_sender_only", generate_auth_code: true, auth_code_ttl_hours: 12 }`

**Figlio #1** "Auto-reply out_of_hours" (`priority=510`):
- action = `auto_reply`
- action_map = `{ template_id: 1, reply_subject_prefix: "Re: " }`
- continue_in_group = `True`

**Figlio #2** "Crea ticket NORMALE" (`priority=520`):
- action = `create_ticket`
- action_map = `{ settore: "assistenza", urgenza: "NORMALE" }`
- continue_in_group = `False`, exit_group_continue = `False`

#### Cosa succede a runtime

Mail da `mario@cliente.com` a `assistenza@domarc.it` alle 23:00 di sabato:

1. **Resolve customer**: `cliente.com` è nel customers_cache, ha
   `contract_active=1` e profilo `STD`. Sabato 23:00 → `in_service=False`.
2. **Rule engine** valuta orfane in priority < 500 (skip mailer-daemon, ecc.):
   nessuna matcha.
3. Padre prio 500 → match (to_domain ✓, in_service=0 ✓, contract_active=1 ✓).
4. Figlio prio 510 → match (nessun match_* aggiuntivo) → esegue `auto_reply`
   con action_map mergiata; `continue_in_group=True` → continua nei fratelli.
5. Figlio prio 520 → match → esegue `create_ticket`; STOP.
6. `keep_original_delivery=true` (ereditato dal padre): la mail viene anche
   recapitata a `assistenza@domarc.it`.
7. `also_deliver_to=ticket@domarc.it` (ereditato): copia anche a quell'indirizzo.

Eventi loggati: 1 evento principale con `action_taken=create_ticket` (ultima
azione "vincente"), `payload_metadata.chain` mostra il path completo.

### Esempio B — "Skip rumore generico" + "Catch-all log"

Use case: ignorare totalmente i mailer-daemon e le notifiche automatiche di
servizi interni, ma conservare un log generico per ogni altra mail.

#### Configurazione

**Orfana** "Skip mailer-daemon / no-reply" (`priority=10`):
- match_from_regex = `(?i)(mailer-daemon|no-reply|noreply|postmaster)@`
- action = `ignore`
- continue_after_match = `False`

**Orfana** "Skip notifiche interne domarc.it" (`priority=20`):
- match_from_domain = `domarc.it`
- match_subject_regex = `(?i)^\[(monitor|alert|cron|backup)\]`
- action = `ignore`
- continue_after_match = `False`

[Eventuali gruppi e altre orfane in mezzo, prio 100..900]

**Orfana** "Catch-all log" (`priority=999`):
- match_to_regex = `.*` (oppure tutti vuoti)
- action = `flag_only`
- continue_after_match = `False`

In questo modo le mail "rumorose" vengono scartate subito (priority bassa),
le mail vere passano per la logica del business (gruppi/figli), e tutto ciò
che non ha matchato nulla finisce nel log generico in fondo.

### Esempio C — "Forward a Libraesva per quarantena selettiva"

Use case: in finestra notturna, forwardare le mail di un dominio specifico
verso un gateway anti-spam esterno (Libraesva ESG) anziché recapitare
direttamente.

#### Configurazione

**Orfana** "Forward notturno a ESG" (`priority=300`):
- match_to_domain = `cliente-rischioso.it`
- match_at_hours = `mon-fri=22:00-06:00;sat=*;sun=*`
- action = `forward`
- action_map = `{ forward_target: "esg.cliente.com", forward_port: 25, forward_tls: "opportunistic" }`
- continue_after_match = `False`

> Nota: `match_at_hours` è presente nello schema ma non ancora consumato dal
> listener. Per scenari time-based oggi va usato `match_in_service` con un
> profilo `service_hours` adatto al cliente.

---

## 7. Operatività e troubleshooting

### "Ho cambiato una regola in UI ma il listener non la applica"

Il listener cache-a le regole. Tipicamente il sync è di pochi minuti. Per
forzare:

```bash
sudo systemctl restart stormshield-smtp-relay
```

Verifica che l'admin stia esponendo le regole aggiornate:

```bash
KEY=$(sudo sqlite3 /var/lib/domarc-smtp-relay-admin/admin.db \
      "SELECT value FROM settings WHERE key='relay_api_key'")
curl -s -H "X-API-Key: $KEY" http://127.0.0.1:5443/api/v1/relay/rules/active \
     | jq '.rules | length'
```

### "Voglio capire perché una mail non ha matchato la regola che mi aspettavo"

1. Vai su `/events`, trova la mail.
2. Apri il dettaglio: `payload_metadata.chain` ha la lista completa di tutte
   le regole valutate, con `matched=false` e la `reason` (es. "match_subject_regex:
   no match", "match_to_domain 'domarc.it': no (got 'datia.it')").
3. Se la chain non riporta nemmeno la regola che ti aspettavi, è perché il
   listener non l'aveva ancora cache-ata o perché aveva `enabled=0` al
   momento.
4. Per testare al volo cosa farebbe oggi quella regola: pagina
   `/rules/simulate`, copia from/to/subject/body dalla mail, premi "Esegui
   simulazione". Vedi step by step il flow.

### "Ho aggiunto un gruppo ma non vedo niente nella vista flatten"

Controlla:

- gruppo `enabled=1`?
- gruppo ha almeno un figlio `enabled=1`? Un gruppo senza figli abilitati
  non genera nessuna riga flat (W001).
- in `/rules/flatten-preview` ti aspetti **una riga flat per ogni figlio
  abilitato**, non una riga per il gruppo.

### "L'action_map che vedo nel listener ha più chiavi di quelle che ho messo"

È intenzionale. Sono le chiavi ereditate dal padre. Verifica nella riga
flatten preview (colonna `action_map ereditata + own`): le chiavi che non
hai messo nel figlio vengono dal gruppo padre.

Se vuoi azzerare un default ereditato per un singolo figlio, devi
sovrascriverlo esplicitamente con un valore neutro (es.
`keep_original_delivery=false` esplicito sul figlio).

### "Mi serve far passare una regola SOPRA un'altra dello stesso blocco"

Cambia la priority. Lo spazio è 1..999999, c'è ampio margine. Se ti trovi
con priority adiacenti (es. fratelli a 510 e 511), assegna alla nuova un
valore intermedio fra il padre (500) e il prossimo top-level (es. 600).

Se vedi il warning `W_PRI_GAP` significa che hai meno di 5/10 di gap fra
fratelli. Considera di rinumerare quel blocco con gap di 10.

### "Voglio disabilitare temporaneamente un intero blocco"

Toggle del gruppo (`enabled=0`). Tutti i figli vengono automaticamente
esclusi dalla flatten (il flatten skippa il gruppo intero quando il padre è
disabilitato).

### "Devo cancellare un gruppo ma ho figli importanti"

L'eliminazione del gruppo in UI cancella **a cascata** tutti i suoi figli
(constraint `ON DELETE CASCADE` della migration 010). Conferma manuale
richiesta nel dialog.

Per conservare i figli come orfane: prima sposta ciascun figlio
manualmente cambiando il `parent_id` (oggi via SQL diretto, in UI futura
sarà drag-drop). Poi cancella il gruppo vuoto.

### "Il wizard non mi suggerisce nulla anche se ho regole simili"

Il wizard cerca **fingerprint match_* identici** (case-insensitive sui
domini, valore esatto sui tristate). Se due tue regole hanno
`match_subject_regex` simile ma non identico (es. una usa `(?i)urgente`
l'altra `(?i)urgent`), il wizard non le trova. È intenzionale per evitare
falsi positivi. Promuovi manualmente la prima e poi attacca le altre come
figli, oppure uniforma i regex.

### "Il test di parità del rule engine v2 fallisce dopo che ho modificato qualcosa"

Lancia:

```bash
cd /opt/domarc-smtp-relay-admin
.venv/bin/python3 -m pytest tests/ -v
```

Lo `tests/test_rule_engine_parity.py` è il **gate**: se fallisce, una tua
modifica ha rotto l'equivalenza fra valutazione gerarchica (admin) e
valutazione flat (listener legacy). Tipicamente la causa è una modifica a
`flatten.py`, `evaluator.py`, o `legacy_evaluator.py` non sincronizzata con
le altre. Vedi il diff `legacy=[...]` vs `v2=[...]` nel messaggio del test
e ragiona su quale lato è "giusto".

---

## 8. Riferimenti rapidi

### File chiave

- DAO + flatten: [domarc_relay_admin/storage/sqlite_impl.py](../domarc_relay_admin/storage/sqlite_impl.py)
- Rule engine v2: [domarc_relay_admin/rules/](../domarc_relay_admin/rules/)
- Routes UI: [domarc_relay_admin/routes/rules.py](../domarc_relay_admin/routes/rules.py)
- Endpoint listener: [domarc_relay_admin/routes/api.py](../domarc_relay_admin/routes/api.py)
- Migration 010: [domarc_relay_admin/migrations/010_rule_groups.sqlite.sql](../domarc_relay_admin/migrations/010_rule_groups.sqlite.sql)
- Listener pipeline: `/opt/stormshield-smtp-relay/relay/pipeline.py`
- Listener rule engine legacy: `/opt/stormshield-smtp-relay/relay/rules.py`
- Test parità (gate): [tests/test_rule_engine_parity.py](../tests/test_rule_engine_parity.py)

### Comandi utili

```bash
# Stato servizi
systemctl status domarc-smtp-relay-admin stormshield-smtp-relay

# Tail log admin
journalctl -u domarc-smtp-relay-admin -f

# Tail log listener
journalctl -u stormshield-smtp-relay -f

# Lancia migrazioni (admin)
sudo -u domarc-relay /opt/domarc-smtp-relay-admin/.venv/bin/domarc-smtp-relay-admin migrate

# Backup admin DB
cp /var/lib/domarc-smtp-relay-admin/admin.db \
   /opt/domarc-smtp-relay-admin/backups/admin.db.$(date +%Y%m%d-%H%M%S)

# Test suite
cd /opt/domarc-smtp-relay-admin && .venv/bin/python3 -m pytest tests/ -v

# Verifica payload listener
KEY=$(sudo sqlite3 /var/lib/domarc-smtp-relay-admin/admin.db \
      "SELECT value FROM settings WHERE key='relay_api_key'")
curl -s -H "X-API-Key: $KEY" http://127.0.0.1:5443/api/v1/relay/rules/active | jq .
```

### Indirizzi di test

Per qualunque test SMTP/IMAP usare **solo**:

- `r.grandi@domarc.it`
- `r.grandi@datia.it`

Non inviare mai test verso `info@`, `monitoring@` o altri indirizzi generici
@domarc — verrebbero recapitati in caselle reali con effetti collaterali.

---

*Ultimo aggiornamento: 2026-04-29 — post Rule Engine v2 (migration 010, Fasi 1-4 della roadmap).*
