# Design: Make Apollo auto-provisioning run end-to-end — Phase 1

- **Date:** 2026-06-22
- **Branch:** `ApolloRun`
- **Status:** Approved (design); implementation plan to follow
- **Owner doc to update on implementation:** `docs/architecture/apollo.md`

## Context

Apollo's 6-stage LLM auto-provisioning pipeline turns textbook content
(`aita_chunks`) into teachable problems with a machine-graded **reference graph**
(the correct-answer "answer key"):

```
aita_chunks → [1] scrape_questions → [2] find_or_generate (reference_solution)
            → [3] validate_pair → [4] tag_and_mint (concept tag + mint KG entities
            + prereq edges) → [5] promote (8-gate lint + project_canon → :Canon)
```

The collaborator (SchoolsInBeirut) has finished his work on this pipeline; PR #61
(stages 1–3 grounding) was his last contribution. **We now own it.** It currently
dies on one BLOCKER in stage 4. Goal: get it producing a reference graph
end-to-end, then make it correct/safe.

Naming: the correct-answer graph is the **reference graph** (`reference_solution`
in JSON → minted entities → `:Canon` → `R_norm` at grade time). The student's
parsed teaching is the **student graph** (`S_norm`). Grading is `R_norm ⊑ S_norm`.

### Shadow / flag topology (verified against code)

The whole subsystem is flag-gated and runs in shadow; the isolation boundary is
"the pipeline doesn't run," not "it runs but its output is quarantined":

- **`APOLLO_AUTOPROVISION_ENABLED`** gates the worker (`provision_worker.py:63-85`),
  which ships at 0 replicas in prod. When off, `enqueue_provisioning_job` still
  inserts the `apollo_ingest_runs` row during teacher upload
  (`knowledge/teacher_weekly.py:1184`), but nothing drains it → zero Tier-2
  problems, zero `:Canon`. The entire pipeline is dormant.
- **`APOLLO_MISCONCEPTION_ENABLED`** gates the misconception path
  (`apollo/overseer/misconception.py:503`); env comment: "no-op until P7 chat
  wiring; harmless now." H3 is an explicitly unwired, separately-flagged feature.
- **`APOLLO_GRAPH_SIM_SHADOW_ENABLED` / `_LIVE_ENABLED`** gate the grade-time
  consumer of the reference graph (`apollo/handlers/done.py:66-83`). SHADOW
  computes the graph rubric/diagnostic but does not surface it; a human reads
  calibration output (`apollo/grading/calibration.py:4`) before flipping LIVE.
- **Caveat:** the serve path `list_problems_for_concept`
  (`apollo/overseer/problem_selector.py:55`, `tier == 2`) is NOT behind a
  provisioning flag. A promoted Tier-2 row enters the student pool (mixed with
  §8-seeded rows) the moment it exists — but it only exists if the worker ran.

**Consequence for scoping:** correctness bugs (H2/H4) and the unwired
misconception feature (H3) cannot corrupt production while
`APOLLO_AUTOPROVISION_ENABLED` stays off. So deferring them is safe; this plan
prioritizes making the pipeline RUN reliably.

## Goal & success criterion

One claimed provisioning job drives a scraped candidate through stages 1→5,
promotes to Tier-2, and writes `:Canon` nodes.

**Verified by:**
- `pytest apollo/provisioning/ -v` green, and
- a local end-to-end run (`scripts/drain_one_provision.py`, untracked) that
  produces a `:Canon` node (real LLM, local Neo4j docker, staging Supabase;
  ~$0.05). Local run contract: `RUNBOOK.md` (gitignored) +
  `source ./apollo_run_env.sh`.

## Scope (settled)

**IN (this plan):**
1. **BLOCKER + H1** — key normalization (approach A).
2. **H4** — label the two prereq-edge kinds (no migration: derive from `kind` +
   document + guard test).
3. **Robustness** — downgrade whole-doc aborts → per-candidate rejects.

**DEFERRED (documented in `apollo.md`; future plans):**
- Phase 2 (correct): H2 (symbolic validator), H3 misconception *wiring*, scope
  asymmetry (scrape reads all chunks vs week-gated grounding).
