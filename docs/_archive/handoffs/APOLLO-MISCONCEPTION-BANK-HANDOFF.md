# Apollo Misconception-Bank Session Handoff

**Date:** 2026-06-23
**Scope:** Apollo graph-grading misconception system — investigation, two shipped
fixes, a preserved experiment, prior-art research, and a forward design take on
the deferred "store-unification" fix (fix a).
**Repo:** `ai-ta-backend` (branch model: cut from `staging`, PR into `staging`;
`ApolloV3` is prod and untouched).

---

## TL;DR

- Started from the macro graph-grading probe writeup
  (`ai-ta-backend/docs/experiments/2026-06-22-macro-graph-grading-probe/RESULTS.md`),
  drilling into the **misconception system** (defects D5/D6).
- **Shipped fix (b): soundness N/A** — an empty misconception bank now reports
  `N/A` instead of a fake `1.0`. **PR #62 → `staging`, CI green.**
- **Shipped the macro-probe code edits**, split from experiment scaffolding:
  **PR #63 → `staging`, CI green** (derived-equation resolution + generalized
  seeder + macroeconomics subject); the full experiment is preserved on branch
  **`experiment/macro-graph-grading-probe`** (pushed, no PR).
- **Deferred fix (a): misconception store unification.** A binary "approved-only"
  admission gate was **rejected as suboptimal**; the recommended direction is a
  **trust-gradient / promotion pipeline** (see §4). Needs a real design session
  before any code.
- **Nothing merged; nothing applied to any DB.** Migration 031 is local-only.

---

## 1. Context — the defect landscape

The macro probe (RESULTS.md) catalogued defects **D1–D7** in the graph-grading
pipeline. This session focused on the **misconception subsystem**:

- **D5 — dual-storage footgun.** Grading's `soundness` reads the
  `apollo_misconceptions` TABLE (via `overseer/misconception_bank.py::load_for_concept`),
  but the learner model + auto-provisioning write `apollo_kg_entities (kind='misconception')`.
  The two stores are **never synced** → seeding one does not reach grading.
- **D6 — vacuous 1.0.** An empty bank yields `soundness = 1.0` (reads as
  "verified sound") instead of "never checked" — a silent **fail-open**.

Other open defects (NOT addressed here, flagged for later): **D2** non-equation
nodes resolve weakly; **D3** LLM adjudicator violates the type-gate; **D4**
`DEPENDS_ON` density depresses `edge_coverage`; **D7** old-path vs graph-sim
divergence. **D1 (case-3, derived forms)** is *partially* addressed by PR #63's
derived-equation resolution.

---

## 2. What shipped

### 2.1 Fix (b) — soundness N/A on an empty bank  ·  PR #62  ·  CI GREEN

**Branch:** `fix/apollo-soundness-na-empty-bank` (off `staging`, commit `507292b`).
**Design:** `ai-ta-backend/docs/design/2026-06-23-apollo-soundness-na-sentinel.md`
(committed in the PR).

**Mechanism (hybrid, split on the existing layer boundary):**
- Pure score-math (`soundness.py` / `bisimilarity.py` / `scores.py` / `core.py`):
  `soundness_score` and the `contradiction` sub-score become `float | None`;
  `None` = bank empty. `bisimilarity_score` **renormalizes to `coverage`** when
  soundness is `None` (the harmonic mean of one available dimension *is* that
  dimension — never ×0, never a fake 1.0; stays NaN-free, in-range).
- Persistence: the `soundness_score` / `bisimilarity_score` columns stay
  `REAL NOT NULL` (fed the coverage-only fallback); a new
  **`soundness_applicable BOOLEAN NOT NULL DEFAULT true`** column carries the
  truth; `contradiction_score` persists `NULL`. **Migration 031**
  (`031_apollo_soundness_applicable.sql`, additive, backfill-safe).
- Orchestration (`handlers/done_grading.py`): `bank_applicable = bool(entries)
  and sess.concept_id is not None`; logs `soundness_not_applicable_empty_bank`;
  records abstention reason `misconception_bank_empty` **reason-only** — does NOT
  set `abstained` (coverage + the other six dimensions still valid; Layer-3 still
  updates).

**Files (9 source + migration + 8 tests + owner doc):** `soundness.py`,
`bisimilarity.py`, `scores.py`, `core.py`, `grading/persistence.py`,
`grading/abstention.py`, `grading/audited_grade.py`, `handlers/done_grading.py`,
`persistence/models.py`, `database/migrations/031_*.sql`, the matching tests, and
`docs/architecture/apollo.md` (drift contract, `last_verified` bumped).

**Quality:** built via subagent-driven development (TDD implementer → independent
verifier → cold-eyes task review → final opus whole-branch review). **100% patch
coverage** (24/24 changed lines), 1515 passed, ruff/mypy clean. Final review
verified no external consumer reads the now-`Optional` scalars unguarded (only
`persistence.grade_to_run_spec` does, with a None guard).

### 2.2 Macro-probe code edits  ·  PR #63  ·  CI GREEN  +  preservation branch

