#!/usr/bin/env python3
"""Anonimizzazione automatica degli screenshot del manuale utente.

Applica blur a colonne sensibili (email reali, codici cliente, oggetti,
mittenti, codici monouso/permanenti) di una serie di screenshot generati
da ``capture_user_manual_screenshots.py``.

Strategia:
  - Per ogni screenshot → crop verticale (taglio sotto i KPI/filtri,
    mantenendo qualche riga di tabella per scopi didattici)
  - Blur GaussianBlur(radius=22) su colonne identificate per coordinate
  - Overlay grigio semi-opaco per sicurezza extra
  - Banner "DATI ANONIMIZZATI A SCOPO DIDATTICO" in alto a destra

Uso:
    python3 scripts/anonymize_screenshots.py

Sostituisce in-place i file in ``docs/manuale_utente/img/``.

Nota: lo script è permissivo — se un file non esiste lo salta senza errore.
La definizione delle "zone sensibili" è specifica per il viewport 1440x900.
Se cambi il viewport rigenera anche queste coordinate.
"""
from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

IMG_DIR = Path(__file__).resolve().parent.parent / "docs" / "manuale_utente" / "img"

# Per ogni file: (crop_height_max, lista zone sensibili (x0, y_top, x1, y_bottom))
# Le zone vengono blurrate + overlay grigio. y_bottom=None → fino al fondo del crop.
ANON_PLAN: dict[str, tuple[int, list[tuple[int, int, int, int | None]]]] = {
    "03_customers_list.png": (
        1100,
        [
            (25, 320, 155, None),    # codice cliente
            (155, 320, 525, None),   # ragione sociale
            (640, 320, 1130, None),  # domini & alias
        ],
    ),
    "04_events_list.png": (
        1100,
        [
            (25, 320, 360, None),    # mittente
            (360, 320, 700, None),   # destinatario
            (700, 320, 1100, None),  # oggetto
        ],
    ),
    "06_activity_live.png": (
        1100,
        [
            (25, 280, 380, None),    # mittente
            (380, 280, 700, None),   # destinatario
            (700, 280, 1100, None),  # subject
        ],
    ),
    "08_ai_decisions.png": (
        1100,
        [
            (25, 320, 380, None),    # event/from
            (380, 320, 760, None),   # subject
            (760, 320, 1080, None),  # summary AI
        ],
    ),
    "09_ai_clusters.png": (
        1100,
        [
            (25, 320, 540, None),    # representative subject
        ],
    ),
    "18_addresses_to.png": (
        1100,
        [
            (60, 380, 480, None),    # email destinatario
            (480, 380, 760, None),   # codcli
        ],
    ),
    "19_recipient_groups.png": (
        # Solo banner — non mostra membri qui
        900,
        [],
    ),
    "20_h24_codes_list.png": (
        1100,
        [
            (25, 280, 200, None),    # codice permanente
            (200, 280, 460, None),   # codcli + ragione sociale
        ],
    ),
    "21_h24_targets.png": (
        1100,
        [
            # Source email/domain potrebbe essere un dominio cliente reale
            (25, 280, 380, None),
            (380, 280, 720, None),   # h24_alias
        ],
    ),
    "22_auth_codes_lifecycle.png": (
        1100,
        [
            (25, 280, 240, None),    # codice
            (380, 280, 1100, None),  # cycle (sent_to/accepted_by)
        ],
    ),
    "23_customer_groups.png": (
        1100,
        [],  # visualizza solo nomi gruppi, sicuro
    ),
    "24_privacy_bypass.png": (
        1100,
        [
            (25, 280, 700, None),    # email/domini in privacy bypass (sensibili)
        ],
    ),
    "25_rule_form_new.png": (
        1400,
        [],  # form vuoto, niente da anonimizzare
    ),
}


def _add_banner(canvas: Image.Image) -> Image.Image:
    """Banner 'DATI ANONIMIZZATI' in alto a destra."""
    w = canvas.size[0]
    draw = ImageDraw.Draw(canvas)
    try:
        small_font = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 13,
        )
    except OSError:
        small_font = ImageFont.load_default()
    label = "  DATI ANONIMIZZATI A SCOPO DIDATTICO  "
    text_w = draw.textlength(label, font=small_font)
    draw.rectangle(
        (w - text_w - 30, 12, w - 12, 38),
        fill=(254, 215, 170), outline=(234, 88, 12),
    )
    draw.text(
        (w - text_w - 22, 17), label,
        fill=(124, 45, 18), font=small_font,
    )
    return canvas


def _anonymize(img_path: Path, crop_h: int,
                zones: list[tuple[int, int, int, int | None]]) -> bool:
    if not img_path.exists():
        print(f"[skip] {img_path.name}: file mancante")
        return False
    img = Image.open(img_path).convert("RGB")
    w, h = img.size
    crop_h_eff = min(crop_h, h)
    img = img.crop((0, 0, w, crop_h_eff))
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw_overlay = ImageDraw.Draw(overlay)
    canvas = img.copy()
    for x0, y_top, x1, y_bottom in zones:
        y_bot_eff = y_bottom if y_bottom is not None else crop_h_eff
        # Blur AGGRESSIVO + overlay grigio chiaro 35% opacità
        region = canvas.crop((x0, y_top, x1, y_bot_eff))
        if region.size[0] <= 0 or region.size[1] <= 0:
            continue
        region = region.filter(ImageFilter.GaussianBlur(radius=22))
        canvas.paste(region, (x0, y_top))
        draw_overlay.rectangle(
            (x0, y_top, x1, y_bot_eff),
            fill=(241, 245, 249, 90),
        )
    canvas = Image.alpha_composite(
        canvas.convert("RGBA"), overlay,
    ).convert("RGB")
    canvas = _add_banner(canvas)
    canvas.save(img_path, format="PNG", optimize=True)
    print(f"[ok] {img_path.name} ({img_path.stat().st_size // 1024} KB)")
    return True


def main() -> int:
    n_done = 0
    for fname, (crop_h, zones) in ANON_PLAN.items():
        if _anonymize(IMG_DIR / fname, crop_h, zones):
            n_done += 1
    print(f"\nAnonimizzati {n_done}/{len(ANON_PLAN)} file in {IMG_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
