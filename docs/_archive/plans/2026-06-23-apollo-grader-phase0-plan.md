# Plan: Apollo Graph-Grader Phase 0 — Resolver Safety Fixes (TDD)

**Goal:** Land the three Phase-0 resolver-safety fixes (type-gate the LLM adjudication result; explicit canonical merge type; PRECEDES self-loop guard) so easier resolution in Phase 1 cannot scale false positives.
**Architecture:** `apollo/resolution/` (resolver type-gate) + `apollo/graph_compare/canonical.py` (merge type + self-loop guard). Pure, deterministic, upstream of grade-math. Shadow-gated; no student-facing grade change.
**Tech stack:** Python 3.12, pytest 9.0.3, sympy 1.14.0 (not exercised by Phase 0), ruff + mypy (BLOCKING on changed/new files), diff-cover ≥95% patch vs `origin/staging`.

---
provides:
  - Type-safe LLM adjudication path (resolver.py) — Phase 1 depends on this guardrail existing before lowering unresolved_rate
  - Explicit, observable canonical merge type (canonical.py) — removes the silent mis-label amplifier
  - Self-loop edge drop into dropped_edge_count (canonical.py) — diagnostic edge sub-score correctness
consumes:
  - apollo/resolution/structural.py::type_compatible (already imported in resolver.py:44)
  - apollo/ontology/nodes.py::NodeType (already imported in canonical.py:37)
depends_on:
  - none — Phase 0 is the prerequisite; Phase 1a/1b/1c plans depend_on THIS plan
---

## Overview

Three localized, additive, behavior-preserving-when-`adjudicator=None` fixes. Phase 0 is the §5 prerequisite: it removes the false-positive amplifiers (un-type-gated LLM path, silent first-member merge type, post-merge self-loop) BEFORE Phase 1 makes resolution easier. No grade-math file is touched. The three purity tests must stay green after the canonical.py edits.

Shadow envelope: all changes execute under `APOLLO_GRAPH_SIM_SHADOW_ENABLED`; `APOLLO_GRAPH_SIM_LIVE_ENABLED` stays OFF and is never referenced.

## Prior art (sibling modules)
- Type gate already enforced on content tiers: `apollo/resolution/resolver.py:76` (`type_compatible(node.node_type, c)`), imported `:44`. Reuse verbatim — no new helper.
- `_unresolved(n)` fall-through emitter: `apollo/resolution/resolver.py:194,216-225`. The 0.1 fix routes a cross-type LLM hit to this existing branch.
- Edge-drop-and-count pattern: `apollo/graph_compare/canonical.py:271-285` (`dropped += 1; continue`). 0.3 mirrors it one branch deeper.
- Deterministic tie-break by `(-confidence, method)`: `canonical.py:_winning_method:305-319`. 0.2's tiebreak mirrors this deterministic-sort discipline.
- NodeType is a 6-member `Literal[str]` (`apollo/ontology/nodes.py:18-25`) — no IntEnum, no canonical priority; lexicographic `min()` is the only ordering available (L7).

## Structural prep (from neighborhood scan)
- None — neighborhood is clean. `resolver.py` (10 imports, all first-party resolution-layer) and `canonical.py` (single NodeType import added in-tree) are within budget for this change; no circular imports; both are leaf consumers of `structural`/`nodes`. No debt prep required.
- Verify: `.venv/Scripts/python.exe -m pytest apollo/resolution apollo/grading apollo/graph_compare -q` (baseline 429 passed).

## Layered tasks (ORDER MATTERS — 0.1 → 0.2 → 0.3)

(See per-task sections below. Each: exact seam, RED test first, minimal change, verify command, patch-coverage note.)

## Task 0.1 — Type-gate the LLM adjudication result

**Why:** `type_compatible` screens the content tiers (`resolver.py:76`) but is NOT re-applied to the LLM remainder. `adjudicate` (`adjudication.py:88-110`) only rejects keys ABSENT from the candidate set — a real-but-cross-type key (e.g. a Definition node mapped to a `simp.*` key) passes `adjudicate` and is consumed unchecked at `resolver.py:192-193`, producing a cross-type resolution.

