"""Endpoint UI per il manuale auto-generato + il CHANGELOG.

- ``GET /manual`` — render HTML del ``docs/manual.md``.
- ``GET /manual/raw`` — versione raw markdown (download).
- ``GET /manual/changelog`` — render HTML del ``CHANGELOG.md``.
- ``POST /manual/regenerate`` — forza rigenerazione (admin/superadmin).
"""
from __future__ import annotations

import re
from pathlib import Path

from flask import Blueprint, Response, current_app, flash, redirect, render_template, url_for

from ..auth import login_required
from ..manual_generator import MANUAL_PATH, read_manual, write_manual

manual_bp = Blueprint("manual", __name__, url_prefix="/manual")

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CHANGELOG_PATH = REPO_ROOT / "CHANGELOG.md"
USER_MANUAL_PATH = REPO_ROOT / "docs" / "manuale_utente" / "MANUALE_UTENTE.md"
USER_MANUAL_IMG_DIR = REPO_ROOT / "docs" / "manuale_utente" / "img"


def _markdown_to_html(md: str, *, image_url_prefix: str | None = None) -> str:
    """Renderer markdown leggero (NO dipendenze esterne).

    Supporta: header (#), tabelle, code inline (`...`), bold (**...**),
    italic (_..._), liste, hr, link [...](...), immagini ![](img/...).
    Se ``image_url_prefix`` è fornito, le immagini ``img/foo.png`` vengono
    riscritte come ``{prefix}/foo.png``.
    """
    html: list[str] = []
    in_table = False
    in_list = False
    in_code_block = False

    def _md(s: str) -> str:
        return _inline_md(s, image_url_prefix=image_url_prefix)

    for raw in md.split("\n"):
        line = raw

        # Code block fenced
        if line.strip().startswith("```"):
            if in_code_block:
                html.append("</code></pre>")
                in_code_block = False
            else:
                html.append("<pre><code>")
                in_code_block = True
            continue
        if in_code_block:
            html.append(_escape_html(line))
            continue

        # Tabelle
        if "|" in line and line.strip().startswith("|"):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if all(re.match(r"^\s*-+\s*$", c) or c == "" for c in cells):
                continue
            if not in_table:
                html.append('<table class="dr-table">')
                in_table = True
                html.append("<thead><tr>" + "".join(
                    f"<th>{_md(c)}</th>" for c in cells
                ) + "</tr></thead><tbody>")
            else:
                html.append("<tr>" + "".join(
                    f"<td>{_md(c)}</td>" for c in cells
                ) + "</tr>")
            continue
        elif in_table:
            html.append("</tbody></table>")
            in_table = False

        # Header
        m = re.match(r"^(#{1,6})\s+(.+)$", line)
        if m:
            level = len(m.group(1))
            html.append(f"<h{level}>{_md(m.group(2))}</h{level}>")
            continue

        # Hr
        if re.match(r"^-{3,}\s*$", line):
            html.append("<hr>")
            continue

        # Liste
        if re.match(r"^\s*[-*]\s+", line):
            if not in_list:
                html.append("<ul>")
                in_list = True
            html.append(f"<li>{_md(line.lstrip().lstrip('-*').strip())}</li>")
            continue
        elif in_list:
            html.append("</ul>")
            in_list = False

        # Paragrafo
        if line.strip():
            html.append(f"<p>{_md(line)}</p>")
        else:
            html.append("")

    if in_table:
        html.append("</tbody></table>")
    if in_list:
        html.append("</ul>")
    if in_code_block:
        html.append("</code></pre>")
    return "\n".join(html)


