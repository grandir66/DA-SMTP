---
applies_to: domarc_relay_admin/ai_assistant/**/*.py, services/smtp_listener/relay/actions.py
---

# AI payload — direttive

## PII redaction obbligatoria

- Tutto ciò che va a un provider AI (Anthropic Claude, DGX, futuri) **DEVE** passare attraverso `ai_assistant/pii_redactor.py` prima.
- Categorie da redigere: email indirizzi, telefoni, codici fiscali/P.IVA, IBAN, indirizzi postali, nomi propri se non già anonimizzati a monte.
- I redattori sostituiscono con token semantici (`[EMAIL_1]`, `[PHONE_2]`) mantenendo la coerenza intra-prompt.

## Cosa NON inviare

- **Mai** `secrets.env`, master.key, API key, password.
- **Mai** body mail di clienti in privacy bypass (la lista esiste per questo).
- **Mai** subject/body completi di mail che contengono allegati riservati (PDF buste paga, referti, contratti).
- **Mai** dati clienti raw del PG `solution` (anagrafica, indirizzi, fatturato).

## Provider routing

- Provider configurati in `/ai/providers` (Anthropic, DGX, futuri). Routing per `job_code` → `provider + model` in `/ai/models` con A/B traffic split.
- Nuove integrazioni: implementare interfaccia in `ai_assistant/providers/`, registrare in tabella `ai_providers`.
- Mai hardcodare model ID o endpoint nel codice di chiamata — leggere da DB.

## Audit

- Ogni chiamata AI logga: timestamp, provider, model, job_code, input_token, output_token, latency_ms, eventuale errore.
- Per chiamate `ai_classify` su mail: persistere `prompt_hash` e `response_hash` (non il contenuto) per traceback senza leak PII.

## Costo e rate limit

- Job batch (>10 chiamate): preferire batch API del provider (Anthropic Message Batches) se disponibile.
- Cache prompt statici (system prompt lunghi) via prompt caching del provider.
- Fallback su provider secondario in caso di 429/5xx, mai loop infinito di retry.
