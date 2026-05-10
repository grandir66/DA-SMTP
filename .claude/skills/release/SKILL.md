---
name: release
description: Bump versione pyproject + tag git + voce CHANGELOG + push
---

# Release

Versioning manuale (no release-please / semantic-release). Source of truth: `pyproject.toml [project].version` + tag git `vX.Y.Z[-suffix]` + sezione `CHANGELOG.md`.

## Comando standard

```bash
# 1. Determina il bump (patch | minor | major) — guida dal CHANGELOG non rilasciato
NEW_VERSION="0.9.5"      # adatta

# 2. Bump pyproject
sed -i "s/^version = \".*\"/version = \"${NEW_VERSION}\"/" pyproject.toml
grep '^version' pyproject.toml

# 3. Aggiorna CHANGELOG.md (Keep a Changelog, data ISO YYYY-MM-DD, lingua italiana)
#    Sposta voci da "Non rilasciato" → "[X.Y.Z] - YYYY-MM-DD"
#    Sezioni: Aggiunte / Modifiche / Ottimizzazioni / Correzioni
$EDITOR CHANGELOG.md

# 4. Commit release
git add pyproject.toml CHANGELOG.md
git commit -m "chore(release): v${NEW_VERSION}"

# 5. Tag annotato + push
git tag -a "v${NEW_VERSION}" -m "Release v${NEW_VERSION}"
git push origin main --follow-tags
```

## Quando NON usare

- Per modifiche solo a `docs/` o documentazione interna: nessun bump versione.
- Tag `pre-prod` o `rc`: usare suffix `v0.9.5-rc1` / `v0.9.5-pre-prod` (non bumpare la version base finché non è stable).

## Anti-regressione

- **CHANGELOG sempre in italiano**, formato Keep a Changelog, date ISO `YYYY-MM-DD`.
- Nessun bump versione senza voce CHANGELOG corrispondente (i tag senza changelog rendono il rollback opaco).
- Mai usare `git push --force` su `main` (warning automatico). Per correzioni: nuovo commit `revert:` o `fix:`, mai amend del tag pubblicato.
- Verifica che `.venv/bin/pytest` sia verde PRIMA del tag. Un tag che non passa i test inquina la storia.
