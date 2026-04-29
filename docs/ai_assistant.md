# AI Assistant — guida operatore

Modulo `ai_assistant` introdotto con la migration 012. Affianca il rule
engine deterministico esistente per task che richiedono comprensione
semantica: classificazione mail, summary, dedup errori, learning loop di
proposte regole.

## Architettura

```
Listener regola action="ai_classify"
        │
        ▼ POST /api/v1/relay/ai/classify
        │ (X-API-Key)
        ▼
  ai_assistant.classify_email(event, ctx)
        │
        ├─▶ pii_redactor (regex + spaCy + dictionary)
        │
        ├─▶ AiRouter.pick_binding(job_code)
        │       └─▶ traffic split A/B fra binding attivi
        │
        ├─▶ Provider (Claude API / DGX local)
        │       └─▶ structured output via tool_use o response_format
        │
        ├─▶ insert_ai_decision()  ← log per audit/KPI/learning
        │
        └─▶ return {classification, urgenza, summary, ...}
```

## Componenti chiave

| Componente | Path | Scopo |
|---|---|---|
| Provider Claude | `ai_assistant/providers/claude_provider.py` | Anthropic SDK con prompt caching + structured output. |
| Provider Local | `ai_assistant/providers/local_http_provider.py` | OpenAI-compatible (DGX Spark, Ollama, vLLM). F4. |
| Router | `ai_assistant/router.py` | Lookup binding per job_code, traffic split, render prompt. |
| PII Redactor | `ai_assistant/pii_redactor.py` | Rimozione PII PRIMA di inviare a Claude. |
| Decisions | `ai_assistant/decisions.py` | Orchestrazione `classify_email` con shadow mode + budget. |
| Prompt template | `ai_assistant/prompts/*.j2` | Jinja2, sezioni `## SYSTEM` e `## USER`. |
| DAO | `storage/sqlite_impl.py` | Metodi `*_ai_*` per le 7 tabelle. |
| UI | `routes/ai.py` + `templates/admin/ai_*.html` | 7 viste UI in `/ai/*`. |

## Concetti

### Job + Binding (routing per job)

Ogni "lavoro IA" ha un **job_code** stabile (`classify_email`, `summarize`,
`critical_classify`, ecc.). Il **binding** assegna il job a un provider+modello.

- I bindings sono **versionati**: salvando come "nuova versione" si crea v(n+1)
  e si disabilitano le precedenti (rollback con un click).
- Più binding attivi sullo stesso job = **A/B test** via `traffic_split`. Es.
  Haiku 80% + Sonnet 20% per confronto qualità/costo prima di committarsi.

### Shadow mode

Setting `ai_shadow_mode=true` (default): le decisioni vengono **loggate** ma
**non applicate**. Per andare live: setting `ai_shadow_mode=false`. La modifica
è un punto di switch atomico tracciato nei log.

### Master switch + Budget cap

- `ai_enabled=false` (default) — il modulo è disabilitato. Le action `ai_*`
  vengono saltate (fail-safe) anche se le regole le richiamano.
- `ai_daily_budget_usd=50` — budget giornaliero. Quando raggiunto, ulteriori
  chiamate IA falliscono con `budget_exhausted`.

### PII Redactor

Tre stadi prima di inviare il testo al provider:

1. **Regex deterministici**: IBAN, CF, P.IVA, telefono italiano, email, IPv4,
   URL con token. Sempre attivi, zero dipendenze.
2. **Signature stripping**: rimuove tutto da "Cordialmente"/"Distinti
   saluti"/"-- " in poi.
3. **spaCy NER** (opzionale, italiano `it_core_news_sm`): nomi propri (PER),
   organizzazioni (ORG), luoghi (LOC). Se non installato, lo step viene
   saltato con un warning (graceful fallback).
4. **Dizionario custom** in `ai_pii_dictionary` per termini specifici
   appresi nel tempo.

## Setup operativo

### 1. Installare le dipendenze

```bash
sudo -u domarc-relay /opt/domarc-smtp-relay-admin/.venv/bin/pip install anthropic

# spaCy (opzionale ma consigliato per redaction nomi italiani)
sudo -u domarc-relay /opt/domarc-smtp-relay-admin/.venv/bin/pip install spacy
sudo -u domarc-relay /opt/domarc-smtp-relay-admin/.venv/bin/python -m spacy download it_core_news_sm
```

### 2. Configurare la API key Anthropic

In `/etc/domarc-smtp-relay-admin/secrets.env` aggiungi:

