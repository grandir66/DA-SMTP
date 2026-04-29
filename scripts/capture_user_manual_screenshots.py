#!/usr/bin/env python3
"""Cattura screenshot delle pagine principali per il manuale utente.

Usa Playwright (chromium headless) per fare login e screenshottare le
viste chiave. I file PNG vengono salvati in ``docs/manuale_utente/img/``.

Esecuzione:
    python3 scripts/capture_user_manual_screenshots.py

Richiede: server attivo su https://manager-dev.domarc.it:8443.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE_URL = os.environ.get("DOMARC_BASE_URL", "https://manager-dev.domarc.it:8443")
USERNAME = os.environ.get("DOMARC_USER", "admin")
PASSWORD = os.environ.get("DOMARC_PASS", "")  # NON hardcodato: passa via env
OUT_DIR = Path(__file__).resolve().parent.parent / "docs" / "manuale_utente" / "img"

# Lista (path_relativo, filename, descrizione, eventuale azione_post_load)
PAGES: list[tuple[str, str, str]] = [
    ("/", "01_dashboard.png", "Dashboard principale"),
    ("/rules", "02_rules_list.png", "Elenco regole"),
    ("/customers", "03_customers_list.png", "Anagrafica clienti"),
    ("/events", "04_events_list.png", "Cronologia eventi mail"),
    ("/queue/", "05_queue.png", "Coda outbound + quarantena"),
    ("/activity/", "06_activity_live.png", "Activity live realtime"),
    ("/ai/", "07_ai_dashboard.png", "Dashboard IA"),
    ("/ai/decisions", "08_ai_decisions.png", "Decisioni IA"),
    ("/ai/clusters", "09_ai_clusters.png", "Cluster errori IA"),
    ("/ai/proposals", "10_ai_proposals.png", "Proposte regole IA"),
    ("/ai/models", "11_ai_models.png", "Routing modelli IA per job"),
    ("/ai/providers", "12_ai_providers.png", "Provider IA"),
    ("/profiles", "13_profiles.png", "Profili orari"),
    ("/service-hours", "14_service_hours.png", "Orari clienti + eccezioni"),
    ("/templates", "15_templates.png", "Template di reply"),
    ("/users", "16_users.png", "Utenti & ruoli"),
    ("/manual", "17_technical_manual.png", "Manuale tecnico auto-generato"),
]


def main() -> int:
    if not PASSWORD:
        print("[ERROR] Password admin non impostata: esporta DOMARC_PASS=<pwd>",
              file=sys.stderr)
        return 2
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            ignore_https_errors=True,
            viewport={"width": 1440, "height": 900},
        )
        page = context.new_page()

        # === Login ===
        print(f"[login] {BASE_URL}/login")
        page.goto(f"{BASE_URL}/login", wait_until="networkidle")
        page.fill('input[name="username"]', USERNAME)
        page.fill('input[name="password"]', PASSWORD)
        page.click('button[type="submit"]')
        page.wait_for_load_state("networkidle")

        if "/login" in page.url:
            print("[ERROR] Login fallito", file=sys.stderr)
            return 1

        # Screenshot della login PRIMA di proseguire
        page.goto(f"{BASE_URL}/logout", wait_until="networkidle")
        page.goto(f"{BASE_URL}/login", wait_until="networkidle")
        page.screenshot(path=str(OUT_DIR / "00_login.png"), full_page=False)
        print(f"[ok] 00_login.png")

        # Re-login
        page.fill('input[name="username"]', USERNAME)
        page.fill('input[name="password"]', PASSWORD)
        page.click('button[type="submit"]')
        page.wait_for_load_state("networkidle")

        # === Pagine ===
        for path, fname, desc in PAGES:
            url = f"{BASE_URL}{path}"
            try:
                page.goto(url, wait_until="networkidle", timeout=15000)
                # Aspetta che eventuali JS finiscano
                page.wait_for_timeout(800)
                out = OUT_DIR / fname
                page.screenshot(path=str(out), full_page=True)
                print(f"[ok] {fname} ← {desc}")
            except Exception as exc:
                print(f"[ERR] {fname}: {exc}", file=sys.stderr)

        browser.close()
    print(f"\nScreenshot salvati in: {OUT_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
