---
doc: reports/_index
description: Router for the AI-use report feature — generate a PDF summarizing a chat's AI usage (routes, service, ORM, PDF rendering)
owns: []
related: []
last_verified: 2026-07-25
stub: false
---

# Reports — the AI-use report feature

A self-contained teacher/student-facing feature: generate a PDF summarizing a
chat's AI usage. Previously mis-filed under `domain-data`; now its own domain.
It depends OUTWARD on platform (`auth`, `vendors/openai_client`) and domain-data
(`chats`, `database`) but owns its own routes/service/model/pdf.

## Request flow
`POST /reports/ai-use/{chat_id}` → build redacted evidence pack → generate
markdown via the vendor OpenAI client → persist an `AIUsageReport` → `GET
…/{id}.pdf` renders it with WeasyPrint.

## Cross-cutting invariants
- **`user_id` is always the authenticated identity, `course_id` always comes off
  the owning chat session** — never client-supplied (IDOR-safe reads/writes).
- The ORM (`ai-use-models`) documents the mapping; the **DDL authority** is the
  supabase migration (`database/supabase-migrations`) — the two must not drift.

## Leaves
| Leaf | Role · owns |
|---|---|
| [ai-use-routes](ai-use-routes.md) | FastAPI endpoints + ownership guards · reports/ai_use/routes.py (+2 __init__) |
| [ai-use-service](ai-use-service.md) | evidence-pack build + redaction + generation · reports/ai_use/service.py |
| [ai-use-models](ai-use-models.md) | AIUsageReport ORM + async persistence · reports/ai_use/models.py |
| [ai-use-pdf](ai-use-pdf.md) | WeasyPrint markdown→PDF · reports/ai_use/pdf.py |
