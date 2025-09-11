from __future__ import annotations

import io
from datetime import datetime
from typing import List, Optional

from markdown import markdown as md_to_html  # Markdown>=3.6
from weasyprint import HTML, CSS  # weasyprint>=61


PRINT_CSS = CSS(string=
    """
    @page { size: A4; margin: 20mm; }
    body { font-family: -apple-system, BlinkMacSystemFont, Segoe UI, Roboto, Helvetica, Arial, sans-serif; color: #111; }
    h1 { font-size: 22pt; margin: 0 0 8pt; }
    h2 { font-size: 16pt; margin-top: 18pt; }
    h3 { font-size: 13pt; margin-top: 14pt; }
    pre, code { font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace; }
    pre { background: #fafafa; border: 1px solid #eee; padding: 8px; border-radius: 6px; overflow: auto; }
    .meta { color: #444; font-size: 10pt; margin-bottom: 10pt; }
    .banner { background: #fffbe6; border: 1px solid #fde68a; padding: 8px; border-radius: 6px; color: #92400e; font-size: 10pt; }
    footer { position: fixed; bottom: 0; left: 0; right: 0; text-align: center; color: #666; font-size: 9pt; }
    footer .pagenum:before { content: counter(page) " / " counter(pages); }
    a { color: #2563eb; text-decoration: none; }
    a:hover { text-decoration: underline; }
    """
)


def _wrap_html(title: str, chat_id: str, created_at: str, body_html: str, truncated_note: Optional[str]) -> str:
    banner = f'<div class="banner">{truncated_note}</div>' if truncated_note else ''
    return f"""
<!doctype html>
<html>
  <head>
    <meta charset="utf-8" />
    <title>{title}</title>
  </head>
  <body>
    <header>
      <h1>{title}</h1>
      <div class="meta">chat_id: {chat_id} • created_at: {created_at}</div>
      {banner}
    </header>
    <main>
      {body_html}
    </main>
    <footer><span class="pagenum"></span></footer>
  </body>
</html>
"""


def render_pdf_from_markdown(markdown: str, *, css_paths: Optional[List[str]] = None, metadata: Optional[dict] = None) -> bytes:
    """Convert Markdown to a styled PDF.

    - Markdown -> HTML with code highlighting via 'fenced_code' + 'codehilite'.
    - Wrap in a simple printable HTML skeleton with header & footer.
    - Render to PDF with WeasyPrint.
    """
    metadata = metadata or {}
    title = metadata.get("title", "AI-use Report")
    chat_id = metadata.get("chat_id", "-")
    created_at = metadata.get("created_at", datetime.utcnow().isoformat())
    truncated = metadata.get("truncated")
    truncated_note = "Generated from a truncated chat transcript" if truncated else None

    # Convert Markdown to HTML. Enable code highlighting via Pygments.
    body_html = md_to_html(markdown or "", extensions=["fenced_code", "codehilite", "tables", "toc"])
    full_html = _wrap_html(title, chat_id, created_at, body_html, truncated_note)

    css_list = [PRINT_CSS]
    if css_paths:
        css_list += [CSS(filename=p) for p in css_paths]

    out_io = io.BytesIO()
    HTML(string=full_html, base_url=".").write_pdf(out_io, stylesheets=css_list)
    return out_io.getvalue()

