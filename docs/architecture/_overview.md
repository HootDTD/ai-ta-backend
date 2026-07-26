---
doc: ai-ta-backend/_overview
description: Thin root router for the ai-ta-backend architecture tree — routes to the nine domain indexes.
owns: []
related: []
last_verified: 2026-07-25
stub: false
---

# ai-ta-backend architecture — root router

This is the top of the backend durable-doc tree. It only routes. Reading protocol:
`shared-architecture/README.md` → this file → a domain `_index.md` → 1-3 leaves
(≤4 hops, ≤~500 doc-lines). Resolve any source file to its owning leaf via
`docs/index.json`. Never read source to understand the system — only to change it.

| Domain | Index | Covers |
|---|---|---|
| apollo | [apollo/_index](apollo/_index.md) | teach-a-confused-AI mode: teaching loop, KG, grading, provisioning, persistence (365 files, two-tier index) |
| rag-pipeline | [rag-pipeline/_index](rag-pipeline/_index.md) | the /ask QA pipeline — `ai/` + `retrieval/` + `citations/` |
| chats | [chats/_index](chats/_index.md) | chat session/turn persistence, rolling memory, bundle cache |
| knowledge | [knowledge/_index](knowledge/_index.md) | teacher weekly upload + PDF ingestion |
| platform | [platform/_index](platform/_index.md) | server, auth, config, vendors, workspaces, ops scripts, CI |
| reports | [reports/_index](reports/_index.md) | the AI-use report feature |
| indexing | [indexing/_index](indexing/_index.md) | the PDF→pgvector embedding pipeline + OCR layer |
| database | [database/_index](database/_index.md) | core ORM, RLS, migrations (legacy frozen chain + active supabase chain) |
| campaign | [campaign/_index](campaign/_index.md) | the offline Apollo grading-campaign harness |

Cross-repo durable docs (conventions, security, supabase, data-flow, branching,
admin-setup, product-context) live one level up in
[`shared-architecture/`](../shared-architecture/README.md).