- Phase 3 (scale): H5 (dedup re-embeds pool per candidate), unbounded per-doc
  chunk fan-out.
- Phase 4 (hygiene): LOWs (`concept_id` slug-not-FK, `_judge_distinct` stub,
  CanonProjectionError re-export, etc.) + the dedup cosine NaN guard.

The misconception *feature* is deferred, but its latent `link_opposes` key bug
(H1) is fixed now via the shared helper, so the path is correct when the flag is
eventually flipped.

## The BLOCKER (verified)

`tag_and_mint` builds `key_to_id` from **prefixed** canonical keys
(`eq.<id>`/`proc.<id>`/…; `learner_model_seed.py:198-201,227` via
`_ENTRY_TYPE_TO_KIND_PREFIX`). The LLM prereq draft emits **bare** ids, and
`insert_prereqs` does a hard `key_to_id[from_key]`
(`tag_mint_persist.py:217`) → `KeyError` → `TagMintError` → whole-document abort
(`orchestrator.py:494`). The tag prompt (`orchestrator.py:100-111`) says "minted
entity keys" but never shows the prefix scheme, and `build_tag_schema`
(`provisioning_schema.py:110-119`) types `from`/`to` as free strings with no
enum. **This is a code fix, not a prompt fix** — the model can't author keys it
never sees. `link_opposes` (`tag_mint_persist.py:197`) shares the bug (H1),
dormant only because `misconceptions=[]`. Tests miss it: every prereq fixture in
`test_tag_mint.py` (lines 508/538/640/653) hand-writes prefixed keys.

**Cross-check cleared:** "promote receives an un-annotated problem so every
promotion fails `validate_reference_graph`" is a FALSE ALARM — `_annotate`
(`promote.py:105`) annotates via `annotate_reference_solution` BEFORE the gate-2
validation (`promote.py:116`), using the same `_entity_key_for_step` prefix
helper. The stage 4→5 annotation seam is sound.

## Component 1 — Key normalization (BLOCKER + H1), approach A

- **New helper in `tag_mint.py`:** build a `bare_id → canonical_key` map from
  `problem["reference_solution"]` via the frozen `_entity_key_for_step`. After
  `key_to_id` is populated by canonical key, register **bare-id aliases** that
  point at the same entity ids.
- **Effect:** both `insert_prereqs` and `link_opposes` lookups succeed for
  LLM-emitted bare ids (`bernoulli`) AND prefixed ids (`eq.bernoulli`).
  Genuinely-unmappable keys still raise `KeyError → TagMintError` — **fail-closed
  preserved** (the existing `eq.nonexistent` test stays green).
- **No signature changes** to `insert_prereqs`/`link_opposes`; the alias map is
  enriched in `tag_and_mint` where the problem dict is in scope.
- **Assumption (documented):** reference-step ids are unique within a problem —
  the KG graph derivation (`apollo/schemas/problem.py` `to_kg_graph`) and
  `depends_on` resolution already rely on it, so the `bare → canonical` map is a
  well-defined 1:1 within a problem.

## Component 2 — H4 prereq-edge labeling (no migration)

The seed path emits **concept→concept** edges (`concept_dag_to_prereqs`,
`learner_model_seed.py:144`); the auto path emits **ref-node→ref-node** edges
(approach A keeps the LLM draft). `apollo_entity_prereqs` is a bare composite-PK
`(from_entity_id, to_entity_id)` table (`apollo/persistence/models.py:416-431`)
with no column for a label. The two kinds answer different questions
(curriculum-level topic ordering vs within-problem step structure) and we keep
both.

**Decision: label by convention, derived from endpoint `kind` — no migration.**
- A prereq edge is **concept-level** when both endpoints are
  `apollo_kg_entities.kind == 'concept'`, else **ref-node-level**.