The macro probe had ~50 uncommitted files mixing real code edits, new subject
data, experiment scaffolding, and docs. We **split** it (user's choice):

**PR #63** — `feat/apollo-derived-eq-seeder-macro` (off `staging`, commits
`2babe90` + ruff/mypy fix `3eff114`). The mergeable subset:
- **Derived-equation resolution (partially closes D1/case-3):**
  `problem_inputs.py::_collect_symbolic_mappings` now harvests per-simplification
  `substitution` maps (+ bernoulli `problem_01/02.json` data), so a
  derived/computed/solved form resolves to its governing entity.
- **Generalized learner-model seeder:** `learner_model_seed.py` +
  `scripts/seed_apollo_learner_model.py` (`authored_definitions_from_spec()`),
  bernoulli shim kept for back-compat.
- **Macroeconomics subject:** new `apollo/subjects/macroeconomics/` (2 concepts,
  5 problems; OpenStax Ch.6, CC BY 4.0) + `competition.py` antonyms.
- 100% patch coverage, ruff/mypy clean, `apollo.md` reconciled. `server.py`'s
  local-only `.env.local` loader was **excluded** (lives only on the snapshot).

**`experiment/macro-graph-grading-probe`** (commit `b7785de`, pushed, **no PR**)
— the complete 75-file snapshot: probe scripts/tooling, `supabase/` local config,
handoff docs, the design docs + research memo from this session, etc. Nothing lost.

> CI note: PR #63's first run failed `quality`+`typecheck` — CI blocks on
> **newly-added** files with `ruff format --check` + mypy, and `apollo/**/tests/`
> files ARE mypy-blocking (only top-level `tests/` is excluded). Fixed by
> ruff-formatting the new tests + typing `test_learner_model_seed_generic.py`
> (SQLAlchemy-stub `Column[int]`/`FromClause` gaps → targeted `# type: ignore`).

---

## 3. Prior-art research (how others build misconception banks)

Full cited memo:
`ai-ta-backend/docs/research/2026-06-23-misconception-bank-prior-art.md`
(on `experiment/macro-graph-grading-probe`).

- **Authored banks dominate.** MATHia/Cognitive Tutor = hand-authored "buggy
  production rules"; constraint-based tutors (SQL-Tutor) have *no* misconception
  library at all (a constraint violation *is* the error).
- **Dynamic generation is precedented only as candidate-gen behind a HUMAN GATE**
  — ASSISTments mines "common wrong answers", Hint Factory mines remediation, but
  never publishes autonomously.
- **Pure LLM misconception generation is the weak link** — HEDGE (2024): only
  **37%** of GPT-4-generated misconceptions valid. LLM *detection* against a
  curated bank is strong (MAP@3 > 0.9) but hallucinates false positives unless
  **retrieval-grounded**.
- **Apollo's shape (authored bank + LLM-graded detection) IS the validated
  mainstream** — cf. the Vanderbilt/Eedi "Charting Student Math Misunderstandings"
  (Kaggle 2025): expert taxonomy + LLM free-text detection.

**Implication:** keep the bank human-authoritative; use the LLM for
retrieval-grounded *detection*, not free generation; treat any mined/LLM
misconception as a *proposal for review*.

---

## 4. Fix (a) — store unification: the problem and OUR TAKE

This is the deferred item. **Do not just implement a binary approved-only gate.**

### 4.1 The problem (D5)
Grading's soundness only sees `apollo_misconceptions` (the TABLE). Auto-provisioning
and the learner-model seeder write `apollo_kg_entities (kind='misconception')`.
Seeding the learner model therefore does **not** make grading detect contradictions
— the macro probe had to seed a *second* script to populate the table grading reads.

### 4.2 The mechanism we'd use — Option C (read-adapter union)
A workflow evaluated three options and chose **Option C**:
- Rewrite `overseer/misconception_bank.py::load_for_concept` to **union both
  stores**, folding `apollo_kg_entities kind='misconception'` rows into the
  `MisconceptionEntry` shape, deduped by `code == canonical_key` (the hand-authored
  table row wins on conflict), synthesizing the Socratic fields empty for
  store-2-only rows.
- **Zero migration** (the workflow corrected a stale assumption — on-disk migration
  max is **030**, not 026). One file changes (`misconception_bank.py`) + tests.
- Rejected **(A) unify** (would relocate the `vector(3072)`+HNSW onto kg_entities,
  which has no persisted vector) and **(B) dual-write** (forces the irresponsible
  `probe_question`/`rt_steps` write that auto-provisioning deliberately avoids).
- Bare-Option-C design:
  `ai-ta-backend/docs/design/2026-06-23-apollo-misconception-store-unification.md`
  (on `experiment/macro-graph-grading-probe`).

### 4.3 Why we did NOT ship it — the admission-gate decision
Option C **routes store-2 (auto-provisioned / learner-seeded) misconceptions into
grading**. Today that's benign (auto-provisioning currently mints zero
misconceptions). But the moment the stubbed auto-generation path is built, bare
Option C would feed **unvetted, possibly-LLM-generated** misconceptions straight
into the soundness score — exactly the anti-pattern the prior-art research warns
against (HEDGE: ~⅓ of LLM-generated misconceptions are invalid).