def _escape_html(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _inline_md(text: str, *, image_url_prefix: str | None = None) -> str:
    """Inline transformations: code, bold, italic, link, image."""
    # Escape primo tutti i tag HTML potenzialmente pericolosi
    text = _escape_html(text)
    # Code inline `...`
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    # Bold **...**
    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    # Italic _..._
    text = re.sub(r"\b_([^_]+)_\b", r"<em>\1</em>", text)
    # Image ![alt](src) — DEVE precedere il link generico
    if image_url_prefix:
        def _img_repl(m: re.Match) -> str:
            alt = m.group(1)
            src = m.group(2)
            # Solo immagini relative al sotto-folder img/
            if src.startswith("img/"):
                src = f"{image_url_prefix.rstrip('/')}/{src[len('img/'):]}"
            return (f'<img src="{src}" alt="{alt}" '
                    f'style="max-width:100%; border:1px solid #cbd5e1; '
                    f'border-radius:6px; margin:0.6rem 0; '
                    f'box-shadow:0 1px 3px rgba(0,0,0,0.06);">')
        text = re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", _img_repl, text)
    # Link [text](url)
    text = re.sub(
        r"\[([^\]]+)\]\(([^)]+)\)",
        r'<a href="\2" target="_blank" rel="noopener">\1</a>',
        text,
    )
    return text


@manual_bp.route("/")
@login_required()
def view():
    """Renderizza il manuale auto-generato in HTML."""
    md = read_manual()
    html_content = _markdown_to_html(md)
    return render_template("admin/manual_view.html",
                            html_content=html_content,
                            title="Manuale tecnico (auto-generato)",
                            raw_url=url_for("manual.raw"),
                            other_link_url=url_for("manual.changelog"),
                            other_link_label="CHANGELOG")


@manual_bp.route("/raw")
@login_required()
def raw():
    """Versione raw del markdown."""
    md = read_manual()
    return Response(md, mimetype="text/markdown; charset=utf-8")


@manual_bp.route("/changelog")
@login_required()
def changelog():
    """Renderizza il CHANGELOG.md in HTML."""
    if CHANGELOG_PATH.exists():
        md = CHANGELOG_PATH.read_text(encoding="utf-8")
    else:
        md = "# CHANGELOG\n\n_File non disponibile._"
    html_content = _markdown_to_html(md)
    return render_template("admin/manual_view.html",
                            html_content=html_content,
                            title="CHANGELOG",
                            raw_url=None,
                            other_link_url=url_for("manual.view"),
                            other_link_label="Manuale tecnico")


@manual_bp.route("/utente")
@login_required()
def user_manual():
    """Manuale utente non-tecnico (operatori/amministratori) con screenshot."""
    if USER_MANUAL_PATH.exists():
        md = USER_MANUAL_PATH.read_text(encoding="utf-8")
    else:
        md = "# Manuale utente\n\n_File non disponibile._"
    img_prefix = url_for("manual.user_manual_image", filename="").rstrip("/")
    html_content = _markdown_to_html(md, image_url_prefix=img_prefix)
    return render_template("admin/manual_view.html",
                            html_content=html_content,
                            title="Manuale utente",
                            raw_url=None,
                            other_link_url=url_for("manual.view"),
                            other_link_label="Manuale tecnico")


@manual_bp.route("/utente/img/<path:filename>")
@login_required()
def user_manual_image(filename: str):
    """Serve gli screenshot del manuale utente (PNG)."""
    # Sicurezza: solo file dentro USER_MANUAL_IMG_DIR, niente path traversal
    safe = USER_MANUAL_IMG_DIR / filename
    try:
        safe = safe.resolve()
        safe.relative_to(USER_MANUAL_IMG_DIR.resolve())
    except (ValueError, OSError):
        return Response("Forbidden", status=403)
    if not safe.is_file():
        return Response("Not found", status=404)
    suffix = safe.suffix.lower()
    mime = {".png": "image/png", ".jpg": "image/jpeg",
             ".jpeg": "image/jpeg", ".svg": "image/svg+xml"}.get(suffix,
                                                                  "application/octet-stream")
    return Response(safe.read_bytes(), mimetype=mime)


@manual_bp.route("/regenerate", methods=["POST"])
@login_required(role="admin")
def regenerate():
    write_manual(current_app)
    flash("✓ Manuale rigenerato.", "success")
    return redirect(url_for("manual.view"))
