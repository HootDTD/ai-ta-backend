# Merged Plan: Apollo Graph-Grader Hardening — Node-Recovery (Phases 0 / 1a / 1b / 1c)

**Goal:** Make legitimately-correct student content resolve in the SHADOW graph-grader (derived-equation forms + curated exact aliases) while installing the precision guardrails (resolver type-safety, explicit merge type, self-loop guard, exact-only aliases, normalization-confidence abstention brake) that stop an easier resolver from over-crediting. All work is shadow-gated; no student-facing grade changes.

**Domains:** single — backend `ai-ta-backend/` (`apollo/**`). No db / frontend / ai-pipeline planners.
**Branch:** `feat/apollo-grader-node-recovery` (off `origin/staging`; PR #63 derived-eq work is in the base, commit 134391b).
**Test runner:** `ai-ta-backend/.venv/Scripts/python.exe -m pytest`. sympy **1.14.0 PINNED**.
**Source plans:**
- `ai-ta-backend/docs/_archive/plans/2026-06-23-apollo-grader-phase0-plan.md`
- `ai-ta-backend/docs/_archive/plans/2026-06-23-apollo-grader-phase1a-plan.md`
- `ai-ta-backend/docs/_archive/plans/2026-06-23-apollo-grader-phase1b-plan.md`
- `ai-ta-backend/docs/_archive/plans/2026-06-23-apollo-grader-phase1c-plan.md`
- Scout: `ai-ta-backend/docs/_archive/plans/2026-06-23-apollo-grader-scout.md`
- Spec: `docs/superpowers/specs/2026-06-23-apollo-shadow-grader-node-recovery-design.md`

**Resolved decisions (already adjudicated — design to these, do NOT relitigate):** D1 (1c reorder: rewrite findings → compute nc over POST-rewrite findings → abstain → construct; persisted nc value byte-identical), D2 (floor = 0.85 starting value; derived@0.95 & alias@0.92 sit ABOVE it — only fuzzy@0.80/llm@0.75 abstain; do NOT raise to "catch derived"), D3 (include the 0.3 self-loop guard now, count into existing `dropped_edge_count`), D4 (econ verification = the no-infra `scripts/econ_grading_delta.py` harness; the full local-stack `run_macro_probe.py` is human-gated and NOT runnable by agents).

---

## Global execution order (waves)

Phase 0 MUST precede Phase 1 (it removes the false-positive amplifiers that easier resolution would scale — spec §5). The econ BEFORE snapshot must be captured on clean base code, so the harness lands first as a standalone step.

```
Wave 0  (econ BEFORE)        →  Step 1   (harness + BEFORE capture, no source-edit)
Wave 1  (Phase 0 prereq)     →  Steps 2-4  (0.1 → 0.2 → 0.3)         [COMMIT 1]
Wave 2  (Phase 1a resolve)   →  Steps 5-8  (module → method → dispatch → tests)  [COMMIT 2]
Wave 3  (Phase 1b aliases)   →  Steps 9-11 (field → populate → tier+tests)       [COMMIT 3]
Wave 4  (Phase 1c brake)     →  Steps 12-15 (threshold → gate → helper → reorder) [COMMIT 4]
Wave 5  (econ AFTER + docs)  →  Steps 16-17 (AFTER capture + invariant flip; docs already in each commit) → PR
```

The econ harness file (`scripts/econ_grading_delta.py`) is CREATED in Step 1 (Wave 0) but its source code is committed with **Phase 1a (COMMIT 2)** — it is Phase 1a's owned artifact (D4). The BEFORE JSON is captured by running it against base before COMMIT 1. The AFTER flip happens in Step 16, inside COMMIT 2's authoring window (the harness invariant test ships green-on-AFTER in COMMIT 2). See "Commit boundaries" for the exact mapping.

---

## Full Plan (Sequenced)

Legend: **[P]** = keeps the 3 purity tests green (touches `canonical.py` or grade-math-adjacent). RED test is written first for every task (TDD contract). Verify = pytest target. Patch gate = `diff-cover coverage.xml --compare-branch=origin/staging --fail-under=95` over changed lines.

The 3 purity tests (must stay green throughout):
- `apollo/graph_compare/tests/test_core.py:245::test_grade_attempt_is_pure`
- `apollo/graph_compare/tests/test_scores.py:235::test_sub_scores_pure_same_input_same_output`
- `apollo/graph_compare/tests/test_student_canonical.py:195::test_builder_is_pure_same_input_same_output`

### Wave 0 — Econ BEFORE snapshot (run on clean base, no source edit)

- [ ] **[1a] Step 1 — Econ before/after harness + capture BEFORE (D4).**
  - File (NEW): `scripts/econ_grading_delta.py` + `scripts/tests/test_econ_grading_delta.py`.
  - Insertion point: new files only; expose `run_delta() -> dict`, thin `if __name__=="__main__"` guard (`# pragma: no cover`).
  - What it builds: `build_problem_candidates(load(problem_01.json), {"misconceptions": []}, canon_key_by_canonical_key={})` (confirmed `symbolic_mappings == {"PI": "deflator"}`) → hand-authored strong student `KGGraph` (`stu_eq_base` control, `stu_eq_rearranged` `realGDP - nomGDP/(PI/100)`, `stu_proc`, `USES` edge proc→rearranged) → `resolve_attempt` (proc-only stub adjudicator, `llm_adjudicator` pure) → `build_student_canonical` → `grade_attempt` against `build_reference_canonical(load(problem_01.json))` (no infra). Print `json.dumps(..., indent=2, sort_keys=True)`: `per_node`, `unresolved_rate`, `dropped_edge_count`, `sub_scores{coverage,node_coverage,edge_coverage,scoping,usage}`. Mirror helpers from `test_derived_equation_resolution.py:61-93`.
  - RED test: `scripts/tests/test_econ_grading_delta.py::` asserts structural keys + the **BEFORE invariant** `out["per_node"][<rearranged>]["resolution"] == "unresolved"` (green on base). (Flipped to AFTER in Step 16.)
  - Capture: run the harness against CURRENT base, save stdout into a `# BEFORE (captured 2026-06-23, base <sha>)` comment block at the bottom of the script. Expected BEFORE: rearranged eq `unresolved`, `dropped_edge_count >= 1`, `edge_coverage ≈ 0`/`usage ≈ 0`.
  - Verify: `.venv/Scripts/python.exe scripts/econ_grading_delta.py` prints valid JSON; `.venv/Scripts/python.exe -m pytest scripts/tests/test_econ_grading_delta.py -q` green on base.
  - NOTE: this step's files are authored now but COMMITTED with Phase 1a (COMMIT 2). Capturing BEFORE first guarantees a clean delta.

### Wave 1 — Phase 0 resolver safety (prerequisite — must land before Phase 1) → COMMIT 1

- [ ] **[P] Step 2 — 0.1 Type-gate the LLM adjudication result.**
  - File: `apollo/resolution/resolver.py:192-193` (the `elif n.node_id in llm_resolved:` branch in the per-node build loop). `type_compatible` already imported (`:44`).
  - RED test: `apollo/resolution/tests/test_resolver.py::test_llm_adjudication_cross_type_key_stays_unresolved` — recorded (non-None) adjudicator returns a real-but-cross-type in-set key → node stays `resolution=="unresolved"`, `method=="unresolved"`, `resolved_key is None`. Control `::test_llm_adjudication_same_type_key_still_resolves` → type-compatible in-set key still `resolved`/`method=="llm"`/conf 0.75.
  - Change: `elif n.node_id in llm_resolved and type_compatible(n.node_type, llm_resolved[n.node_id]):` ... `else: _unresolved(n)`. No signature change; byte-identical when `adjudicator is None` (CI default).
  - Verify: `.venv/Scripts/python.exe -m pytest apollo/resolution/tests/test_resolver.py -q`.

- [ ] **[P] Step 3 — 0.2 Explicit canonical merge type.**
  - File: `apollo/graph_compare/canonical.py:249` (`node_type = member_nodes[0].node_type`), inside `for key in sorted(members_by_key):`. Add module logger `_LOG = logging.getLogger(__name__)`.
  - RED test: `apollo/graph_compare/tests/test_canonical_types.py::test_merge_mixed_member_types_picks_deterministic_explicit_type` — mixed-type members same `resolved_key` → `CanonicalNode.node_type == min(member types)` lexicographically (L7: `NodeType` is a `Literal[str]`, no IntEnum; `"condition" < "equation"`). `::test_merge_logs_warning_on_type_disagreement` (caplog → WARNING naming key + types). Control `::test_merge_uniform_member_types_unchanged` (uniform → that type, NO warning — protects purity).
  - Change: `member_types = {n.node_type for n in member_nodes}`; if `len==1` keep `member_nodes[0].node_type`; else deterministic `min` by `(type, lowest member-id)` + `_LOG.warning`. Surface source stays `member_nodes[0]`.
  - FLAGGED: adds a log side-effect to a pure builder. Kept (warning fires only on never-fire branch; purity tests assert return-value equality, unaffected). Fallback documented = drop warning if reviewer objects.
  - Verify: `.venv/Scripts/python.exe -m pytest apollo/graph_compare/tests/test_canonical_types.py apollo/graph_compare/tests/test_student_canonical.py -q`.

- [ ] **[P] Step 4 — 0.3 PRECEDES self-loop guard (D3).**
  - File: `apollo/graph_compare/canonical.py:272-285` edge-normalization loop. Insert AFTER the `from_key is None or to_key is None` check, BEFORE `edges.append(...)`: `if from_key == to_key: dropped += 1; continue`. Counts into existing `dropped_edge_count` (no new field).
  - RED test: `apollo/graph_compare/tests/test_student_canonical.py::test_post_merge_self_loop_edge_is_dropped_and_counted` — two nodes resolve to SAME key + `PRECEDES` edge between them → edge absent from `result.edges`, `dropped_edge_count == prior+1`, no `CanonicalEdge` has `from_key==to_key`. Control `::test_distinct_endpoint_edge_still_kept` (may already exist — check before duplicating; if so, add a one-line assertion).
  - Verify: `.venv/Scripts/python.exe -m pytest apollo/graph_compare/tests/test_student_canonical.py -q`.

- [ ] **Step 4-docs — Reconcile `apollo.md` (SAME commit as COMMIT 1).** Document the LLM type-gate, the explicit merge-type rule + new `canonical.py` logger, the post-merge self-loop drop. Bump `last_verified` to implementation date. No new `owns:` glob (both files under `apollo/**`).

> **COMMIT 1 boundary** — Phase 0. Files: `resolver.py`, `canonical.py`, `test_resolver.py`, `test_canonical_types.py`, `test_student_canonical.py`, `apollo.md`. Independently passes full suite + 95% patch gate. The 3 purity tests stay green (canonical.py edits are return-value-preserving on uniform-type/non-self-loop inputs).

### Wave 2 — Phase 1a deterministic equation-alignment (`derived`@0.95) → COMMIT 2

Order within wave: **5 (module) → 6 (method+cap) → 7 (dispatch) → 8 (tests)**. T5-BEFORE (Step 1) already captured.

- [ ] **Step 5 — T1: NEW pure module `apollo/resolution/equation_alignment.py`.**
  - Signature: `match_equation_alignment(node, candidates, *, mappings: dict[str,str]) -> TierHit | None`, mirrors `match_symbolic`. Returns `(cand, "derived", 0.95)` on align.
  - REUSE (do NOT reimplement): `from apollo.resolution.tiers import _extended_locals, _zero_form, student_surface_text` (+ `TierHit`). Wrap SymPy (`solve`, `simplify`, `nsimplify`, `free_symbols` — all `Any`) in typed helpers for mypy (L9, BLOCKING).
  - Algorithm: equation nodes only; per candidate build zero-forms, apply DECLARED `mappings` subs to BOTH; `nsimplify(..., rational=True)`; for each `x in sorted(r.free_symbols, key=str)`: `solve(Eq(r,0), x)`, for each `branch in sorted(branches, key=str)` test `simplify((x-branch) - s) == 0`. First match → align. Every SymPy call `try/except → non-match` (mirror `_symbolic_equiv:177-180`). Hard exclusions: no numeric-given substitution; only declared mappings; Rational-ize floats.
  - RED test (FIRST): `apollo/resolution/tests/test_equation_alignment.py::test_deflator_rearrangement_aligns_via_derived` — `realGDP - nomGDP/(PI/100)` ↔ `deflator - (nomGDP/realGDP)*100` under `{PI:deflator}` → `hit[1]=="derived"`, `hit[2]==0.95`. FAILS on ImportError → RED.
  - Verify: `.venv/Scripts/python.exe -m pytest apollo/resolution/tests/test_equation_alignment.py -q`; `ruff check`, `ruff format --check`, `mypy` on the new file all clean (BLOCKING).

- [ ] **Step 6 — T2: `derived` method + cap `0.95` (+ FIX exact-snapshot test L1, BLOCKING).**
  - File: `apollo/resolution/candidates.py`. `RESOLUTION_METHODS` (`:24-31`): insert `"derived"` BETWEEN `"symbolic"` and `"alias"`. `METHOD_CONFIDENCE_CAP` (`:35-42`): add `"derived": 0.95` between `symbolic` and `alias`. `_resolved` re-derives from the cap dynamically → no change there.
  - **L1 (BLOCKING):** `apollo/resolution/tests/test_candidates.py:62-71::test_method_confidence_caps_match_spec` exact-snapshots BOTH the dict literal AND `set(RESOLUTION_METHODS)==set(METHOD_CONFIDENCE_CAP)`. RED-first: update the test snapshot to include `"derived": 0.95` BEFORE the source edit (→ RED), then edit source (→ GREEN).
  - Verify: `.venv/Scripts/python.exe -m pytest apollo/resolution/tests/test_candidates.py -q`.

- [ ] **Step 7 — T3: tier dispatch in `resolver._content_match`.**
  - File: `apollo/resolution/resolver.py`. Import `match_equation_alignment` (separate import — no circular dep). Insert immediately AFTER the `match_symbolic` block (after `:89`), BEFORE the lexical tiers (`:91`): mirror the symbolic block, call with `type_ok` candidates + `mappings=symbolic_mappings`, on hit `return ScoredMatch(node.node_id, cand, "derived", METHOD_CONFIDENCE_CAP["derived"])`.
  - **Conflict note (resolver.py):** Step 2 (0.1) edits `:192-193`; this step edits `:89`. Disjoint lines, Phase 0 lands first (COMMIT 1) — apply cleanly. After-symbolic placement ⇒ substitution-collapsible forms still report `symbolic@0.98`; only solve-for-variable forms report `derived@0.95`.
  - RED test: covered by Step 8's live-seam assertion (RED until dispatch lands).
  - Verify: `.venv/Scripts/python.exe -m pytest apollo/resolution apollo/graph_compare -q`.

- [ ] **Step 8 — T4: tests (pure unit + live-seam) — spec §14.**
  - `test_equation_alignment.py` POSITIVES (method `derived`, cap 0.95): deflator rearrangement; Bernoulli solved (`v2 = sqrt(2*g*h1)`); a genuine solve-for-variable form (NOT substitution-collapsible — else symbolic@0.98 wins first; assert accordingly). NEGATIVE CONTROLS (stay unresolved): sign-flipped rearrangement; unrelated equation; **numeric-coincidence** `growth-(10739.0/2859.5)*100` (Q5 `real_gdp_growth` — documented recall gap / negative control); undeclared-simplification form. Determinism: `assert sympy.__version__ == "1.14.0"` + run each positive twice → identical.
  - Live-seam: extend `apollo/graph_compare/tests/test_derived_equation_resolution.py` with Q4 `problem_01.json` block — `stu_eq_rearranged` resolves to `eq.gdp_deflator` via content tiers (`llm_calls==0`), `USES` edge survives with both endpoints resolved, `dropped_edge_count==0`.
  - Verify: `.venv/Scripts/python.exe -m pytest apollo/resolution/tests/test_equation_alignment.py apollo/graph_compare/tests/test_derived_equation_resolution.py -q`.

- [ ] **Step 8-docs — Reconcile `apollo.md` (SAME commit as COMMIT 2).** Add the `derived` tier (between symbolic and alias) + `METHOD_CONFIDENCE_CAP["derived"]=0.95`; mention `equation_alignment.py` (covered by `apollo/**` — no new glob). Bump `last_verified`.

> **COMMIT 2 boundary** — Phase 1a. Files: NEW `equation_alignment.py`, `scripts/econ_grading_delta.py`, `scripts/tests/test_econ_grading_delta.py`, `test_equation_alignment.py`; EDIT `candidates.py` (methods/caps only), `resolver.py` (dispatch only), `test_candidates.py` (L1 snapshot), `test_derived_equation_resolution.py`, `apollo.md`. The harness AFTER invariant (Step 16) ships in this commit green-on-AFTER. Independently passes full suite + 95% patch gate. Grade-math byte-identical.

### Wave 3 — Phase 1b exact-only alias channel → COMMIT 3

Order within wave: **9 (field) → 10 (populate) → 11 (tier + tests)**.

- [ ] **Step 9 — T1: `Candidate.exact_aliases` field.**
  - File: `apollo/resolution/candidates.py` — append `exact_aliases: tuple[str, ...] = ()` as the LAST field of the frozen `Candidate` (after `opposes_key`, `:72`). L6: must be terminal + defaulted (first defaulted field; all constructors use kwargs → safe). One-sentence docstring update.
  - RED test: `apollo/resolution/tests/test_candidates.py::test_candidate_exact_aliases_defaults_empty_and_is_frozen` — default `()`, explicit set, `FrozenInstanceError` on assign.
  - **Conflict note (candidates.py):** Phase 1a (Step 6) edited the caps table / `RESOLUTION_METHODS` (`:24-42`); this step edits the dataclass body (`:56-73`). Disjoint regions, Phase 1a lands first (COMMIT 2). If 1a shifted lines, re-locate by the `aliases=()` kwarg, not the line number.
  - Verify: `.venv/Scripts/python.exe -m pytest apollo/resolution/tests/test_candidates.py -q`.

- [ ] **Step 10 — T2: populate `exact_aliases` in `candidates_from_reference_solution`.**
  - File: `apollo/resolution/candidates.py:95-106` — add `exact_aliases=tuple(content.get("aliases", ()))` to the `Candidate(...)` call; keep `aliases=()`. `candidates_from_misconceptions` UNCHANGED (keeps fuzzy recall via `trigger_phrases`).
  - RED test: `::test_reference_aliases_flow_into_exact_aliases_not_aliases` (in-memory problem dict with `content.aliases` → `exact_aliases` populated, `aliases==()`) + no-aliases regression (`exact_aliases==()`).
  - Verify: `.venv/Scripts/python.exe -m pytest apollo/resolution/tests/test_candidates.py -q`.

- [ ] **Step 11 — T3/T5: exact-alias tier reads BOTH; fuzzy tier reads ONLY `aliases` (+ end-to-end + negative corpus).**
  - File: `apollo/resolution/tiers.py:224` `match_alias_all` — inner loop reads `(*cand.exact_aliases, *cand.aliases)` (exact_aliases first). `match_fuzzy_all:293` STAYS `cand.aliases`-only (invariant comment, fold into docstring if diff-cover flags a bare line). Extend `_cand` helpers in `test_tiers.py`/`test_resolver.py` with an `exact_aliases=()` passthrough.
  - RED tests: `test_tiers.py::test_exact_alias_tier_resolves_curated_phrasing_via_exact_aliases`; **CRITICAL GUARD** `::test_fuzzy_tier_ignores_exact_aliases_near_miss_does_not_resolve` (hand-wave paraphrase of a curated alias stays missing in both tiers); `::test_exact_alias_tier_still_reads_misconception_aliases`; `::test_negative_corpus_curated_resolves_handwave_missing` (parametrized §14, 5 cases — `_surface_node` helper sets definition `meaning=""` so surface == concept exactly, MEDIUM risk flagged). `test_resolver.py::test_exact_alias_resolves_end_to_end_via_resolve_attempt` (method `alias`, conf 0.92, `resolved_key`==candidate key) + `::test_empty_exact_aliases_resolution_unchanged` (worked-example byte-identical regression).
  - **Conflict note (tiers.py):** only Phase 1b edits this file — no cross-phase conflict.
  - Verify: `.venv/Scripts/python.exe -m pytest apollo/resolution -q`.

- [ ] **Step 11-docs — Reconcile `apollo.md` (SAME commit as COMMIT 3).** Document `Candidate.exact_aliases` + the rule (exact-alias tier reads `exact_aliases`+`aliases`; fuzzy reads ONLY `aliases`); populated from reference-solution `content.aliases` (hand-authored v1; harvest→approval queue DEFERRED, AliasCandidate stays capped 0.75). Bump `last_verified`. No new glob.

> **COMMIT 3 boundary** — Phase 1b. Files: `candidates.py` (dataclass + populate), `tiers.py` (`match_alias_all` loop), `test_candidates.py`, `test_tiers.py`, `test_resolver.py`, `apollo.md`. Does NOT touch shipped `subjects/**/problem_*.json` (alias data lives in test fixtures). Independently passes full suite + 95% patch gate. Grade-math byte-identical (`exact_aliases=()` default ⇒ no-curated-alias attempts byte-identical).

### Wave 4 — Phase 1c abstention-quality brake (normalization-confidence floor) → COMMIT 4

Order within wave: **12 (threshold+reason) → 13 (gate) → 14 (helper+cycle-break) → 15 (D1 reorder)**. (12+13 may co-commit; 14+15 may co-commit — but all four ship as ONE phase commit.)

- [ ] **Step 12 — T1: threshold + reason constant.**
  - File: `apollo/grading/abstention.py` — `ABSTENTION_THRESHOLDS["min_normalization_confidence"] = 0.85` (`:31-35`, NOT exact-snapshotted → safe); `REASON_LOW_NORMALIZATION_CONFIDENCE = "normalization_confidence_below_threshold"` (after `:47`).
  - RED test: `test_abstention.py` asserts constant importable + threshold == 0.85.
  - **D2/L8 TRAP:** 0.85 is the starting value; derived@0.95 & alias@0.92 sit ABOVE it; only fuzzy@0.80/llm@0.75 abstain. Do NOT raise it. Calibration-owned (§10).

- [ ] **Step 13 — T2: the gate in `apply_abstention`.**
  - File: `apollo/grading/abstention.py:83-91` — append keyword-only `normalization_confidence: float = 1.0` (default never trips; L3 backward-compatible at all 3 call sites). Gate body BETWEEN the `unresolved_rate` block (`:114-116`) and `min_parser_confidence` (`:118`): `if normalization_confidence < ABSTENTION_THRESHOLDS["min_normalization_confidence"]: abstained = True; reasons.append(REASON_LOW_NORMALIZATION_CONFIDENCE)`. Use plain `abstained = True` (not `or`). Update module docstring binding note.
  - RED tests: `::test_low_normalization_confidence_abstains` (nc=0.80 → abstained + reason at low unresolved_rate); `::test_normalization_confidence_at_threshold_does_not_abstain` (0.85 not < 0.85); `::test_default_normalization_confidence_does_not_abstain` (default 1.0).
  - **L4 (MUST update):** `test_abstention.py:122-140::test_reasons_deterministic_order` snapshots the exact 5-tuple. Insert `REASON_LOW_NORMALIZATION_CONFIDENCE` in SECOND position (right after `REASON_HIGH_UNRESOLVED`), add `normalization_confidence=0.0` to its kwargs, import the new reason.
  - Verify: `.venv/Scripts/python.exe -m pytest apollo/grading/tests/test_abstention.py -q`.

- [ ] **Step 14 — T3: pure-helper extraction + cycle break.**
  - File: `apollo/grading/normalization_confidence.py`. Move `from apollo.grading.audited_grade import AuditedGrade` (`:20`) under `if TYPE_CHECKING:` (safe — `from __future__ import annotations` at `:18`, only use is a parameter annotation). Extract `_normalization_confidence_over(findings, resolution) -> float` (the existing MIN-over-scored-finding-backers body, byte-for-byte); `compute_normalization_confidence` becomes a thin delegator → identical value. Add `from apollo.graph_compare.findings import Finding`.
  - RED test: `test_normalization_confidence.py::test_helper_matches_public_function` (direct helper == public fn == 0.75). All existing nc tests stay green (value unchanged).
  - Verify: `.venv/Scripts/python.exe -m pytest apollo/grading/tests/test_normalization_confidence.py -q` + cycle check `.venv/Scripts/python.exe -c "import apollo.grading.audited_grade, apollo.grading.normalization_confidence"`.

- [ ] **Step 15 — T4: D1 reorder wiring in `build_audited_grade`.**
  - File: `apollo/grading/audited_grade.py:196-214`. Import `_normalization_confidence_over` (runtime, one-directional — safe after Step 14's TYPE_CHECKING guard). Reorder tail to: `_rewrite_findings(grade.findings, audit) → new_findings` FIRST → `nc = _normalization_confidence_over(new_findings, resolution)` → `apply_abstention(..., normalization_confidence=nc)` → construct `AuditedGrade(findings=new_findings, ...)`. Keep `_misconception_confidences(grade.findings, ...)` as-is (CONTRADICTION findings identical pre/post-rewrite — minimizes diff, keeps misconception-gate tests untouched). **SIGNATURE UNCHANGED** → `test_done_grading_unit.py:329` stays green (L5).
  - **D1 (POST-rewrite is load-bearing):** the audit upgrade turns `missing_node` into `COVERED_NODE@0.75` (`:105-111`), a scored kind nc counts. Persisted nc (`done_grading.py:265` → `grader_confidence`, `persistence.py:293`) is ALREADY post-rewrite ⇒ internal nc over `new_findings` == external value. Byte-identical.
  - RED tests: `test_audited_grade.py::test_reorder_preserves_persisted_nc_value` (external recompute == internal, neutral floor, no abstain); `::test_weak_tier_backing_triggers_1c_abstain_end_to_end` (fuzzy@0.80 backer → nc 0.80 < 0.85 → abstained + reason at unresolved_rate 0). **RECOMMENDED extra (closes HIGH risk):** a `found_audit_fn` test upgrading `missing_node → COVERED_NODE@0.75` with a 0.75 backer → gate fires ONLY if nc saw POST-rewrite findings (proves the pre/post distinction; the `notfound_audit_fn` fixtures have pre==post and do not prove it).
  - Verify: `.venv/Scripts/python.exe -m pytest apollo/grading/tests/test_audited_grade.py apollo/handlers/tests/test_done_grading_unit.py -q`.

- [ ] **Step 15-docs — Reconcile `apollo.md` (SAME commit as COMMIT 4).** Document the new quality gate (`min_normalization_confidence=0.85` + reason + SECOND `abstained` trigger), the D1 reorder (nc over POST-rewrite findings, persisted value unchanged), the helper extraction + TYPE_CHECKING cycle break. Bump `last_verified`. No new glob.

> **COMMIT 4 boundary** — Phase 1c. Files: `abstention.py`, `normalization_confidence.py`, `audited_grade.py`, `test_abstention.py`, `test_audited_grade.py`, `test_normalization_confidence.py`, `apollo.md`. Independently passes full suite + 95% patch gate. Grade-math byte-identical; persisted nc value byte-identical; `done_grading` mock test green (L5). ONE documented coverage exemption pre-authorized: the `if TYPE_CHECKING:` guarded import may need `# pragma: no cover`.

### Wave 5 — Econ AFTER capture (folded into COMMIT 2 authoring) + PR

- [ ] **Step 16 — T5-AFTER: re-run harness, flip invariant.** After Step 7 (dispatch) lands, re-run `.venv/Scripts/python.exe scripts/econ_grading_delta.py`. Expected AFTER: `stu_eq_rearranged → {resolution:"resolved", method:"derived"}` (key `eq.gdp_deflator`), `dropped_edge_count==0`, `edge_coverage`/`usage` strictly up vs BEFORE. Flip `scripts/tests/test_econ_grading_delta.py` from BEFORE→AFTER invariant. Record both `# BEFORE …` / `# AFTER …` JSON blocks in the script's trailing comment (§10 calibration record). These changes are part of COMMIT 2.
- [ ] **Step 17 — Final verification + PR.** Run the full VERIFICATION section below; open PR `feat/apollo-grader-node-recovery → staging`. (`apollo.md` is reconciled per-commit, NOT a separate trailing docs commit — drift contract requires same-commit reconciliation. There is no 5th "docs-reconcile" commit; the 4 phase commits each carry their own doc delta.)

---

## Conflict resolutions applied (Decisions I Made)

### Decision 1: Ordering — econ harness FIRST, Phase 0 before Phase 1
- Conflict: Phase 1a's frontmatter declared `depends_on: Phase 0 sequenced before but NOT in this plan's scope`; the spec §4/§5 mandates Phase 0 before Phase 1 (it removes false-positive amplifiers). The harness must capture BEFORE on clean code.
- Chose: Step 1 (harness BEFORE) → Wave 1 Phase 0 → Wave 2 Phase 1a → 1b → 1c → Step 16 AFTER.
- Because: spec §5 prerequisite + D4 (clean-base BEFORE snapshot). The harness FILE is authored in Step 1 but its source ships in COMMIT 2 (it is Phase 1a's owned artifact per D4); only the BEFORE *capture-run* happens on base before COMMIT 1.
- Propagated to: wave assignment + commit mapping.

### Decision 2: Shared file `candidates.py` (1a + 1b) — 1a then 1b
- Conflict: Phase 1a edits `RESOLUTION_METHODS`/`METHOD_CONFIDENCE_CAP` (`:24-42`) + L1 snapshot test; Phase 1b appends `Candidate.exact_aliases` (`:72`) + populates it (`:95-106`). Same file, both additive.
- Chose: 1a (COMMIT 2) lands first, 1b (COMMIT 3) second. Regions are disjoint (caps table vs dataclass body vs constructor) — compose cleanly.
- Because: spec phase order; both planners independently confirmed disjoint regions in their Compose notes. 1b must NOT touch the caps table / add a method; 1a must NOT touch the dataclass field.
- Propagated to: Step 6 vs Steps 9-10; conflict notes in both steps; re-locate-by-kwarg fallback if 1a shifts lines.

### Decision 3: Shared file `resolver.py` (0.1 + 1a) — 0.1 then 1a
- Conflict: Phase 0.1 edits `:192-193` (LLM type-gate); Phase 1a edits `:89` (derived-tier dispatch). Same file.
- Chose: 0.1 (COMMIT 1) first, 1a dispatch (COMMIT 2) second. Lines are far apart (`:89` vs `:192`) — no overlap.
- Because: Phase 0 is the spec prerequisite. Low conflict risk confirmed by both plans.
- Propagated to: Step 2 vs Step 7; conflict note in Step 7.

### Decision 4: File ownership — no overlaps elsewhere
- `tiers.py` edited ONLY by 1b (Step 11). `canonical.py` edited ONLY by 0.2/0.3 (Steps 3-4). `abstention.py` + `audited_grade.py` + `normalization_confidence.py` edited ONLY by 1c (Steps 12-15). `apollo.md` is the one file touched by every commit — reconciled per-commit (drift contract), never deferred to a trailing commit.
- Because: clean single-domain decomposition; the planners pre-partitioned by file.
- Propagated to: commit boundaries; no merge contention.

### Decision 5: No separate trailing docs-reconcile commit
- The task brief mentioned "a final docs-reconcile commit." Overridden: the drift contract (CLAUDE.md) requires `apollo.md` reconciled in the SAME commit as the code that drifted it. Each of the 4 phase commits carries its own `apollo.md` delta + `last_verified` bump. A trailing docs-only commit would violate same-commit reconciliation and leave each intermediate commit drift-inconsistent.
- Propagated to: COMMIT 1-4 boundaries each include `apollo.md`; Step 17 is verification + PR only.

---

## Commit boundaries (atomic, one per phase)

Each commit independently passes the full relevant suite + the 95% patch gate vs `origin/staging`, and carries its own `apollo.md` reconciliation.

| Commit | Phase | Source files | Test files | Doc | Steps |
|---|---|---|---|---|---|
| **1** | Phase 0 | `resolver.py` (`:192`), `canonical.py` (`:249`, `:278`) | `test_resolver.py`, `test_canonical_types.py`, `test_student_canonical.py` | `apollo.md` | 2,3,4 |
| **2** | Phase 1a | NEW `equation_alignment.py`, NEW `scripts/econ_grading_delta.py`, `candidates.py` (caps/methods `:24-42`), `resolver.py` (`:89` dispatch) | NEW `test_equation_alignment.py`, NEW `scripts/tests/test_econ_grading_delta.py`, `test_candidates.py` (L1), `test_derived_equation_resolution.py` | `apollo.md` | 1,5,6,7,8,16 |
| **3** | Phase 1b | `candidates.py` (dataclass `:72` + populate `:95-106`), `tiers.py` (`:224`) | `test_candidates.py`, `test_tiers.py`, `test_resolver.py` | `apollo.md` | 9,10,11 |
| **4** | Phase 1c | `abstention.py`, `normalization_confidence.py`, `audited_grade.py` | `test_abstention.py`, `test_audited_grade.py`, `test_normalization_confidence.py` | `apollo.md` | 12,13,14,15 |

Note: COMMIT 2 carries BOTH the harness BEFORE/AFTER record (Steps 1 + 16) and the Phase 1a tier — the harness is Phase 1a's deliverable (D4). The BEFORE capture-run is performed on base BEFORE COMMIT 1, but the file is committed with COMMIT 2 green-on-AFTER.

---

## GUARDRAILS

**DO-NOT-TOUCH grade-math (keep byte-identical):** `apollo/graph_compare/{core,scores,coverage,bisimilarity,soundness}.py`. None are in any change set.

**DO-NOT-TOUCH live grade path:** `apollo/handlers/done.py:264-293`, `apollo/overseer/coverage.py` (the LLM matcher — name collision with `graph_compare/coverage.py`, UNRELATED), `apollo/overseer/rubric.py`.

**3 purity tests MUST stay green** (marked **[P]** on Phase 0 steps that touch `canonical.py`): `test_grade_attempt_is_pure`, `test_sub_scores_pure_same_input_same_output`, `test_builder_is_pure_same_input_same_output`.

**Flags:** all work runs inside `APOLLO_GRAPH_SIM_SHADOW_ENABLED`. `APOLLO_GRAPH_SIM_LIVE_ENABLED` stays **OFF** — never flipped here, never referenced in code. `APOLLO_GRAPH_SIM_LAYER3_ENABLED` also stays OFF. No student-facing grade change in any wave.

**Do NOT relitigate the resolved decisions:** derived cap = 0.95 (NOT lowered "to be safe"); abstention floor = 0.85 (NOT raised "to catch derived" — D2/L8 TRAP); self-loop guard included now (D3); persisted nc value byte-identical (D1).

**New `.py` files face BLOCKING gates:** `equation_alignment.py`, `econ_grading_delta.py` (+ their test files) — `ruff check`, `ruff format --check`, `mypy`. Wrap SymPy `Any` returns in typed helpers (L9).

**OUT OF SCOPE — deferred follow-ons (do NOT build here):**
- The **§10 promotion acceptance bar** (zero-upward-bias vs human labels on a ≥50-attempt labeled corpus): a calibration MEASUREMENT, human-gated, needs the labeled corpus. This work hardens the grader *toward* that gate; it does not clear it.
- **Phase 2 edge-recovery** (dropped-edge retention + one-anchor recovery + structural-rubric consumer): the next milestone, depends on this node-recovery work. Tracked as the immediate follow-on spec.
- **Harvest→teacher-approval→promote alias queue** (`AliasCandidate` table + teacher-UI; `APOLLO_REFERENCE_ALIAS_HARVEST_ENABLED`): deferred; v1 ships hand-authored aliases only, harvested candidates stay capped 0.75.
- **`unresolved_rate = 0.35` re-justification** (spec §8): calibration against the post-Phase-1 labeled corpus — out of scope; `0.35` stays byte-identical.
- The full local-stack macro probe `scripts/run_macro_probe.py`: human-gated infra, NOT agent-runnable (D4 — replaced by the no-infra `econ_grading_delta.py`).

---

## VERIFICATION

Run from `ai-ta-backend/`. Per-commit: run the phase's targeted suite, then the full regression + patch gate before committing.

**Per-phase targeted suites:**
```bash
# COMMIT 1 (Phase 0)
.venv/Scripts/python.exe -m pytest apollo/resolution/tests/test_resolver.py \
  apollo/graph_compare/tests/test_canonical_types.py \
  apollo/graph_compare/tests/test_student_canonical.py -q

# COMMIT 2 (Phase 1a)
.venv/Scripts/python.exe -m pytest apollo/resolution/tests/test_equation_alignment.py \
  apollo/resolution/tests/test_candidates.py \
  apollo/graph_compare/tests/test_derived_equation_resolution.py \
  scripts/tests/test_econ_grading_delta.py -q
.venv/Scripts/python.exe -m ruff check apollo/resolution/equation_alignment.py scripts/econ_grading_delta.py
.venv/Scripts/python.exe -m ruff format --check apollo/resolution/equation_alignment.py scripts/econ_grading_delta.py
.venv/Scripts/python.exe -m mypy apollo/resolution/equation_alignment.py scripts/econ_grading_delta.py

# COMMIT 3 (Phase 1b)
.venv/Scripts/python.exe -m pytest apollo/resolution -q

# COMMIT 4 (Phase 1c)
.venv/Scripts/python.exe -m pytest apollo/grading/tests/test_abstention.py \
  apollo/grading/tests/test_audited_grade.py \
  apollo/grading/tests/test_normalization_confidence.py \
  apollo/handlers/tests/test_done_grading_unit.py -q
.venv/Scripts/python.exe -c "import apollo.grading.audited_grade, apollo.grading.normalization_confidence"  # cycle check
```

**Full regression (every commit — baseline 429 passed must stay green + new tests):**
```bash
.venv/Scripts/python.exe -m pytest apollo/resolution apollo/grading apollo/graph_compare -q
```

**3 purity tests (every commit):**
```bash
.venv/Scripts/python.exe -m pytest \
  apollo/graph_compare/tests/test_core.py::test_grade_attempt_is_pure \
  apollo/graph_compare/tests/test_scores.py::test_sub_scores_pure_same_input_same_output \
  apollo/graph_compare/tests/test_student_canonical.py::test_builder_is_pure_same_input_same_output -q
```

**Patch coverage (every commit, BLOCKING ≥95%):**
```bash
.venv/Scripts/python.exe -m pytest apollo --cov=apollo --cov-report=xml -q
diff-cover coverage.xml --compare-branch=origin/staging --fail-under=95
```

**Change-set scope assertion (Phase 0 example — adapt per commit):**
```bash
git diff --name-only origin/staging...HEAD   # expect ONLY the per-commit files listed in the table; NO grade-math, NO live-grade files
```

**Econ before/after diff (D4):**
```bash
# BEFORE — run on base BEFORE COMMIT 1:
.venv/Scripts/python.exe scripts/econ_grading_delta.py > /tmp/econ_before.json
# AFTER — run after Step 7 (dispatch) lands:
.venv/Scripts/python.exe scripts/econ_grading_delta.py > /tmp/econ_after.json
diff /tmp/econ_before.json /tmp/econ_after.json
# Expected delta: stu_eq_rearranged resolution unresolved->resolved, method unresolved->derived,
# dropped_edge_count 1->0, edge_coverage/usage strictly up.
```

---

## Risks (what I'm least sure about)

- **[HIGH] 1c nc pre/post-rewrite distinction (D1).** If the executor computes nc over `grade.findings` (PRE-rewrite) instead of `new_findings`, the gate's nc diverges from the persisted value. The provided `notfound_audit_fn` fixtures have pre==post and DO NOT prove the distinction. Mitigation: the RECOMMENDED `found_audit_fn` discriminator test (Step 15) is effectively mandatory — flag it as required, not optional.
- **[HIGH] L1 caps-snapshot break (1a).** `test_candidates.py:62-71` exact-snapshots both the cap dict and the methods-set; adding `derived` breaks both. Must update in the SAME change as the source edit (RED-first ordering in Step 6). A miss turns the whole suite red.
- **[MEDIUM] Phase 1a positive must be a genuine solve-for-variable form.** If the chosen "derived" positive is substitution-collapsible, the symbolic tier wins at 0.98 first and the test asserts `symbolic`, not `derived`. The deflator rearrangement is safe; the Bernoulli/pressure positives must be picked carefully (Step 8).
- **[MEDIUM] mypy on SymPy `Any` (L9, BLOCKING).** `solve`/`simplify`/`nsimplify`/`free_symbols` return `Any` on the two new files; must wrap in typed helpers or the typecheck job fails the commit.
- **[MEDIUM] Negative-corpus definition surface (1b).** `student_surface_text` concatenates `concept + " " + meaning`; the `_surface_node` helper must set `meaning=""` so the normalized surface equals the curated string exactly — else spurious whitespace mismatch fails the exact-alias assertion.
- **[LOW] 0.2 logger side-effect in a pure builder.** Warning fires only on the never-fire disagreement branch; purity tests assert return-value equality. Fallback = drop the warning if a reviewer objects (loses observability).
- **[LOW] TYPE_CHECKING import coverage (1c).** The guarded import may show uncovered; pre-authorized single `# pragma: no cover` exemption.
- **[LOW] diff-cover on a bare invariant comment (1b `tiers.py:293`).** Fold into the `match_fuzzy_all` docstring if flagged.

## Unresolved — need human input

None. D1-D4 cover every cross-phase ambiguity, and the four plans pre-coordinated their shared-file edits via frontmatter + Compose notes. The only judgment call I overrode is the "final docs-reconcile commit" framing (Decision 5) — reconciling `apollo.md` per-commit is required by the drift contract; if the orchestrator specifically wants a trailing docs commit instead, that would violate same-commit reconciliation and I recommend against it. Flagging it here, not as a blocker.

## TL;DR

**What will ship (all SHADOW-gated, no student grade change):**
- Phase 0 resolver safety: type-gate the LLM adjudication path, explicit deterministic canonical merge type, post-merge PRECEDES self-loop drop.
- Phase 1a: a new deterministic `derived`@0.95 equation-alignment tier (solves/rearranges reference equations) + a no-infra econ before/after harness.
- Phase 1b: an exact-only `exact_aliases` channel for curated condition/simplification/definition phrasings (fuzzy tier deliberately never reads it).
- Phase 1c: a normalization-confidence abstention brake (floor 0.85) so weak-tier-only attempts still abstain even after 1a/1b lower `unresolved_rate`.
- Grade-math, live grade, and the persisted nc value stay byte-identical.

**Execution path:** Econ BEFORE capture → COMMIT 1 (Phase 0) → COMMIT 2 (Phase 1a + harness AFTER) → COMMIT 3 (Phase 1b) → COMMIT 4 (Phase 1c) → PR to `staging`. Four atomic commits, each carrying its own `apollo.md` reconciliation and independently passing the 95% patch gate.

**Risks worth watching:** (1) 1c nc must be computed over POST-rewrite findings — add the `found_audit_fn` discriminator test; (2) L1 caps-snapshot must be updated in the same change as the `derived` method; (3) mypy on SymPy `Any` in the two new files.

**User action needed before start:** NO. All decisions (D1-D4) are resolved; flags stay OFF; no migrations; no remote DB. The §10 promotion gate and Phase 2 edge-recovery are explicitly deferred.
