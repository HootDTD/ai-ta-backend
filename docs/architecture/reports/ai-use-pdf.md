---
doc: reports/ai-use-pdf
description: reports/ai_use/pdf.py — markdown→PDF rendering via WeasyPrint for the AI-use report GET .pdf route
owns:
  - reports/ai_use/pdf.py
related:
  - reports/ai-use-routes
  - platform/ci-workflows
last_verified: 2026-07-25
stub: false
---

# reports/ai-use-pdf — markdown→PDF

## Interface

- `render_pdf_from_markdown(markdown, *, css_paths=None, metadata=None) -> bytes`
  — converts markdown → HTML (`fenced_code`, `codehilite`, `tables`, `toc`),
  wraps it in a print skeleton with a metadata header + optional truncation
  banner, and renders to PDF bytes.
- `_wrap_html(title, chat_id, created_at, body_html, truncated_note)` — the HTML
  skeleton.
- Module-level `PRINT_CSS` — the A4 print stylesheet.

## Data flow

`ai-use-routes.get_ai_use_report_pdf` passes the stored markdown + a metadata
dict (`title`, `chat_id`, `created_at`, `truncated`); the returned bytes become
the PDF `Response`.

## Invariants & gotchas

- **WeasyPrint needs native pango/cairo libraries** — installed by the CI
  composite setup action (`ci-workflows`); a missing native lib breaks PDF
  rendering at runtime (and `import server` in CI).

## Related

`reports/ai-use-routes` (the caller), `platform/ci-workflows` (installs the
native libs the import depends on).
