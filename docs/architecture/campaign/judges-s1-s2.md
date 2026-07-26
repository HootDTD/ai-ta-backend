---
doc: campaign/judges-s1-s2
description: The two upstream (pre-teaching) quality judges — reference-graph structure and WU-AAS ingestion.
owns:
  - campaign/judges/s1_reference_graph.py
  - campaign/judges/s2_ingestion.py
related:
  - campaign/judges-base
  - campaign/scripts-run-s1-s2
  - apollo/persistence/learner-model-seed
last_verified: 2026-07-25
stub: false
---

# campaign/judges-s1-s2 — reference-graph + ingestion

Two `StageJudge` subclasses that score quality BEFORE the teaching loop. Both
gate (E3) at ≥95% item-level correct.

## Interface

- **S1 (`s1_reference_graph.py`)** `S1ReferenceGraphJudge` — one item per node +
  per edge (LLM: is this a real, grounded step / true dependency) PLUS
  code-only structural items. `find_structural_defects(nodes, edges)` +
  `_cycle_defect(...)` detect duplicate node ids, PRECEDES/DEPENDS_ON cycles
  (Kahn's algorithm, mirroring `KGGraph.topological_order` /
  `promotion_lint._gate_3`), and dangling endpoints. Its overridden `judge`
  passes structural items straight through (never sent to the LLM).
- **S2 (`s2_ingestion.py`)** `S2IngestionJudge` — one item per audited
  page/problem triple (LLM: scraped-label match, problem↔solution pairing, OCR
  faithfulness). `check_verify_path_fired(items)` is the pure code-side check
  that the low-OCR verify path fired iff `ocr_confidence < threshold`; its
  `judge` folds those verdicts into the same `pass_rate`.

## Invariants & gotchas

- The cycle/dangling/duplicate checks are CODE (deterministic); node/edge truth
  and page faithfulness are the LLM's job (they need the source material).
- Raw inputs are produced by [scripts-run-s1-s2](scripts-run-s1-s2.md) (S1 =
  subject reference graph, S2 = page-evidence rows).

## Related

- [judges-base](judges-base.md), [scripts-run-s1-s2](scripts-run-s1-s2.md),
  [apollo/persistence/learner-model-seed](../apollo/persistence/learner-model-seed.md)
  (reference-graph provenance).