**(a) Exact seam / insertion point:** `apollo/resolution/resolver.py:192-193`, the `elif n.node_id in llm_resolved:` branch inside the per-node build loop (`:188-195`). The existing `else: ... _unresolved(n)` at `:194-195` is the fall-through the gated-out node must reach.

**(b) RED test (write first):** `apollo/resolution/tests/test_resolver.py`
- `test_llm_adjudication_cross_type_key_stays_unresolved`:
  - Build a single student node of one type (e.g. a `definition` node) that NO content tier resolves (surface text matching no alias/symbolic).
  - Build candidates including a type-INCOMPATIBLE candidate whose `canonical_key` IS in the candidate set (e.g. a `simplification`-typed `simp.*` candidate) — so `adjudicate` accepts the key (passes its in-set check) but `type_compatible(node.node_type, candidate)` is False.
  - Pass a RECORDED (pure, non-None) adjudicator: `lambda req: {req.nodes[0][0]: "simp.<key>"}` returning the cross-type key.
  - **Assert:** the resolved node for that node_id has `resolution == "unresolved"`, `method == "unresolved"`, `resolved_key is None`. (Closes the live-LLM coverage gap per spec §14 — `adjudicator=None` CI default never exercises this path.)
- Companion GREEN-stays-green control: `test_llm_adjudication_same_type_key_still_resolves` — a recorded adjudicator returning a type-COMPATIBLE in-set key still yields `resolution == "resolved"`, `method == "llm"`, confidence `0.75`. (Proves the gate does not over-reject.)

**(c) Minimal change:** in the `:192-193` branch, gate the LLM hit on type compatibility, falling through to `_unresolved` when incompatible:
```python
elif n.node_id in llm_resolved and type_compatible(n.node_type, llm_resolved[n.node_id]):
    resolved_nodes.append(_resolved(n.node_id, llm_resolved[n.node_id], "llm"))
else:
    resolved_nodes.append(_unresolved(n))
```
`type_compatible` is already imported (`resolver.py:44`). No new import, no signature change. Deterministic; when `adjudicator is None`, `llm_resolved == {}`, so the `elif` is never entered — byte-identical CI behavior (the `None`-path tests stay green).

**(d) Verify:** `.venv/Scripts/python.exe -m pytest apollo/resolution/tests/test_resolver.py -q`
- Patch-coverage note: the changed `elif`/`else` lines are exercised by both the RED test (false branch → `_unresolved`) and the control (true branch → `_resolved`). 100% of changed lines covered; the previously-uncovered live-LLM cross-type path is now covered.

## Task 0.2 — Explicit canonical merge type

**Why:** `build_student_canonical` takes `member_nodes[0].node_type` (`canonical.py:249`) trusting "type-compat guarantees it" (comment `:247-248`). After 0.1 this should be impossible, but the merge must convert a silent mis-label into an explicit, observable, deterministic rule.

**(a) Exact seam / insertion point:** `apollo/graph_compare/canonical.py:249` (the `node_type: NodeType = member_nodes[0].node_type` assignment), inside the `for key in sorted(members_by_key):` loop (`:244-267`). `symbolic` at `:253` reads `member_nodes[0]` for the equation surface — leave the SURFACE source as `member_nodes[0]` (it is the evidence span, not the type) but compute the equation-ness check from the explicitly-merged `node_type`.

**(b) RED test (write first):** `apollo/graph_compare/tests/test_canonical_types.py` (or a new `test_student_canonical.py` case — prefer `test_canonical_types.py`, it already imports the canonical types and has no LLM/DB).
- `test_merge_mixed_member_types_picks_deterministic_explicit_type`:
  - Construct a `ResolutionResult` where two student nodes resolve to the SAME `resolved_key` but carry DIFFERENT `node_type`s (e.g. `member_nodes` of types `condition` and `equation` — fabricated to force the should-never-fire branch; the node_index must return nodes of those types).
  - **Assert:** the resulting `CanonicalNode.node_type` equals the deterministic pick = `min(member types)` lexicographically (here `"condition" < "equation"` → `"condition"`); and (if a disagreement-tiebreak on node-id applies) it is stable across re-runs.