The obvious guard — a **binary `approved`-only filter** — was **rejected by the
user as suboptimal**: it just relocates the manual bottleneck (a human must
approve every machine-suggested misconception before it can ever affect a grade),
throwing away the "build over time" upside.

### 4.4 Recommended direction — a trust-gradient / promotion pipeline
Not an on/off switch. A spectrum of trust, so machine-suggested misconceptions
can *earn* their way into grading without a human in the hot path:

- **Shadow-grade unapproved misconceptions** — they influence a *shadow* soundness
  + get logged, never the student-facing grade. Auto-promote once they prove
  reliable on real data.
- **Confidence-weighted influence** — a low-trust misconception needs a higher
  detection confidence to count toward the grade.
- **Source-tiered trust** — `authored` = full, `data-mined` = medium,
  `LLM-generated` = shadow-only.
- **Admission loop** (the ASSISTments / Hint-Factory pattern):
  `mine → cluster → expert-label → admit`. Expert seeding at provisioning for
  cold-start.

This both honours "build over time" AND keeps grading safe. It also pairs with
Apollo's existing **retrieval-grounding adapter** — grounding contradiction
detection against the curated bank is the literature's prescribed fix for
detection false-positives.

A pragmatic **schema hook** for whichever store wins: a `source`
(`authored|mined|llm`) + `status` (`proposed|approved|shadow`) on the
misconception record (cheap to add now, expensive to retrofit), so grading reads
only `approved` (or applies tiered weighting) while proposals accumulate.

### 4.5 Open design questions for the next session (brainstorm first)
1. Unify vs. keep-two-stores once a `source`/`status` model exists — does the
   read-adapter (Option C) still win, or does a single store become cleaner?
2. What's the *promotion criterion* (N detections × confidence × correlation with
   low grades?) and who/what signs off?
3. Does "shadow soundness" need its own persisted column, or reuse the
   `soundness_applicable` machinery shipped in fix (b)?
4. Where does retrieval-grounding plug into contradiction detection?

### 4.6 Load-bearing facts verified this session
- **The Socratic tutoring loop is DEAD in v1.** `infer_misconception` is dead
  (`apollo/handlers/tests/test_chat_no_signals.py` *asserts* it's gone from the
  live chat handler); grading even loads `probe_question`/`rt_steps` but drops
  them in `_misconceptions_dict`. **Grading is the only live consumer of
  `apollo_misconceptions`** → Option C's "synthesize Socratic fields empty" is
  safe (no longer an open risk).

---

## 5. Artifacts, branches, PRs

| Thing | Where |
|---|---|
| Fix (b) PR (soundness N/A) | **#62** `fix/apollo-soundness-na-empty-bank` → `staging` (CI green) |
| Macro code PR | **#63** `feat/apollo-derived-eq-seeder-macro` → `staging` (CI green) |
| Full experiment snapshot | `experiment/macro-graph-grading-probe` (pushed, no PR) |
| Fix (b) design | `ai-ta-backend/docs/design/2026-06-23-apollo-soundness-na-sentinel.md` |
| Fix (a) design (bare Option C) | `ai-ta-backend/docs/design/2026-06-23-apollo-misconception-store-unification.md` (on experiment branch) |
| Prior-art memo | `ai-ta-backend/docs/research/2026-06-23-misconception-bank-prior-art.md` (on experiment branch) |
| Probe writeup | `ai-ta-backend/docs/experiments/2026-06-22-macro-graph-grading-probe/RESULTS.md` |
| Memory | `apollo-misconception-bank-decisions.md` (+ MEMORY.md index) |
| Local worktrees (kept for PR iteration) | `.worktrees/apollo-soundness-na`, `.worktrees/apollo-probe-code` |

---

## 6. State — what is intentionally NOT done
- **Nothing merged.** Both PRs await human review/merge into `staging`.
- **Migration 031 not applied** to any remote DB (human/CI does TEST → prod).
- **Promotion to `ApolloV3`** is a separate `staging → ApolloV3` PR.
- **Fix (a) not coded** — awaiting the trust-gradient design session.
- Open defects **D2, D3, D4, D7** (and the non-derived parts of D1) remain.
- `fix/apollo-retrieval-grounding` left clean at its tip; the post-merge macro
  work moved onto `experiment/macro-graph-grading-probe`.

---

## 7. Suggested next steps
1. Review + merge **#62** and **#63** into `staging` (then the human/CI migration-031
   apply for #62).
2. When ready, run a **brainstorming/design session for fix (a)** using §4.4–4.5 as
   the starting brief — output a revised design doc, then build it (subagent-driven,
   95% patch gate) the same way fix (b) was shipped.
3. Optionally split #63 if a reviewer prefers smaller PRs (derived-eq / seeder /
   subject are separable).
4. Clean up the two `.worktrees/*` once the PRs land.
