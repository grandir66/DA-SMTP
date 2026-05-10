---
applies_to: domarc_relay_admin/routes/**/*.py
---

# Flask routes — direttive

## Auth

- Ogni endpoint HTML/UI: `@login_required(role="admin|operator|viewer|superadmin")`.
- Ogni endpoint API esterno (`/api/v1/relay/*`, chiamato dal listener): `@require_api_key`.
- Validare permessi nel **backend**, non solo nei template (un hidden link in HTML non protegge l'endpoint).
- Per superadmin che opera su tenant diversi: rispettare `g.tenant_id` corrente, non hardcodare `tenant_id=1`.

## CSRF

- Tutti i form POST HTML hanno `<input type="hidden" name="csrf_token" value="{{ csrf_token() }}">`.
- Blueprint che espongono API REST con X-API-Key vanno esentati: `csrf.exempt(blueprint)` in `app.py`. Mai disabilitare CSRF globalmente.
- Endpoint AJAX da JS interno: leggere il token da `<meta name="csrf-token">` e inviarlo come header `X-CSRFToken`.

## Validation

- Input edge boundary (form, query, body JSON): validare TIPO e LUNGHEZZA prima dell'uso.
- Regex utente forniti (`match_subject_regex`, `match_from_regex`, ecc.): `re.compile()` PRIMA del save in DB, errore → flash + redirect.
- Numerici: cast esplicito `int(request.form["x"])` dentro try/except, mai assumere stringhe siano numeri.

## Errori

- Mai esporre traceback nel response (`debug=False` in produzione, già di default).
- Log strutturato: `current_app.logger.error("descrizione", extra={"key": val})`, mai `print()`.
- Errori user-facing in italiano (`flash("Errore: ...", "error")`), coerenti con tono UI.

## Pattern di routing

- File `routes/<blueprint>.py` registra `<blueprint>_bp = Blueprint(...)`. Import e `app.register_blueprint(...)` in `app.py:create_app`.
- URL kebab-case (`/customer-sync/sources/<id>/edit`), parameter snake_case.
- Una route = una responsabilità. Logica DB in `storage/sqlite_impl.py`, logica business in modulo dedicato, route fa solo: parse input → chiama service → render/redirect.
