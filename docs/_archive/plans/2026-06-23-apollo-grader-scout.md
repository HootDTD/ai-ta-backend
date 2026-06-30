# Apollo Grader Node-Recovery — Cold-Eyes Scout Report

**Date:** 2026-06-23
**Branch:** feat/apollo-grader-node-recovery (off origin/staging, PR #63 work in base)
**Spec:** docs/superpowers/specs/2026-06-23-apollo-shadow-grader-node-recovery-design.md
**Role:** scope/landmine validation only — no plan, no edits.

## Baseline health (run, not fixed)

`.venv/Scripts/python.exe -m pytest apollo/resolution apollo/grading apollo/graph_compare -q`
→ **429 passed, 0 failed in 4.47s.** Suite is GREEN at baseline. sympy 1.14.0 installed.

## Scope verdict

Spec scope is ACCURATE — no correction. Four-phase, additive, shadow-gated,
single-domain (backend `apollo/**`). One NEW file (`equation_alignment.py`),
plus surgical edits to 4 existing modules + their tests + owner doc. Confirmed
this does NOT touch grade-math (`graph_compare/{core,scores,coverage,bisimilarity,soundness}.py`)
nor the live grade path. The work is correctly `feller:do`-shaped (multi-file,
new capability), not a fast-path fix.

## LANDMINES (design around these)

### L1 — BLOCKING brittle test on the method tables (Phase 1a)
`apollo/resolution/tests/test_candidates.py:62-71` `test_method_confidence_caps_match_spec`
asserts `METHOD_CONFIDENCE_CAP == {exact:1.0,…,unresolved:0.0}` as an EXACT dict
literal AND `set(RESOLUTION_METHODS) == set(METHOD_CONFIDENCE_CAP)`. Adding
`derived:0.95` breaks BOTH assertions. This test MUST be updated in the same
change that adds the method — it is not optional. (It is the only exact-snapshot
of these tables; no length/tuple-order snapshot found elsewhere.)

### L2 — D1 reorder: compute_normalization_confidence needs the AuditedGrade object, not just findings
`compute_normalization_confidence(audited_grade, resolution)` (normalization_confidence.py:38)
reads ONLY `audited_grade.findings` (line 56) + resolution — it does NOT use any
other AuditedGrade field. So the D1 reorder inside `build_audited_grade` is
feasible WITHOUT constructing the object early: compute nc over the local
`new_findings` tuple directly. Cleanest path is a small pure helper that takes
`(findings, resolution)` and have both the existing public
`compute_normalization_confidence(audited, resolution)` AND the in-orchestrator
1c gate call it — so the persisted nc at done_grading.py:265 stays byte-identical.
DO NOT change the signature/behavior of the public function (persistence.py:293
feeds grader_confidence off its return). The reorder only adds an INTERNAL nc
computation to gate `apply_abstention`; the returned/persisted value is untouched.
Obstacle check: NONE — no circular import, nc reads only findings.

### L3 — apply_abstention is called in 3 places; new kwarg must be keyword-only-safe
`apply_abstention` call sites: audited_grade.py:196 (the real one),
test_abstention.py:25/130/164/173, corpus.py fixture (via build_audited_grade).
It is `*`-keyword-only with all-defaulted optionals, so adding
`normalization_confidence: float = 1.0` (defaulted) is BACKWARD-COMPATIBLE — the
fixture/corpus path that does not pass it keeps neutral 1.0 (no abstain). Good.

### L4 — abstention gate-order snapshot test (Phase 1c)
`test_abstention.py:122-140` `test_reasons_deterministic_order` asserts the EXACT
5-tuple of reasons in gate-declaration order (unresolved→parser→audit→misconception→ref).
The new `REASON_LOW_NORMALIZATION_CONFIDENCE` must be inserted at a DECIDED
position and this tuple updated. `ABSTENTION_THRESHOLDS` is NOT exact-snapshotted
anywhere (only individual keys are read), so adding `min_normalization_confidence`
to the dict is safe — but the reason-order test is a hard gate.

### L5 — done_grading_unit mocks build_audited_grade + asserts its kwargs
`test_done_grading_unit.py:329/518/547/583` snapshot `build_audited_grade.call_args.kwargs`.
Phase 1c reorders the BODY of build_audited_grade but its SIGNATURE/kwargs are
unchanged, so these stay green — confirm no new REQUIRED kwarg is added to
build_audited_grade itself (keep any 1c addition internal or defaulted).

### L6 — Candidate is a frozen dataclass with NO defaults today (Phase 1b)
`Candidate` (candidates.py:56-73) has 8 positional-capable fields, none defaulted.
Adding `exact_aliases: tuple[str,...] = ()` is safe ONLY if appended LAST (a
defaulted field after non-defaulted is required by dataclass rules). All current
constructors use KEYWORD args (candidates.py:96, :126; test fixtures), so a
trailing defaulted field will not break positional callers. No exact-field
snapshot test of Candidate found — but verify test_candidates fixtures don't
assert field count.

### L7 — NodeType has NO numeric ordering for the Phase 0.2 tiebreak
`NodeType` (ontology/nodes.py:18-25) is a `Literal[str]`, not an IntEnum. The
spec's "lowest node_type" tiebreak therefore means **lexicographic string
comparison** of the literal values ("condition" < "definition" < "equation" <
"procedure_step" < "simplification" < "variable_mapping"). There is NO declared
canonical priority order — plan must pick `min(types)` (string) explicitly and
document that the choice is deterministic-but-arbitrary (the tiebreak is a
should-never-fire safety net after Phase 0.1 type-gates the LLM path).

### L8 — derived@0.95 cap is ABOVE the 0.85 floor (D2 — do NOT raise the floor)
Confirmed: caps derived=0.95, alias=0.92 both sit above min_normalization_confidence=0.85.
At the 0.85 starting value, ONLY fuzzy(0.80)/llm(0.75)-backed scored findings
abstain. This is intended per spec §10/§16.5 (the number is calibration-owned).
A reviewer's instinct to "raise 0.85 to catch derived forms" is a TRAP — reject it.

### L9 — new file faces BLOCKING ruff + mypy (equation_alignment.py)
CI `quality` + `typecheck` jobs (ci.yml:52-66, :98-112): ADDED `.py` files are
BLOCKING for `ruff check`, `ruff format --check`, AND `mypy --config-file mypy.ini`.
mypy.ini is lenient base BUT new files must be mypy-clean (disallow_untyped_defs
is False globally, so full annotations not strictly required, but the file must
not error). All SymPy calls return `Any` — wrap in typed helpers and pin the
return types. Patch coverage ≥95% via diff-cover vs origin/staging is BLOCKING.

### L10 — D4 econ harness: data + helpers exist to mirror
`apollo/subjects/macroeconomics/concepts/nominal_vs_real_gdp/problems/{problem_01,problem_02}.json`
both EXIST on this branch. The deterministic no-infra harness should mirror the
helpers in `test_derived_equation_resolution.py`: `_load_problem_02` (:61),
`_problem_02_inputs` (:65, wraps `build_problem_candidates`), `_eq_node` (:76),
`_proc_node` (:86) — and the resolve→build_student_canonical seam (:135, :183).
Q4 real_gdp_from_deflator (symbolic) is the POSITIVE case; Q5 real_gdp_growth
(numeric-computed) is the NEGATIVE control that deliberately must NOT resolve.

## Phase-1a tier insertion seam (confirmed clean)
`_content_match` (resolver.py:76-126): insert `match_equation_alignment` between
`match_symbolic` (:86-89) and the lexical block (:91). First-hit-wins; equation
nodes never reach lexical/misconception competition. type_compatible filter at
:76 already screens candidates before any tier runs.

## DO-NOT-TOUCH purity tests (must stay green)
test_core.py:245 test_grade_attempt_is_pure; test_scores.py:235
test_sub_scores_pure_same_input_same_output; test_student_canonical.py:195
test_builder_is_pure_same_input_same_output. Plus the package-seam re-export
test test_package_seam.py:57 (build_student_canonical identity).

## Drift contract
Owner doc `ai-ta-backend/docs/architecture/apollo.md` (owns apollo/**) must be
reconciled in the implementation commit; last_verified bumps from 2026-06-23.
`equation_alignment.py` falls under existing apollo/** glob — no new glob needed.