- `test_merge_logs_warning_on_type_disagreement` (use `caplog`):
  - Same mixed-type setup; **assert** a `logging.WARNING` record is emitted naming the `canonical_key` and the disagreeing types.
- Determinism control: `test_merge_uniform_member_types_unchanged` — all members same type → `node_type` equals that type, NO warning emitted (proves the common path is byte-identical; protects `test_builder_is_pure_same_input_same_output`).

**(c) Minimal change (two parts):**
1. Add a module logger at the top of `canonical.py` (none exists today — FLAGGED below). Use the std pattern: `import logging` + `_LOG = logging.getLogger(__name__)`.
2. Replace `node_type = member_nodes[0].node_type` (`:249`) with an explicit merge over `member_nodes`:
```python
member_types = {n.node_type for n in member_nodes}
if len(member_types) == 1:
    node_type: NodeType = member_nodes[0].node_type
else:
    # Should never fire after the resolver type-gate (0.1); make the
    # mis-label explicit + observable instead of silently taking [0].
    # Deterministic: lowest node_type (lexicographic — NodeType is a
    # str Literal with no canonical priority), then by lowest member id.
    node_type = min(
        (n.node_type for n in member_nodes),
        key=lambda t: (t, min(nid for nid in member_ids
                              if node_index[nid].node_type == t)),
    )
    _LOG.warning(
        "canonical_merge_type_disagreement key=%s types=%s chosen=%s",
        key, sorted(member_types), node_type,
    )
```
Then keep `:253` `symbolic = ... if node_type == "equation" else None` reading the now-explicit `node_type` (it already does — only the source of `node_type` changed). No signature change; `CanonicalNode` construction (`:257-267`) is unchanged.

**FLAGGED design concern (state explicitly):** `canonical.py` is a deliberately-pure builder with NO logger today. Adding `_LOG.warning` introduces an IO side-effect (log emission) into a pure function. Chosen approach: keep it — a `logging.warning` is a standard observable for a should-never-fire invariant violation, does NOT mutate inputs or return value, and the purity tests (`test_builder_is_pure_same_input_same_output`) assert RETURN-VALUE equality on same input, which logging does not break. The alternative (return the disagreement as data on `CanonicalGraph`) would change the frozen DTO shape and ripple into persistence — rejected as over-scope for a never-fire net. The warning fires only on the disagreement branch, which uniform-type inputs never hit, so the pure-path tests see no log.

**(d) Verify:** `.venv/Scripts/python.exe -m pytest apollo/graph_compare/tests/test_canonical_types.py apollo/graph_compare/tests/test_student_canonical.py -q`
- Patch-coverage note: the `len(member_types) == 1` true branch is covered by every existing uniform-type test + the new control; the `else` (disagreement) branch + the `_LOG.warning` line are covered by the two new mixed-type tests. `min(...)` key lambda is exercised by the deterministic-pick assertion. 100% of changed lines covered.

## Task 0.3 — PRECEDES self-loop guard (D3: include now)

**Why:** Problem 3's over-merge can produce a normalized edge whose endpoints collapse to the same `from_key == to_key` AFTER merge. The parser-layer self-loop guard (`ontology/edges.py:73-75`) runs PRE-merge and cannot catch this. A post-merge self-loop pollutes the diagnostic edge sub-scores. D3 says count it into the existing `dropped_edge_count` (no new field).

**(a) Exact seam / insertion point:** `apollo/graph_compare/canonical.py`, the edge-normalization loop `:272-285`. Insert AFTER the `from_key is None or to_key is None` check (`:275-277`) and BEFORE the `edges.append(CanonicalEdge(...))` (`:278-285`):
```python
        if from_key is None or to_key is None:
            dropped += 1
            continue
        if from_key == to_key:          # post-merge self-loop (D3)
            dropped += 1
            continue
        edges.append(CanonicalEdge(...))
```