- The readers already self-partition: `personalization_read.py:146-155` loads
  only WITHIN-CONCEPT edges (`from_entity_id.in_(concept_entity_ids) AND
  to_entity_id.in_(...)`), so the auto path's same-concept ref-node edges are
  consumed and the seed's cross-concept concept→concept edges are excluded —
  `prereqs_mastered` (`personalization_select.py:86-104`) never conflates them.
- **Document** the `apollo_entity_prereqs` two-kind contract in `apollo.md`.
- **Guard test** on a mixed table: `personalization_read`/`_select` consume the
  auto ref-node edges within-concept and do NOT pull in concept-level
  cross-concept edges.
- An explicit discriminator *column* is noted as a Phase-2+ option, not now.

## Component 3 — Robustness downgrades (so a real run completes)

Each converts a whole-document abort into a clean per-candidate reject (one bad
LLM candidate must not sink a document whose other candidates promote):

- `retrieval_adapter.py:54` → use `row.get("content")` and skip content-less rows
  rather than `row["content"]` → `KeyError` → abort.
- `promote.py` `_annotate` → guard the `steps_by_id`/`entry_type` lookup so a
  malformed step yields a clean gate-1 reject rather than a pre-gate `KeyError`.
  This is the **real ordering bug**: `_annotate` (`promote.py:105`) runs before
  `run_promotion_lint`'s gate-1 schema validation (`promotion_lint.py:334`).

**Refinements made during plan-writing (kept here so spec↔plan don't drift):**
- `promotion_lint.py:212` gate-5 `next()` is **unreachable through the lint** —
  gate 1 validates the `Problem` and builds the KG from it before gate 5 runs, so
  `chain[-1]` is always a real procedure step. We still harden it to
  `next(..., None)` marked `# pragma: no cover` (matching the sibling convention
  at `promotion_lint.py:345`), but with **no dedicated test**.
- `learner_model_seed.py:251,255` `misconceptions_to_entities` KeyError hardening
  is **deferred to Phase 2 (H3 wiring)**: it is fully dormant while
  `misconceptions=[]` and touches the frozen §8 seed converter, so it is better
  changed alongside the wiring that exercises it. (H1's `link_opposes` bare-key
  bug is still fixed now, via the shared helper.)

Not in scope (verified safe): the dedup cosine NaN routing — `_cosine`
(`dedup.py:64-80`) already guards all-zero vectors; a NaN component routes to
"distinct" (no spurious merge). Full NaN guard deferred to Phase 4.

## Testing (TDD — failing test first)

1. **BLOCKER (fail-first):** `test_tag_mint` — a prereq draft with **bare** ids
   (e.g. `[["solve_p2", "bernoulli"]]`) currently raises `TagMintError`; after the
   fix the edge inserts.
2. **H1:** a misconception fixture whose `opposes` is a bare key currently raises
   `KeyError`; after the fix it links (covers the shared helper even though the
   feature stays unwired).
3. **H4 guard:** a mixed prereq table → `personalization_read`/`_select`
   correctness (auto ref-node edges consumed within-concept; seed concept→concept
   edges excluded; `prereqs_mastered` unaffected).
4. **Robustness:** missing-`content` retrieval row → candidate continues; gate-5
   no-terminal → clean reject; malformed `_annotate` step → clean reject;
   `misconceptions_to_entities` bad key → `TagMintError`.
5. Full `pytest apollo/provisioning/ -v` green; then E2E `drain_one_provision.py`
   → `:Canon` node written.

## Constraints / conventions

- Branch `ApolloRun`. Never push to `main`. Don't merge any PR — the owner merges
  every PR himself; open the PR, report URL + CI, stop.
- No new packages without asking (`fitz`/PyMuPDF already available).
- Supabase: "staging" = `hjevtxdtrkxjcaaexdxt` (test DB). "Apollo" project =
  PROD — never write to prod.
- Keep structured-JSON-from-LLM + comprehensive per-stage debug logging.
- Update `docs/architecture/apollo.md` in the same commit as the code changes:
  the BLOCKER fix, the `apollo_entity_prereqs` two-kind contract, the deferred
  items (H2/H3/H5/scope-asymmetry/fan-out/hygiene), and the misconception
  flag-gated-no-op note.
