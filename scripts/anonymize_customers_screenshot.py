#!/usr/bin/env python3
"""Crop + anonimizzazione dello screenshot anagrafica clienti.

Lo screenshot originale (full-page, 3.5 MB) contiene dati reali dei
clienti. Per il manuale utente dobbiamo:
- tagliare alla parte alta (header + KPI + filtri + qualche riga di tabella),
- sostituire codici cliente / ragioni sociali / domini con placeholder
  generici via blur+overlay.

Output: ``docs/manuale_utente/img/03_customers_list.png`` (sovrascritto).
"""
from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

IMG_DIR = Path(__file__).resolve().parent.parent / "docs" / "manuale_utente" / "img"
SRC = IMG_DIR / "03_customers_list.png"
DST = SRC


def main() -> int:
    if not SRC.exists():
        print(f"[ERR] file mancante: {SRC}", file=sys.stderr)
        return 1
    img = Image.open(SRC).convert("RGB")
    w, h = img.size
    print(f"[info] dimensione originale {w}x{h}")

    # 1) Crop alla parte superiore: header + KPI + filtri + ~7-8 righe.
    crop_h = min(1100, h)
    img = img.crop((0, 0, w, crop_h))
    print(f"[info] croppato a {w}x{crop_h}")

    # 2) Anonimizzazione: blur a copertura larga delle colonne sensibili.
    #    L'header tabella sta intorno a y≈410, le righe iniziano a y≈455.
    #    Per sicurezza copriamo tutta l'area dati dalle prime righe in giù.
    # Coprire dalla prima riga di dati in giù: meglio "sacrificare" un po'
    # di header tabella che lasciare anche solo una riga decifrabile.
    table_top = 320
    canvas = img.copy()
    # Strategia: blur AGGRESSIVO (radius=22) + overlay grigio semi-opaco che
    # rende illegibile qualsiasi testo residuo anche con zoom estremo.
    cols = [
        (25, 155),    # codice cliente
        (155, 525),   # ragione sociale
        (640, 1130),  # domini & alias
    ]
    overlay = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    draw_overlay = ImageDraw.Draw(overlay)
    for x0, x1 in cols:
        region = canvas.crop((x0, table_top, x1, crop_h))
        region = region.filter(ImageFilter.GaussianBlur(radius=22))
        canvas.paste(region, (x0, table_top))
        # Overlay grigio chiaro 35% opacità sopra il blur
        draw_overlay.rectangle(
            (x0, table_top, x1, crop_h),
            fill=(241, 245, 249, 90),
        )
    canvas = Image.alpha_composite(
        canvas.convert("RGBA"), overlay,
    ).convert("RGB")

    # Aggiunge un overlay "DEMO / DATI ANONIMIZZATI"
    draw = ImageDraw.Draw(canvas)
    try:
        font = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 18,
        )
        small_font = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 13,
        )
    except OSError:
        font = ImageFont.load_default()
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

    canvas.save(DST, format="PNG", optimize=True)
    print(f"[ok] salvato {DST} ({DST.stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