**(b) RED test (write first):** `apollo/graph_compare/tests/test_student_canonical.py`
- `test_post_merge_self_loop_edge_is_dropped_and_counted`:
  - Build a student graph with two nodes that RESOLVE TO THE SAME `resolved_key` (so they merge into one canonical node) and a `PRECEDES` edge BETWEEN those two student nodes (distinct `from_node_id`/`to_node_id`, but both map to the same key post-merge → `from_key == to_key`).
  - **Assert:** the edge does NOT appear in `result.edges` (no self-loop `CanonicalEdge`), AND `result.dropped_edge_count` includes it (== prior count + 1). Assert no `CanonicalEdge` in `result.edges` has `from_key == to_key`.
- Control: `test_distinct_endpoint_edge_still_kept` — an edge whose endpoints resolve to DIFFERENT keys is retained (proves the guard does not over-drop). Likely already covered by existing tests; add an explicit assertion if not present.

**(c) Minimal change:** the two-line `if from_key == to_key: dropped += 1; continue` insert shown in (a). No new field, no DTO change, no signature change.

**(d) Verify:** `.venv/Scripts/python.exe -m pytest apollo/graph_compare/tests/test_student_canonical.py -q`
- Patch-coverage note: the new `if from_key == to_key` true branch is covered by the self-loop RED test; the false branch by every existing distinct-endpoint edge test + the control. 100% of changed lines covered.

## Purity & no-touch verification

After 0.2/0.3 edit `canonical.py`, the THREE purity tests MUST stay green (no grade-math change):
- `apollo/graph_compare/tests/test_core.py:245` `test_grade_attempt_is_pure`
- `apollo/graph_compare/tests/test_scores.py:235` `test_sub_scores_pure_same_input_same_output`
- `apollo/graph_compare/tests/test_student_canonical.py:195` `test_builder_is_pure_same_input_same_output`

Verify command:
```
.venv/Scripts/python.exe -m pytest \
  apollo/graph_compare/tests/test_core.py::test_grade_attempt_is_pure \
  apollo/graph_compare/tests/test_scores.py::test_sub_scores_pure_same_input_same_output \
  apollo/graph_compare/tests/test_student_canonical.py::test_builder_is_pure_same_input_same_output -q
```

DO-NOT-TOUCH (must stay byte-identical): `apollo/graph_compare/{core,scores,coverage,bisimilarity,soundness}.py`; live grade `apollo/handlers/done.py`, `apollo/overseer/coverage.py`, `apollo/overseer/rubric.py`. None of these are in the change set. Confirm with:
```
git diff --name-only origin/staging...HEAD   # expect only resolver.py, canonical.py, the 3 test files, apollo.md
```

Full-suite regression gate before commit:
```
.venv/Scripts/python.exe -m pytest apollo/resolution apollo/grading apollo/graph_compare -q
```
(Baseline 429 passed — must stay green + new tests added.)

## Drift reconciliation (apollo.md) — SAME COMMIT

Owner doc `ai-ta-backend/docs/architecture/apollo.md` (`owns: apollo/**`). Reconcile in the SAME commit as the code:
- Document the Phase-0 resolver type-gate on the LLM adjudication path (cross-type LLM hits now fall through to unresolved).
- Document the explicit canonical merge-type rule (deterministic lowest-type-then-lowest-id pick + warning on disagreement) and the new `canonical.py` module logger.
- Document the post-merge PRECEDES self-loop drop (counted into `dropped_edge_count`).
- Bump `last_verified` to `2026-06-23` (today). No new `owns:` glob needed — both files are under `apollo/**`.

## Per-task patch-coverage notes (≥95% diff-cover vs origin/staging)

| Task | Changed lines | Covered by |
|------|---------------|-----------|
| 0.1 | `elif` type-gate condition + `else` fall-through (resolver.py:192-195) | RED cross-type test (false branch) + same-type control (true branch) — 100% |
| 0.2 | logger import/init, `member_types` set, `if/else` branch, `min()` key, `_LOG.warning` (canonical.py ~:249) | uniform-type control (true branch) + mixed-type pick test + caplog warning test (else branch) — 100% |
| 0.3 | `if from_key == to_key: dropped += 1; continue` (canonical.py ~:278) | self-loop RED test (true branch) + distinct-endpoint control (false branch) — 100% |

