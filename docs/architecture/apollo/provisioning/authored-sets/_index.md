---
doc: apollo/provisioning/authored-sets/_index
description: Router for the authored_sets subpackage — the teacher-gated authored problem/solution-set provisioning pipeline
owns:
  - apollo/provisioning/authored_sets/__init__.py
related: []
last_verified: 2026-07-25
stub: false
---

**Parent router:** [`provisioning/_index`](../_index.md). This is a nested
sub-router — the sanctioned 5–6-hop exception (PLAN R4): `CLAUDE.md → shared README
→ apollo/_index → provisioning/_index → authored-sets/_index → leaf`, justified
because a flat provisioning index would blow the 60-line cap.

The authored-set pipeline indexes a problem document + optional paired solution
document (both hidden from student retrieval), scrapes candidates, grounds each
against ONLY its paired solution, derives/validates a reference graph, and promotes
trusted references (generated or OCR-suspect ones stay tier-1 for teacher review).
The package `__init__.py` is an empty namespace init — submodules are imported by
full path, there is no re-export barrel.

## Leaf docs

| Leaf | Owns | One-liner |
|---|---|---|
| [api](api.md) | `authored_sets/api.py` | Teacher HTTP surface (monolith ~1616 lines): create/list/get/edit/delete + approve; `router` mounts in `apollo/api.py` |
| [orchestrator](orchestrator.md) | `authored_sets/orchestrator.py` | Drives scrape → ground → derive/pair → mint+promote per candidate; owns the single mint+promote savepoint |
| [graph-derivation](graph-derivation.md) | `authored_sets/graph_derivation.py` | Solution-grounded gold-format reference-graph derivation + pure defect validator |
| [structure-pass](structure-pass.md) | `authored_sets/structure_pass.py` | One structured pass segmenting question/answer units + deterministic label pairing |
| [paired-retrieval](paired-retrieval.md) | `authored_sets/paired_retrieval.py`, `label_match.py` | Doc-scoped grounding vs the paired solution; deterministic label matching |
| [indexing](indexing.md) | `authored_sets/indexing.py` | Index authored PDFs into HIDDEN (PENDING) documents |
| [observability](observability.md) | `authored_sets/observability.py` | Ingest-run + per-page OCR-evidence audit writes |
| [verification](verification.md) | `authored_sets/verification.py` | Low-OCR-confidence generate-and-compare cross-check |

## Cross-cutting invariants

- **Governing flags:** `APOLLO_REVERSED_PROVISIONING` (default on when the course has
  registered concepts; `0` reverts to the legacy LLM-tag-draft path) and
  `APOLLO_STRUCTURE_PAIRING` (off/shadow/on).
- **Combined-Q&A ordering (student-safety boundary):** with structure pairing on, the
  structure pass runs BEFORE scrape and the scraper receives question-only slices —
  tier-1 problem text persists immediately after scrape and cannot be repaired later.
- **Mint+promote savepoint:** the orchestrator runs `tag_and_mint` + `promote` inside
  one `begin_nested()` so a lint rejection rolls back every flushed KG row (no
  orphaned entities). The full recipe lives in `provisioning/_index` (PLAN D21).