```
ANTHROPIC_API_KEY=sk-ant-api03-...
```

Riavvia: `systemctl restart domarc-smtp-relay-admin`.

### 3. Configurare il provider in UI

`/ai/providers` → **Nuovo provider**:

- Nome: `Claude API`
- Kind: `claude`
- Endpoint: vuoto (default Anthropic)
- API key env: `ANTHROPIC_API_KEY`
- Default model: `claude-haiku-4-5`
- Stato: attivo

Click **test connettività** → deve restituire OK.

### 4. Configurare i binding

`/ai/models` → **Nuovo binding**:

- Job: `classify_email`
- Provider: `Claude API`
- Model id: `claude-haiku-4-5`
- Temperature: `0.0`
- Max tokens: `500`
- Timeout (ms): `5000`
- Fallback provider: vuoto (o stesso, model `claude-sonnet-4-6`)
- Traffic split: `100`
- Stato: attivo

Ripeti per altri job man mano che servono (`summarize_email`,
`critical_classify`, ecc.).

### 5. Attivare il modulo

In `/settings`:

- `ai_enabled` → `true` (master switch)
- `ai_shadow_mode` → `true` (consigliato per i primi giorni)
- `ai_daily_budget_usd` → `50` (o quanto preferisci)

### 6. Fase shadow

Per i primi giorni il modulo classifica le mail ma le decisioni non vengono
applicate. Ispeziona `/ai/decisions` per vedere cosa avrebbe fatto. Quando sei
soddisfatto della qualità, metti `ai_shadow_mode=false` per andare live (F3).

## Costo Claude

Pricing al 2026-04 (per 1M token, modificabile in `claude_provider.py`):

| Modello | Input | Output |
|---|---|---|
| claude-haiku-4-5 | $1.00 | $5.00 |
| claude-sonnet-4-6 | $3.00 | $15.00 |
| claude-opus-4-7 | $15.00 | $75.00 |

Stima con Haiku per una classify_email tipica (~400 input + 80 output token):

- per mail: ~$0.0008
- per 1.000 mail: ~$0.80
- per 10.000 mail/mese: ~$8

Con prompt caching attivo (system prompt cached) l'input cost si riduce
ulteriormente.

## Troubleshooting

### "ai_disabled" in tutte le decisioni
→ Setting `ai_enabled=false`. Imposta a `true`.

### "budget_exhausted"
→ La spesa giornaliera ha raggiunto `ai_daily_budget_usd`. Aumenta il budget
o aspetta il reset alle 00:00 UTC.

### "no_binding_configured"
→ Nessun binding attivo per quel job_code. Vai in `/ai/models` e creane uno.

### Provider test → "API key mancante"
→ La env var non è caricata. Verifica `/etc/domarc-smtp-relay-admin/secrets.env`
e riavvia il servizio.

### Decisioni con `error="..."`
→ Inspeziona `/ai/decisions/<id>` per il dettaglio. Cause comuni:
- timeout (ridurre dimensione body o aumentare timeout_ms)
- rate limit Anthropic (configura fallback a Sonnet o Opus, oppure attendi)
- modello non valido (typo nel `model_id`)

### spaCy non disponibile
Il warning nei log dice `PII redactor: spaCy non disponibile`. Il modulo
funziona comunque con solo regex. Per attivare NER nomi italiani, vedi
[setup §1](#1-installare-le-dipendenze).

## Roadmap

- **F1** ✅ Foundation + routing per job + UI minimal + shadow mode default.
- **F1.5**: action `do_ai_classify` lato listener (chiamata sync 5s +
  fail-safe forward). Documenterà come integrare nel rule engine.
- **F2**: error aggregator semantico (sentence-transformers + clustering)
  per sostituire `error_aggregations` rigide.
- **F3**: rule proposer (learning loop) + uscita da shadow mode con switch
  atomico.
- **F4**: provider DGX Spark self-hosted in coesistenza con Claude API,
  routing per job permette di mettere ogni task sul provider ottimale
  (locale per alta frequenza, Claude per task sofisticati).

## Riferimenti

- Migration: [migrations/012_ai_assistant.sqlite.sql](../domarc_relay_admin/migrations/012_ai_assistant.sqlite.sql)
- Plan completo: [/root/.claude/plans/nel-sisteam-presente-concurrent-crescent.md](../../../root/.claude/plans/nel-sisteam-presente-concurrent-crescent.md)
- Guida funzionamento generale: [docs/guida_funzionamento.md](guida_funzionamento.md)