New file note: NONE in Phase 0 (no `equation_alignment.py` — that is Phase 1a). No new-`.py`-file ruff/mypy gate triggers here, but edited files still face ruff + mypy on the diff. `min(...)` key lambda must be mypy-clean: `node_index[nid].node_type` returns `NodeType` (str Literal), tuple `(t, str)` is orderable — typed OK.

Patch-coverage command:
```
.venv/Scripts/python.exe -m pytest apollo --cov=apollo --cov-report=xml -q
diff-cover coverage.xml --compare-branch=origin/staging --fail-under=95
```

## Files touched + tests added

**Source files modified (2):**
- `apollo/resolution/resolver.py` — 0.1 type-gate at the `elif n.node_id in llm_resolved` branch (:192-195).
- `apollo/graph_compare/canonical.py` — 0.2 explicit merge type + module logger (:249); 0.3 self-loop drop (:278).

**Test files modified/added (2 existing, no new files):**
- `apollo/resolution/tests/test_resolver.py` — `test_llm_adjudication_cross_type_key_stays_unresolved` (RED), `test_llm_adjudication_same_type_key_still_resolves` (control).
- `apollo/graph_compare/tests/test_canonical_types.py` — `test_merge_mixed_member_types_picks_deterministic_explicit_type` (RED), `test_merge_logs_warning_on_type_disagreement` (RED, caplog), `test_merge_uniform_member_types_unchanged` (control).
- `apollo/graph_compare/tests/test_student_canonical.py` — `test_post_merge_self_loop_edge_is_dropped_and_counted` (RED), `test_distinct_endpoint_edge_still_kept` (control).

**Doc reconciled (1):** `ai-ta-backend/docs/architecture/apollo.md` (same commit, bump `last_verified`).

## Risks / open questions

- **[LOW] 0.2 logger in a pure builder.** Adding `_LOG.warning` to `canonical.py` introduces a log side-effect into a function the codebase treats as pure. Mitigated: warning fires ONLY on the never-fire disagreement branch; purity tests assert return-value equality (unaffected by logging); uniform-type path emits no log. Resolved approach = keep the logger (documented in 0.2). If a reviewer objects, the fallback is to drop the warning and rely on the deterministic pick alone — but that loses the observability the spec §5 0.2 asks for.
- **[LOW] 0.2 NodeType has no semantic priority (L7).** The lowest-type tiebreak is lexicographic over the 6-member str Literal — `condition < definition < equation < procedure_step < simplification < variable_mapping`. This is a should-never-fire net (0.1 makes disagreement impossible), so the *specific* order carries no grading meaning; it only needs to be DETERMINISTIC. Documented as such in the test + apollo.md.
- **[LOW] 0.1 control test coverage of the `llm` method path.** The same-type control test now exercises the `method="llm"` resolved branch with a recorded adjudicator — previously only reachable via the live path. This is a coverage GAIN, not a risk, but the test must use a recorded (pure) adjudicator so no live OpenAI call fires (CI-safe).
- **[NONE] Open question — self-loop control may already exist.** `test_student_canonical.py` likely already asserts distinct-endpoint edges are kept; if so, 0.3's control is a one-line added assertion rather than a new test. Executor: check before duplicating.
- **Deviations the executor MAY make:** (1) place the 0.2 mixed-type tests in `test_student_canonical.py` instead of `test_canonical_types.py` if the node-index fabrication is easier there; (2) name the new tests differently if matching an existing naming convention in the file; (3) reuse an existing merge/self-loop fixture rather than building fresh graphs. Executor MUST NOT: change any signature, add a required kwarg to `build_audited_grade`, touch grade-math, raise the abstention floor, or add the `derived` method/`exact_aliases` field (those are Phase 1).
