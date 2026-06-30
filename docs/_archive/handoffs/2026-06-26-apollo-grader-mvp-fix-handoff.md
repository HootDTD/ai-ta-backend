# Handoff — Apollo Shadow Grader: MVP fix to make it actually grade

**Date:** 2026-06-26
**Author:** Claude (Opus 4.8), orchestrated subagents
**Predecessor:** `docs/_archive/handoffs/2026-06-26-apollo-grader-node-recovery-handoff.md` (the work this tested)
**Full test report:** `docs/_archive/experiments/2026-06-26-apollo-grader-node-recovery-e2e/FEEDBACK.md`
**Spec:** `docs/superpowers/specs/2026-06-23-apollo-shadow-grader-node-recovery-design.md`
**Implementation branch (this handoff's fixes):** `fix/apollo-grader-abstention-mvp` (off `feat/apollo-grader-node-recovery`)

---

## 1. What this is

I ran the node-recovery shadow grader end-to-end (15 econ + 2 fluids attempts, live, full Postgres + Neo4j capture) and found it **abstains on 100 % of attempts** — it can never produce a usable grade. This handoff explains the root cause, then specifies the **MVP code changes to make it grade correctly without oversimplifying the model**, with exact file:line targets. Full findings + score matrices are in the FEEDBACK.md above; this doc is the *fix plan*.

---

## 2. Findings TL;DR

- ✅ Pipeline works end-to-end; **clean live per-metric matrix captured** for all 15 econ attempts (closes the predecessor handoff's open §5). Phase-1a `derived@0.95` resolution **fires live** (econ attempts 99/100).
- ❌ **G1 (CRITICAL): 100 % abstention, by construction.** Phase-1c's `normalization_confidence` floor (0.85) sits above the `llm` (0.75) / `fuzzy` (0.80) resolution caps, and conceptual nodes can only reach those tiers → every attempt with procedural content auto-abstains (even two *perfect-coverage* econ attempts, 93 & 96). Generalizes to fluids.
- ❌ **G6 (HIGH): concept-inference mis-route blocks fluids.** A 0-problem autoprovisioned decoy concept hijacks gpt-4o concept selection → `PoolExhaustedError` (409) on every fluids attempt.
- ⚠️ **G2 (HIGH): resolver recall is weak** (38 % econ nodes unresolved, 5/7 fluids-strong) → under-credits correct strong answers + (post-G1) keeps low-recall attempts abstaining on `unresolved_rate`.
- ⚠️ **G4 (HIGH): misconception detection mostly non-functional** (4/5 econ weak attempts get a false `soundness=1.0`; fluids misconception table unseeded).
- ⚠️ **G5 (MEDIUM): `scoping`/`procedure_order` vacuously 1.0** everywhere; edge sub-scores are diagnostic-only.
- No student-facing regression — the live grade is still the LLM matcher, which discriminates fine.

---

## 3. Issues encountered while testing (for reproduction)

1. **Local Neo4j container was down** — `docker start hoot-neo4j-local` (only boot blocker; everything else — Supabase DB, migration 031, `.env.local` override — was already in place).
2. **120 s HTTP read-timeout crashed the prior sweep** (the slow `/done` LLM-adjudicator call). Raised `scripts/apollo_grade_probe.py::_post` timeout to 300 s → clean 15/15 capture.
3. **Fluids 409 `pool_exhausted`** — the G6 routing bug. Unblocked for the test with a **reversible local-DB tweak** (re-parented decoy concept id 4 off search-space 1, then restored byte-identical). Local Docker DB only; no remote DB touched.

---

## 4. Why Phase 1c does what it does (and why it's wrong as built)

**The intent is legitimate.** Spec §8: before 1c, the *only* hard abstention trigger was `unresolved_rate > 0.35`. Phases 1a/1b deliberately *lower* `unresolved_rate` (they resolve more nodes), so messy, derived-form-heavy attempts that *should* abstain would start grading confidently. 1c adds a second brake: "if the grade rests on low-confidence *resolutions*, don't certify it." Sound goal — epistemic humility about guessed node mappings.

**The implementation is broken in three compounding ways:**

1. **It measures the wrong thing.** `normalization_confidence` = `MIN` over scored nodes of the **static per-tier cap** (`resolver.py:222` sets `ResolvedNode.confidence = METHOD_CONFIDENCE_CAP[method]`; `normalization_confidence.py:60-76` takes the MIN). So it scores *which tool was used*, not *whether the mapping is actually right*. A confident, correct `llm` match and a desperate one both read 0.75. The per-match fuzzy/LLM signal is discarded.

2. **The floor sits above the only tiers conceptual nodes can reach.** `abstention.py:37` floor = **0.85**. Caps (`candidates.py:36-44`): exact 1.00 · symbolic 0.98 · derived 0.95 · alias 0.92 · fuzzy 0.80 · llm 0.75. But `symbolic`/`derived` are equation-only (`tiers.py:191`, `equation_alignment.py:133`); `exact` needs a `label` the parser doesn't emit for procedure/simplification/definition/variable-mapping nodes (they declare only `action`/`purpose`, `ontology/nodes.py:88-90`); `alias` needs curated `exact_aliases` that shipped problems don't carry; `fuzzy` never fires for reference candidates (refs have empty `aliases`). **So in production a conceptual node either resolves via `llm` (0.75) or stays unresolved.** `0.75 < 0.85` always.

3. **MIN aggregation makes one conceptual node sink the whole attempt.** Since every real attempt has procedural/conditional content, and those nodes cap at 0.75, the MIN is ≤ 0.75 < 0.85 for *every* attempt that actually covers something. The brake was meant to be *selective* ("distrust weak attempts"); it is *categorical* ("abstain on everything").

The merged plan even encodes the blind spot as intentional (D2: "derived@0.95 & alias@0.92 sit ABOVE it — only fuzzy@0.80/llm@0.75 abstain; do NOT raise to catch derived"). What nobody reconciled: **conceptual nodes have no above-0.80 path in production, so "only llm/fuzzy abstain" means "every attempt with prose abstains."**

**Verdict: fix it, don't remove it.** Removing the brake reverts to the spec's known-insufficient state (unresolved_rate only). The right fix makes the brake judge a resolution *relative to what its node type could achieve*: an **equation** that falls back to llm/fuzzy is genuinely suspicious (it had exact/symbolic/derived available) → abstain; a **conceptual node** resolving via llm is at its ceiling → not suspicious → don't abstain. That preserves 1c's intent and kills the categorical-abstention bug.

---

## 5. MVP — make the grader produce usable, trustworthy grades

Goal: turn abstention from **categorical** (a bug) into **selective** (correct), and unblock concept routing — so the grader emits non-abstained grades where resolution recall is adequate (econ), enabling the spec §10 promotion calibration. Two focused, test-driven changes; everything else is explicitly deferred (§6).

### MVP-1 — Type-aware abstention floor (fixes G1)

Make `normalization_confidence` a **type-normalized** value: each scored node's confidence is judged against the max resolution confidence *realistically achievable for its node type*, not a flat absolute.

- **File:** `apollo/grading/normalization_confidence.py`. Add a per-type resolution ceiling and normalize before the MIN:
  ```python
  # Max resolution confidence realistically reachable per node type (production).
  # Equations have exact/symbolic/derived paths; conceptual nodes only the LLM tier
  # (no symbolic form, no curated aliases on shipped problems).
  RESOLUTION_CEILING_BY_TYPE = {"equation": 1.00}
  RESOLUTION_CEILING_DEFAULT = 0.75  # llm cap — the realistic ceiling for prose nodes

  def _type_normalized(node_type: str, cap: float) -> float:
      ceiling = RESOLUTION_CEILING_BY_TYPE.get(node_type, RESOLUTION_CEILING_DEFAULT)
      return min(1.0, cap / ceiling) if ceiling > 0 else 1.0
  ```
  In `_normalization_confidence_over`, look up each scored backing node's `node_type` (from the `ResolutionResult` / resolved node) and take `min(_type_normalized(node_type, cap) ...)` instead of `min(cap ...)`.
- **Keep** the floor constant at `0.85` in `abstention.py:37` (now meaningful: "resolved within 85 % of achievable"). Keep the reason string `REASON_LOW_NORMALIZATION_CONFIDENCE`.
- **Net effect** (validated against the captured run): equation-exact → 1.0; concept-via-llm → 0.75/0.75 = 1.0 (no abstain); **equation that fell to llm → 0.75/1.00 = 0.75 < 0.85 → abstains (correct)**. Econ strong attempts 90/93/96/99 stop abstaining; low-recall attempts still abstain via `unresolved_rate` (correct).
- **Consistency win:** the persisted `normalization_confidence` column and the `grader_confidence = nc × comparison_confidence` damper (`normalization_confidence.py:4-7`) both become meaningful — a clean strong attempt reads ~1.0 instead of a misleading 0.75. Grade *math* is untouched (`abstained` is a flag only; `audited_grade.py:70` carries `GradeResult` unchanged), so the byte-identity grade tests stay green.

### MVP-2 — Teachable-pool filter in concept inference (fixes G6)

Stop empty provisional concepts from being offered to `infer_concept_id`.

- **File:** `apollo/subjects/curriculum_db.py`, `list_course_concepts` (`:59-75`). Add a correlated `EXISTS` to the query so only concepts with a teachable problem are returned, using the **same** predicate as the downstream pool (`apollo/overseer/problem_selector.py::list_problems_for_concept`): `ConceptProblem.tier == 2 AND ConceptProblem.quarantined_at IS NULL`.
  ```python
  .where(
      Subject.search_space_id == search_space_id,
      exists().where(
          ConceptProblem.concept_id == Concept.id,
          ConceptProblem.tier == 2,
          ConceptProblem.quarantined_at.is_(None),
      ),
  )
  ```
- **Net effect:** the decoy `bernoulli-equation` (0 problems) never enters the candidate set → gpt-4o picks the real `bernoulli_principle` → fluids routes and grades, with no DB tweak needed.

### Verification (both)
- TDD: RED test first per change, then implement to GREEN.
- Run the **full `apollo/` suite** and reconcile every changed expectation as *corrected behavior* (never by gaming a test).
- **Patch coverage ≥ 95 %** on changed lines: `pytest --cov --cov-report=xml -q && diff-cover coverage.xml --compare-branch=origin/staging --fail-under=95`. ruff + mypy clean.
- Re-run the live econ probe (`scripts/run_macro_probe.py --skip-embed --skip-mining --tag .mvp`) and confirm **strong attempts now show `abstained=false`**; re-run fluids without the manual DB tweak and confirm it routes + grades.

---

## 6. Explicitly deferred (NOT in MVP — next phase, with reasons)

- **G2 resolver recall** (parser emitting canonical text for procedure steps; curated `exact_aliases` on shipped problems; better LLM adjudication recall). Large parser + content effort; it's the lever to reduce abstention *further* (esp. fluids/conceptual-heavy) once MVP-1 makes abstention selective. Not blocking "the grader runs."
- **G4 misconception detection** — depends on G2 recall *and* on the misconception store, which the user explicitly deferred (wants a trust-gradient/promotion pipeline; brainstorm before code — see `apollo-misconception-bank-decisions` memory). Seeding `apollo_misconceptions` for fluids concepts is a small data task that can ride along later; the *resolution* half is G2.
- **G5 vacuous-1.0 annotation** — reporting concern (flag `scoping`/`procedure_order` as vacuous when the reference has no such edges; never aggregate diagnostic-only edge scores into a headline). Do alongside the §10 calibration dashboard.
- **§10 promotion calibration** — only meaningful *after* MVP-1 lands; capture shadow-vs-LLM agreement then.

---

## 7. Branch / PR state

- MVP fixes on `fix/apollo-grader-abstention-mvp`, based on `feat/apollo-grader-node-recovery` (which carries the Phase-1c code being fixed). PR target is `staging` (stacked behind the node-recovery PR).
- `GITHUB_TOKEN` is stale (see `github-access-wiring` memory) — PR opening may need a token refresh; the branch + commits are prepared regardless.

---

## 8. Implementation status — DONE & live-verified (2026-06-26)

Both MVP fixes implemented (TDD), unit-tested, and **verified end-to-end on the local stack**. Branch `fix/apollo-grader-abstention-mvp`:

- **`79c6412`** `fix(apollo): type-aware abstention floor + filter unteachable concepts` — 10 files, +496/−51. Surgical (only `apollo/grading/normalization_confidence.py` + `audited_grade.py` + `handlers/done_grading.py` + their tests for G1; `apollo/subjects/curriculum_db.py` + tests for G6; `docs/architecture/apollo.md` drift). Cherry-picks cleanly onto `feat/apollo-grader-node-recovery`.
- **`e1f9235`** `docs: …` — FEEDBACK + this handoff.

**Tests:** full `apollo/` suite **1624 passed, 13 skipped** (pre-existing legacy skips); **100 % patch coverage** on changed lines; ruff + mypy clean; grade-math byte-identity preserved (no `*_score` changed). G6 used the real-Postgres testcontainer.

**Live verification (the proof it works on real data):**

- **G1 — econ re-run (attempts 107–121, `tag .mvp`):** abstention dropped from **15/15 → 7/15**. The 7 remaining all abstain on `unresolved_rate_above_threshold` — **zero** abstain on `normalization_confidence` (was 10/15 pre-fix). **4 of 5 strong + all 5 partial now produce certified (non-abstained) grades.** `normalization_confidence` is now type-normalized: 1.0 (exact-backed), **0.95** (the Phase-1a derived-resolved deflator attempts 116/117), 0.98 (symbolic). The only strong still abstaining (`real_gdp_growth`, attempt 119) does so on `unresolved_rate` — the deferred G2 recall gap, not the G1 floor.
- **G6 — fluids re-run WITHOUT the DB tweak (attempts 122/123, `tag .mvpfluids`):** routed to the real `bernoulli_height_change_find_v2` with **no 409** — the committed EXISTS filter excluded the still-present decoy concept id 4. Both variations graded; live LLM grade discriminates (strong 80 / weak 0). Shadow `normalization_confidence` 0.98/1.0 — the NC brake correctly did **not** fire; fluids still abstains on `unresolved_rate` (G2 recall).

**Net:** abstention is now **selective** (genuine under-resolution) instead of **categorical** (the bug). The shadow grader produces usable, non-abstained grades on well-resolved attempts → spec §10 promotion calibration is now feasible (start with econ strong/partial).

**Still open:** the deferred items in §6 (G2 resolver recall is the next lever — it's why low-recall attempts incl. `real_gdp_growth` and fluids still abstain; G4 misconceptions; G5 vacuous-1.0 annotation; §10 calibration capture). PR `fix/apollo-grader-abstention-mvp → staging` (stacked behind the node-recovery PR) not opened — `GITHUB_TOKEN` is stale (see `github-access-wiring` memory); branch + commits are ready locally.
