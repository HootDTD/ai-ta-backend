# Plan: Apollo Graph-Grader Phase 1a — Deterministic Equation-Alignment Tier (+ econ before/after harness)

**Goal:** Add a deterministic `derived` resolution tier that aligns a student equation stated as a *solved/rearranged form* of a reference equation to that reference entity, so it stops landing `unresolved` — without crediting numeric-coincidence, sign-flipped, or undeclared-simplification forms.
**Architecture:** Pure resolver tier, upstream of `build_student_canonical`. Touches `apollo/resolution/**` only (+ one new no-infra harness script + owner doc). Grade-math byte-identical.
**Tech stack:** Python 3.11, pytest (`.venv/Scripts/python.exe -m pytest`), sympy **1.14.0 (PINNED)**, ruff + mypy BLOCKING on new `.py`, diff-cover ≥95% patch vs `origin/staging`.

---
provides:
  - apollo/resolution/equation_alignment.py::match_equation_alignment
  - RESOLUTION_METHODS += "derived"; METHOD_CONFIDENCE_CAP["derived"] = 0.95
  - resolver "derived" tier (resolver._content_match), visible in tier_counts histogram
  - scripts/econ_grading_delta.py (no-infra before/after metrics harness, D4)
consumes:
  - apollo/resolution/tiers.py::_extended_locals, _zero_form (REUSED, not reimplemented)
  - apollo/resolution/candidates.py::METHOD_CONFIDENCE_CAP, RESOLUTION_METHODS
  - apollo/resolution/structural.py::ScoredMatch, type_compatible
  - apollo/graph_compare/problem_inputs.py::build_problem_candidates (symbolic_mappings channel; PR #63, unchanged)
  - apollo/subjects/macroeconomics/concepts/nominal_vs_real_gdp/problems/problem_01.json (Q4 real_gdp_from_deflator)
depends_on:
  - PR #63 (MERGED into base): folds per-simplification content.substitution into symbolic_mappings. Phase 1a EXTENDS the SAME symbolic_mappings channel — do NOT duplicate _collect_symbolic_mappings.
  - Phase 0 (resolver safety) is sequenced BEFORE Phase 1 in the spec, but is NOT in this plan's scope (Phase 1a is additive + first-hit-wins, independent of the Phase-0 LLM type-gate). Note in Risks.
---

## Overview

Spec §6. A student equation that is a *solved/rearranged form* of a reference equation
(e.g. `realGDP = nomGDP/(PI/100)` for the deflator definition `deflator = (nomGDP/realGDP)*100`,
or the pressure-cancelled Bernoulli) is not sign-exact-equal to the reference, so the existing
symbolic tier (`tiers.match_symbolic`, `tiers.py:183-202`) rejects it and it lands `unresolved`.
This raises `unresolved_rate` and drops every edge incident to it (one unresolved endpoint ⇒
edge dropped, `canonical.py:269-285`).

Phase 1a adds ONE new pure tier — `match_equation_alignment` — that, for each free symbol `x`
in the reference zero-form `r`, computes `solve(Eq(r,0), x)` and tests whether any solved branch's
zero-form matches the student zero-form `s` symbolically (under DECLARED `symbolic_mappings` only).
First candidate with any matching branch aligns → new method `derived`, cap `0.95`.

Placement: in `resolver._content_match`, immediately AFTER the `match_symbolic` block
(`resolver.py:86-89`), BEFORE the lexical tiers (`resolver.py:91`). First-hit-wins ⇒ a
substitution-collapsible form still reports `symbolic@0.98`; only solve-for-variable forms report
`derived@0.95`. Intended.

This EXTENDS PR #63 (already merged into base): #63 folds per-simplification `content.substitution`
maps into `symbolic_mappings` consumed by the UNCHANGED symbolic tier; it does NOT `solve()` the
reference. Phase 1a reuses the SAME `symbolic_mappings` channel (no new input). Do NOT touch or
duplicate `_collect_symbolic_mappings` (`problem_inputs.py:65-88`).

D4: the full local-stack macro probe (`scripts/run_macro_probe.py`) is HUMAN-GATED infra and NOT
runnable by agents. Phase 1a instead OWNS a NEW deterministic NO-INFRA before/after harness
(`scripts/econ_grading_delta.py`, T5) that drives `build_problem_candidates → resolve_attempt →
build_student_canonical → grade_attempt` over the real Q4 problem_01 + a hand-authored strong
student KG, and prints a JSON metrics dict. Phase 1a is SYMBOLIC-ONLY: it resolves Q4
`real_gdp_from_deflator` (symbolic rearrangement) but deliberately does NOT resolve Q5
`real_gdp_growth` (numeric-computed form) — that numeric case is the documented recall gap and a
NEGATIVE control (T4).

## Shadow-gating invariant

ALL changes run inside `APOLLO_GRAPH_SIM_SHADOW_ENABLED`. `APOLLO_GRAPH_SIM_LIVE_ENABLED` stays
OFF — never flipped here. No student-facing grade changes. Phase 1a is upstream of canonical and
adds one resolver tier; the live LLM grade path (`done.py:272-293`, `overseer/coverage.py`,
`overseer/rubric.py`) is untouched.

## Prior art (sibling tiers)

- `apollo/resolution/tiers.py:183-202` — `match_symbolic`: the tier to MIRROR. Equation-only
  (`node.node_type != "equation" → None`), iterates candidates, `cand.node_type != "equation" or
  cand.symbolic is None → continue`, returns `(cand, method, raw_score)` `TierHit` or `None`.
- `apollo/resolution/tiers.py:160-180` — `_symbolic_equiv`: the comparison to MIRROR. Builds
  `_extended_locals`, `_zero_form` for both sides, subs mappings on BOTH zero-forms
  (`b.subs(Symbol(sym), repl_expr)`, `:175-176`), then `simplify(a-b)==0` wrapped in
  `try/except → False` (`:177-180`). Phase 1a REUSES `_extended_locals` (`:128`) and `_zero_form`
  (`:143`) by import; it does NOT reimplement them.
- `apollo/resolution/resolver.py:86-89` — `match_symbolic` dispatch block in `_content_match`:
  the EXACT insertion-point template (call tier → if hit, unpack `cand, method, _` → return
  `ScoredMatch(node.node_id, cand, method, METHOD_CONFIDENCE_CAP[method])`).
- `apollo/resolution/resolver.py:205-213` — `_resolved`: re-derives confidence from
  `METHOD_CONFIDENCE_CAP[method]` DYNAMICALLY. Adding `"derived"` to the cap map ⇒ no change here.
- `apollo/graph_compare/tests/test_derived_equation_resolution.py:61-93` — helper templates
  (`_load_problem_02`, `_problem_02_inputs`, `_eq_node`, `_proc_node`) to MIRROR for the econ
  live-seam test + harness.

## Do-not-touch ledger (purity guarantee)

Byte-identical (spec §4/§12): `apollo/graph_compare/{core,scores,coverage,bisimilarity,soundness}.py`.
Purity tests that MUST stay green:
- `apollo/graph_compare/tests/test_core.py:245` `test_grade_attempt_is_pure`
- `apollo/graph_compare/tests/test_scores.py:235` `test_sub_scores_pure_same_input_same_output`
- `apollo/graph_compare/tests/test_student_canonical.py:195` `test_builder_is_pure_same_input_same_output`

Do-not-touch live grade: `apollo/handlers/done.py:264-293`, `apollo/overseer/coverage.py` (LLM
matcher — name collides with `graph_compare/coverage.py`, UNRELATED), `apollo/overseer/rubric.py`.

NOT in Phase 1a scope (later phases of the spec): `abstention.py`, `normalization_confidence.py`,
`audited_grade.py`, `canonical.py`, `done_grading.py`. Do not edit them.

## Layered tasks (ORDER MATTERS)

TDD order: the standard layer order (repository→DTO→service→controller→module) does not apply —
this is a pure-function resolver change, not a CRUD feature. The executable order is:

**T5-BEFORE → T1 → T2 → T3 → T4 → T5-AFTER**.

Rationale: T5-BEFORE captures the BEFORE metrics (rearranged eq UNRESOLVED, USES edge dropped,
`edge_coverage≈0`) immediately after the harness exists and BEFORE any tier code lands, so the
delta is real. T1–T3 build the tier (T1 RED test first). T4 is the full test matrix. T5-AFTER
re-runs the harness (rearranged eq resolves via `derived`, edge retained, `edge_coverage` up) and
reconciles the owner doc in the SAME commit as the code.

---

### T5-BEFORE — Econ before/after harness (NEW script) + capture BEFORE (D4)

- **File (NEW):** `scripts/econ_grading_delta.py`
- **Run as:** `ai-ta-backend/.venv/Scripts/python.exe scripts/econ_grading_delta.py`
- **No infra:** pure in-process; `llm_adjudicator=None`; reads only the committed problem JSON.
  No DB, no Neo4j, no OpenAI. Mirrors the input/graph helpers in
  `test_derived_equation_resolution.py:61-93`.

**What it builds (mirror RESULTS.md §4.3 attempt-20, Q4 `real_gdp_from_deflator`):**
1. Reference inputs via the live seam:
   `build_problem_candidates(load(problem_01.json), {"misconceptions": []}, canon_key_by_canonical_key={})`.
   Confirmed `symbolic_mappings == {"PI": "deflator"}` (problem-level `{}` + simplification
   `deflator_is_price_index` substitution `{"PI": "deflator"}`, `problem_01.json:42-51`).
2. A hand-authored STRONG student `KGGraph`:
   - `stu_eq_base` equation `deflator - (nomGDP/realGDP)*100` → resolves `eq.gdp_deflator` via EXACT
     symbolic (sign-exact identity). Control node: resolves BEFORE and AFTER.
   - `stu_eq_rearranged` equation `realGDP - nomGDP/(PI/100)` → the REARRANGED form. BEFORE:
     UNRESOLVED (symbolic tier rejects). AFTER: `resolved`/`eq.gdp_deflator` via `derived@0.95`.
   - `stu_proc` procedure_step (e.g. action "rearrange the deflator definition to solve for real
     GDP") resolved by a pure stub adjudicator → `proc.rearrange_for_real_gdp` (so the proc endpoint
     is NOT the variable under test).
   - `USES` edge `stu_proc → stu_eq_rearranged` (`from_node_type="procedure_step"`,
     `to_node_type="equation"`). BEFORE: dropped (rearranged endpoint unresolved). AFTER: retained.
3. Pipeline: `resolve_attempt(student, inputs.candidates, llm_adjudicator=<proc-only stub>,
   symbolic_mappings=inputs.symbolic_mappings)` → `build_student_canonical(student, resolution)` →
   `grade_attempt(student_canonical, reference_graph)` where
   `reference_graph = build_reference_canonical(load(problem_01.json))` (`canonical.py:111` — pure,
   no infra). `grade_attempt` (`core.py:85`) is pure, in-memory.
4. PRINT a JSON dict (stdout, `json.dumps(..., indent=2, sort_keys=True)`):
   - `per_node`: list of `{content, resolution, method}` for every student node.
   - `unresolved_rate`, `dropped_edge_count`.
   - `sub_scores`: `{coverage, node_coverage, edge_coverage, scoping, usage}` from `grade_attempt`'s
     sub-scores.

**Determinism:** no randomness; stub adjudicator returns a fixed dict; sympy 1.14.0. Same output
on repeat runs.

- **Capture BEFORE:** run the harness against CURRENT base code (before T1–T3). Save stdout into a
  fenced block in the owner-doc reconcile section / commit message and into a `# BEFORE (captured
  <date>, base <sha>)` comment block at the bottom of `econ_grading_delta.py`. Expected BEFORE
  signature: `stu_eq_rearranged` → `{resolution: "unresolved", method: "unresolved"}`,
  `dropped_edge_count >= 1` (the USES edge), `edge_coverage ≈ 0`/`usage ≈ 0`.
- **RED test for the harness itself** (so the harness is covered by the 95% gate):
  `scripts/tests/test_econ_grading_delta.py` (NEW) — calls a `run_delta() -> dict` entrypoint the
  script exposes (keep `if __name__ == "__main__": print(json.dumps(run_delta()))` thin so the body
  is importable and testable). Assert structural keys present and types; assert the BEFORE
  invariant `out["per_node"][<rearranged>]["resolution"] == "unresolved"` runs green on base. (This
  same test file gets a second assertion flipped to the AFTER invariant in T5-AFTER.)
- **Minimal change:** new file(s) only; no edits to existing modules.
- **Verify:** `.venv/Scripts/python.exe scripts/econ_grading_delta.py` prints valid JSON;
  `.venv/Scripts/python.exe -m pytest scripts/tests/test_econ_grading_delta.py -q` green on base.
- **Patch-coverage note:** the script body is exercised by `run_delta()` in the harness test;
  the `__main__` guard line may be `# pragma: no cover`. Target 100% of the new lines except the
  guard.

---

### T1 — New pure module `apollo/resolution/equation_alignment.py` (RED test FIRST)

- **File (NEW):** `apollo/resolution/equation_alignment.py`
- **Signature:** `match_equation_alignment(node: Node, candidates: tuple[Candidate, ...], *,
  mappings: dict[str, str]) -> TierHit | None` (mirror `match_symbolic` signature/return; `TierHit
  = tuple[Candidate, str, float]` from `tiers.py:36`). Return `(cand, "derived", 0.95)` on align
  (raw score = the cap; the resolver re-derives confidence from the cap anyway).
- **Imports (REUSE, do NOT reimplement):** `from apollo.resolution.tiers import _extended_locals,
  _zero_form, student_surface_text` (or `TierHit`); `from sympy import Symbol, solve, simplify,
  nsimplify` (Rational-ize floats via `nsimplify(..., rational=True)`); typed helpers around SymPy
  (returns `Any`) to satisfy mypy (L9).

**RED test FIRST** — write `apollo/resolution/tests/test_equation_alignment.py` POSITIVE case
before the module body:
```
def test_deflator_rearrangement_aligns_via_derived():
    # reference deflator def, declared {PI: deflator}; student rearranged-for-realGDP form
    cand = _eq_candidate("eq.gdp_deflator", "deflator - (nomGDP/realGDP)*100")
    node = _eq_node("stu", "realGDP - nomGDP/(PI/100)")
    hit = match_equation_alignment(node, (cand,), mappings={"PI": "deflator"})
    assert hit is not None
    assert hit[0].canonical_key == "eq.gdp_deflator"
    assert hit[1] == "derived"
    assert hit[2] == 0.95
```
This FAILS (ImportError / module absent) → RED. Then write the minimal module.

**Algorithm (minimal change):**
1. `if node.node_type != "equation": return None`.
2. `student_sym = student_surface_text(node)`; if falsy → `None`.
3. `maps = mappings or {}`. Build `ld = _extended_locals(student_sym, *cand.symbolics..., *maps.values())`
   per candidate (or one shared ld over all strings — mirror `_symbolic_equiv`'s per-pair build).
4. Compute student zero-form `s = _zero_form(student_sym, ld)`; if `None` → skip (non-match).
   Apply declared `maps` subs to `s` (same as `_symbolic_equiv` does to BOTH sides).
   Rational-ize: `s = nsimplify(s, rational=True)` inside try/except.
5. For each candidate (in candidate-set order): `cand.node_type == "equation" and cand.symbolic
   is not None` else continue. `r = _zero_form(cand.symbolic, ld)`; apply `maps` subs to `r`;
   Rational-ize.
6. For each free symbol `x` in `sorted(r.free_symbols, key=str)` (deterministic): try
   `branches = solve(Eq(r, 0), x)`; for each `branch` in `sorted(branches, key=str)`: build
   `bx = x - branch`; if `simplify(bx - s) == 0` → return `(cand, "derived", 0.95)`.
7. **EVERY SymPy call wrapped** `try/except Exception → treat as non-match` (mirror
   `_symbolic_equiv:177-180`). No raise ever escapes.
8. No candidate aligns → `None`.

**Hard exclusions (precision controls — encode + test in T4):**
- (i) NO numeric substitution of problem givens — `given_values` is never read here; a
  purely-numeric student form (e.g. `realGDP - 543.3*0.19`, or the Q5 `growth-(10739.0/2859.5)*100`)
  has no symbolic tie to `r` and stays unresolved.
- (ii) Only DECLARED `mappings` may be applied — no silent sign/condition drop. A form matching
  only after an undeclared simplification does NOT align.
- (iii) Rational-ize floats before comparison; determinism asserted under sympy 1.14.0.

- **Verify:** `.venv/Scripts/python.exe -m pytest apollo/resolution/tests/test_equation_alignment.py -q`
  RED→GREEN; `ruff check apollo/resolution/equation_alignment.py`,
  `ruff format --check apollo/resolution/equation_alignment.py`,
  `mypy apollo/resolution/equation_alignment.py` all clean (BLOCKING, ci.yml:52-66/98-112).
- **Patch-coverage note:** new module — every branch needs a test. Cover: align-success, each
  hard-exclusion → `None`, `_zero_form` None → skip, `solve` raising → caught. `try/except` defensive
  arms that cannot be triggered with valid inputs get `# pragma: no cover` mirroring `_symbolic_equiv`
  (`tiers.py:179`).

---

### T2 — New method `derived`, cap `0.95` (+ fix the exact-snapshot test L1)

- **File:** `apollo/resolution/candidates.py`
- **Change A** (`RESOLUTION_METHODS`, `:24-31`): insert `"derived"` BETWEEN `"symbolic"` and
  `"alias"` →
  `("exact", "symbolic", "derived", "alias", "fuzzy", "llm", "unresolved")`.
- **Change B** (`METHOD_CONFIDENCE_CAP`, `:35-42`): add `"derived": 0.95,` between `"symbolic"`
  and `"alias"`.
- `_resolved` (`resolver.py:205-213`) reads the cap dynamically → NO change there.

**L1 BLOCKING — same-change test update.** `apollo/resolution/tests/test_candidates.py:62-71`
`test_method_confidence_caps_match_spec` asserts the EXACT dict literal AND
`set(RESOLUTION_METHODS) == set(METHOD_CONFIDENCE_CAP)`. Both break unless updated in the SAME
edit. RED FIRST means: update the test to the new expected literal (adding `"derived": 0.95`)
BEFORE the source edit, run → RED (source still old), then edit source → GREEN.
```
assert METHOD_CONFIDENCE_CAP == {
    "exact": 1.00, "symbolic": 0.98, "derived": 0.95,
    "alias": 0.92, "fuzzy": 0.80, "llm": 0.75, "unresolved": 0.00,
}
```
- **Verify:** `.venv/Scripts/python.exe -m pytest apollo/resolution/tests/test_candidates.py -q`
  green; full-suite grep for any other exact snapshot of these tables
  (`.venv/Scripts/python.exe -m pytest apollo/resolution apollo/grading apollo/graph_compare -q`)
  green.
- **Patch-coverage note:** the cap map / methods tuple are data lines covered by the updated
  snapshot test. `derived@0.95` path is exercised end-to-end by T1 + T3 + T4.

---

### T3 — Tier dispatch in `resolver._content_match`

- **File:** `apollo/resolution/resolver.py`
- **Import:** add `match_equation_alignment` to the `from apollo.resolution.tiers import (...)`
  block region (`:45-51`) — or a separate
  `from apollo.resolution.equation_alignment import match_equation_alignment` near it. (Separate
  import keeps the new module decoupled; no circular import — `equation_alignment` imports from
  `tiers`/`candidates`/`structural`, none import `resolver`.)
- **Insertion point:** immediately AFTER the `match_symbolic` block (after `resolver.py:89`),
  BEFORE the lexical-tier comment (`resolver.py:91`). Mirror the symbolic block exactly:
```
    derived = match_equation_alignment(node, type_ok, mappings=symbolic_mappings)
    if derived is not None:
        cand, method, _ = derived
        return ScoredMatch(node.node_id, cand, method, METHOD_CONFIDENCE_CAP[method])
```
- Placement-after-symbolic ⇒ substitution-collapsible forms still report `symbolic@0.98`; only
  solve-for-variable forms report `derived@0.95`. `type_ok` already screens type-compat at `:76`,
  so equation candidates only.
- **RED test FIRST:** the live-seam assertion in T4 (`test_derived_equation_resolution.py` econ
  extension) is RED until this dispatch lands.
- **Verify:** `.venv/Scripts/python.exe -m pytest apollo/resolution apollo/graph_compare -q` green.
- **Patch-coverage note:** the 4 new lines are covered by T1's resolver-level test (if added) and
  by T4's live-seam test (`resolve_attempt` end-to-end).

---

### T4 — Tests (pure unit + live-seam extension) — spec §14

- **File (NEW, from T1):** `apollo/resolution/tests/test_equation_alignment.py` (pure unit).
- **File (EXTEND):** `apollo/graph_compare/tests/test_derived_equation_resolution.py` (live seam) —
  add an econ block reusing helper templates (`_load_*`, `_*_inputs`, `_eq_node`, `_proc_node`).

**POSITIVE (must align, method `derived`, cap 0.95):**
- Deflator: `realGDP = nomGDP/(PI/100)` ↔ `deflator - (nomGDP/realGDP)*100` under `{PI: deflator}`.
- Bernoulli solved: `v2 = sqrt(2*g*h1)` ↔ a torricelli/Bernoulli reference form.
- Pressure-cancelled Bernoulli (the `DERIVED_BERNOULLI` form already in
  `test_derived_equation_resolution.py:48`) — note: if a DECLARED equal-pressure substitution
  collapses it, the symbolic tier wins at `0.98` FIRST (intended); the `derived` positive must be a
  genuine solve-for-variable form, not a substitution-collapsible one. Pick the rearranged form
  whose match requires `solve()`, not just `subs()`.

**NEGATIVE CONTROLS (must NOT align → stay unresolved):**
- Sign-flipped rearrangement (e.g. `realGDP - nomGDP*(PI/100)` — wrong direction). `simplify(bx-s)!=0`.
- Unrelated equation (e.g. `Q = m*c*dT`). No shared structure.
- **Numeric-coincidence form** `growth - (10739.0/2859.5)*100` — the Q5 `real_gdp_growth` case.
  STAYS unresolved (hard exclusion i). This is the documented recall gap / negative control.
- Form needing an UNDECLARED simplification (apply a substitution NOT in `mappings`) — must NOT
  align (hard exclusion ii).

**Determinism asserts (L9, sympy pin):**
- Top-of-file: `import sympy; assert sympy.__version__ == "1.14.0"` (or a `pytest.mark`/skip guard
  documenting the pin). Run each positive twice and assert identical result (`solve` branch order +
  Rational-ization are stable).

**Live-seam econ extension** (in `test_derived_equation_resolution.py`): mirror Test 1/Test 3 but
on Q4 `problem_01.json` — assert `stu_eq_rearranged` resolves to `eq.gdp_deflator` via the content
tiers (`llm_calls == 0`) and that the `USES` edge survives `build_student_canonical` with both
endpoints resolved + `dropped_edge_count == 0`.

- **Verify:** `.venv/Scripts/python.exe -m pytest apollo/resolution/tests/test_equation_alignment.py
  apollo/graph_compare/tests/test_derived_equation_resolution.py -q` green.
- **Patch-coverage note:** these tests ARE the coverage for T1/T3. Confirm with
  `diff-cover coverage.xml --compare-branch=origin/staging --fail-under=95`.

---

### T5-AFTER — capture AFTER + owner-doc reconcile (SAME commit as code)

- **Re-run harness** post-T3: `.venv/Scripts/python.exe scripts/econ_grading_delta.py`. Expected
  AFTER signature: `stu_eq_rearranged → {resolution:"resolved", method:"derived"}` (key
  `eq.gdp_deflator`), `dropped_edge_count == 0` for the USES edge, `edge_coverage`/`usage` strictly
  up vs BEFORE.
- **Flip the harness test** assertion in `scripts/tests/test_econ_grading_delta.py` from the BEFORE
  invariant to the AFTER invariant (`resolution == "resolved"`, `method == "derived"`,
  `dropped_edge_count == 0`). Keep BOTH captured JSON blocks recorded in the script's trailing
  comment (`# BEFORE …` / `# AFTER …`) for the calibration record (§10).
- **Owner doc (drift contract, SAME commit):** edit `ai-ta-backend/docs/architecture/apollo.md`:
  add the `derived` resolution tier (between symbolic and alias) + `METHOD_CONFIDENCE_CAP["derived"]
  = 0.95` to the resolver-tier description; register `apollo/resolution/equation_alignment.py` under
  the doc's authority (already covered by `owns: apollo/**` — no new glob, but mention the module);
  bump `last_verified` to the implementation date.
- **Verify:** harness AFTER JSON matches expectation; `apollo.md` updated;
  full suite green; `diff-cover … --fail-under=95` passes.

## Per-task patch-coverage notes

Summarized inline per task. Global gate (CLAUDE.md): ≥95% PATCH coverage measured by
`diff-cover coverage.xml --compare-branch=origin/staging --fail-under=95` after
`.venv/Scripts/python.exe -m pytest --cov --cov-report=xml`. The only permitted exemptions are
defensive `try/except` arms that cannot fire with valid inputs (mirror the existing
`# pragma: no cover` on `tiers.py:179`) and the `__main__` guard in `econ_grading_delta.py`.

## Determinism / sympy-pin strategy

- sympy 1.14.0 is installed; PIN it in `test_equation_alignment.py` (version assert or skip guard).
- `solve(Eq(r,0), x)` branch order can vary by symbol; iterate `sorted(r.free_symbols, key=str)`
  and `sorted(branches, key=str)` so the FIRST match is deterministic across runs.
- Rational-ize floats (`nsimplify(expr, rational=True)`) before `simplify(bx - s) == 0` so a float
  literal in a student form does not defeat structural equality. Numeric-given substitution is NEVER
  performed (hard exclusion i) — Rational-ization is only float→Rational canonicalization, not
  value injection.
- Every SymPy call wrapped `try/except Exception → non-match` (mirror `_symbolic_equiv:177-180`).

## Drift contract (owner doc)

`ai-ta-backend/docs/architecture/apollo.md` (`owns: apollo/**, apollo/graph_compare/**,
apollo/grading/**, …`; `last_verified: 2026-06-23`). Reconcile in the SAME commit as T1–T3
(done in T5-AFTER): document the new `derived` tier + cap, mention `equation_alignment.py`
(covered by existing `apollo/**` glob — no new glob needed), bump `last_verified`.

## Files touched / tests added (explicit)

**Source (NEW):**
- `apollo/resolution/equation_alignment.py` — `match_equation_alignment` (T1)
- `scripts/econ_grading_delta.py` — no-infra before/after metrics harness (T5)

**Source (EDIT):**
- `apollo/resolution/candidates.py` — `RESOLUTION_METHODS += "derived"`,
  `METHOD_CONFIDENCE_CAP["derived"] = 0.95` (T2)
- `apollo/resolution/resolver.py` — import + 4-line dispatch block after `:89` (T3)
- `ai-ta-backend/docs/architecture/apollo.md` — owner-doc reconcile + `last_verified` bump (T5-AFTER)

**Tests (NEW):**
- `apollo/resolution/tests/test_equation_alignment.py` — pure unit (positives + 4 negative
  controls + determinism) (T1/T4)
- `scripts/tests/test_econ_grading_delta.py` — harness `run_delta()` structural + BEFORE→AFTER
  invariant (T5)

**Tests (EDIT):**
- `apollo/resolution/tests/test_candidates.py:62-71` — exact-snapshot update for `derived` (L1, T2)
- `apollo/graph_compare/tests/test_derived_equation_resolution.py` — econ Q4 live-seam block (T4)

## Risks & open questions

- **[HIGH] L1 exact-snapshot break.** `test_candidates.py:62-71` snapshots both
  `METHOD_CONFIDENCE_CAP` and the methods-set. MUST be updated in the same change as T2 or the
  suite goes red. Mitigated by RED-first ordering in T2.
- **[MEDIUM] Phase-0 not in scope.** The spec sequences Phase 0 (LLM type-gate at `resolver.py:192-193`,
  explicit merge type) BEFORE Phase 1. This plan does Phase 1a only. Safe because 1a is additive,
  deterministic, runs at `llm_adjudicator=None` in CI, and first-hit-wins on equation nodes that
  never reach the LLM remainder. But if Phase 0 lands in a separate branch, watch for merge order:
  no file overlap (`equation_alignment.py` new; `candidates.py` table additions; `resolver.py`
  insert at `:89` vs Phase-0 edit at `:192`) — low conflict risk.
- **[MEDIUM] derived@0.95 vs the 0.85 abstention floor (forward-looking, D2/L8).** Phase 1c
  (NOT in this plan) sets `min_normalization_confidence = 0.85`. `derived@0.95` sits ABOVE 0.85, so
  a `derived`-resolved finding does NOT abstain at the starting floor — intended (calibration-owned).
  Phase 1a must NOT lower the cap "to be safe" and Phase 1c must NOT raise the floor "to catch
  derived." Flagged here so the executor of a later phase does not relitigate.
- **[MEDIUM] Pressure-cancelled Bernoulli positive (T4).** If the equal-pressure substitution is
  DECLARED, the symbolic tier resolves it at `0.98` FIRST and `derived` never fires for that exact
  form. The `derived` positive must be a genuine solve-for-variable form (requires `solve()`, not
  just `subs()`). Choose the rearranged form accordingly; otherwise the positive asserts `symbolic`,
  not `derived`.
- **[MEDIUM] mypy on SymPy `Any` (L9).** `solve`/`simplify`/`nsimplify`/`free_symbols` return `Any`.
  Wrap in small typed helpers (`-> bool`, `-> object | None`) so mypy passes on the new file
  (BLOCKING gate). Mirror the typing already present around `_symbolic_equiv`.
- **[LOW] Harness reference-canonical construction — RESOLVED.** `grade_attempt` (`core.py:85`)
  takes a `ReferenceGraph` built no-infra by `build_reference_canonical(problem_dict)`
  (`canonical.py:111`). The harness uses it directly; full `sub_scores` (incl. `edge_coverage`/
  `usage`) are obtainable with zero infra. No fallback needed.
- **[LOW] `solve()` multi-branch cost.** Bounded: candidate sets are small (≤ ~8 equation
  candidates/problem), each with few free symbols. No perf gate, but keep the `try/except` tight so a
  pathological `solve()` degrades to non-match, never a hang.
