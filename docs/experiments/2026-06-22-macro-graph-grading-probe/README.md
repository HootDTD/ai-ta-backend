# Experiment: Macro (OpenStax Ch.6) graph-grading probe

**Date started:** 2026-06-22 · **Run completed:** 2026-06-23
**Owner:** Apollo graph-grading
**Status:** ✅ complete — see `RESULTS.md`

> **Headline:** the engine runs end-to-end on a non-fluid subject and
> discriminates explanation quality (4/5 problems), AND the **"case-3"
> derived-form edge-resolution bug is GENERAL** — it reproduced on macroeconomics
> (`real_gdp_growth`: a computed equation form fails to resolve, its `USES` edge
> drops, `usage`/`edge_coverage` → 0) with the same structural signature as the
> Bernoulli attempt-8 case. The handoff's open question is answered: case-3 is
> not fluid-specific.
**Lineage:** continues `ai-ta-backend/docs/APOLLO-USES-EDGE-RESOLUTION-HANDOFF.md`
(Part 7 "THE NEXT STEP") and its predecessors
`APOLLO-GRADING-EDGE-RESOLUTION-HANDOFF.md`, `APOLLO-GRAPH-GRADING-HANDOFF.md`.

## What this is

A self-contained, reproducible test that stands up a **brand-new non-fluid
subject** — *OpenStax Principles of Macroeconomics 2e, Chapter 6 "The
Macroeconomic Perspective"* (CC BY 4.0) — and drives it through the **entire
Apollo graph-grading pipeline** end-to-end, **without** running the full Apollo
auto-provisioning embedding pipeline (which is being fixed separately).

We use *part* of the production pipeline (the indexing path) to embed the
textbook into a **local** Supabase, mine questions from it (testing the RAG
relevance pathway), hand-author reference answer-KGs, generate 3 student-answer
variations per question, and run the **S_norm vs R_norm** comparison — reading
the resulting scores out of `apollo_graph_comparison_runs`.

## Why

Apollo is fundamentally an equation + SymPy graph engine and the whole test
corpus to date is **fluid-mechanics-biased**. The open question from the
handoff: is the "case-3" edge-resolution failure (a *derived / solved form* of
an in-scope equation fails to resolve → its `USES` edge is dropped from S_norm →
`edge_coverage`/`usage` collapse to 0 even for a correct answer) **general**, or
an artifact of fluids? Ch.6 contains a clean derived-form analog — the GDP
deflator equation rearranged into the real-GDP formula — so it can probe exactly
that.

## Locked decisions (see DESIGN.md)

| Decision | Choice |
|---|---|
| Primary goal | **Generality sanity check + reproduce the case-3 derived-form bug** |
| Sourcing | **Hybrid** — RAG-mine questions + hand-author reference KGs |
| Execution | **Run end-to-end on the LOCAL Docker stack** (agent-driven) |
| Scope | **2 concepts, 5 questions, 3 variations each = 15 graded attempts** |

## Folder contents

| File | What |
|---|---|
| `README.md` | This index + status |
| `BACKGROUND.md` | The system under test (Apollo graph grading, S_norm/R_norm, scores, the case-3 lineage) |
| `DESIGN.md` | The full approved design — architecture, the 5 questions, build items, metric |
| `WORKFLOW.md` | The exact orchestration method + reproducible run commands + agent/run split |
| **`RESULTS.md`** | **The master record** — setup recap, every phase with raw output, the full score matrices, the case-3 deep dive (Q4 vs Q5, with the resolution dumps), and a 7-item **defect catalog** mapping each finding to a pipeline stage, its architectural blast radius, and its fluid (Bernoulli) cross-reference |

**Start with `RESULTS.md`** — it is self-contained and explains the setup too.

## Artifacts produced by this experiment

- Subject content: `ai-ta-backend/apollo/subjects/macroeconomics/**`
- Scripts: `scripts/index_local_pdf.py`, `scripts/run_macro_probe.py`,
  generalized `scripts/seed_apollo_learner_model.py` + `scripts/apollo_grade_probe.py`
- Code: macro polarity antonyms in `apollo/resolution/competition.py`
- Tests: alongside each changed module (95% patch-coverage contract)
