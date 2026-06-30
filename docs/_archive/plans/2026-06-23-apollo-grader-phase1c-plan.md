# Plan: Apollo Graph-Grader Phase 1c — Abstention-Quality Brake (normalization-confidence floor)

**Goal:** Add a resolution-quality abstention gate (`normalization_confidence < 0.85 → abstained=True`) that does NOT key on resolution success, so Phase 1a/1b's lowered `unresolved_rate` cannot let weak-tier attempts grade confidently. Shadow-gated; no student-facing grade change.
**Architecture:** Single backend domain (`apollo/grading/**`). Modifies ONE gate (`abstention.py`) + the wiring order inside `build_audited_grade` (`audited_grade.py`) + a pure-helper extraction in `normalization_confidence.py`. Grade-math untouched.
**Tech stack:** Python 3.12, pytest, sympy 1.14.0 (not used here — 1c is string/dict/float only), ruff + mypy BLOCKING on new/changed `.py`.

---
provides:
  - `ABSTENTION_THRESHOLDS["min_normalization_confidence"] = 0.85` — calibration knob (§10)
  - `REASON_LOW_NORMALIZATION_CONFIDENCE` abstention reason constant
  - `apply_abstention(..., normalization_confidence: float = 1.0)` kwarg
  - `_normalization_confidence_over(findings, resolution)` pure helper (reused by the 1c gate AND the public `compute_normalization_confidence`)
consumes:
  - existing `compute_normalization_confidence` value (done_grading.py:265) — value stays byte-identical
  - existing `_rewrite_findings` / `apply_abstention` inside `build_audited_grade`
depends_on:
  - Phase 0 + Phase 1a + Phase 1b (this spec's earlier phases) land first — 1c is the brake that makes 1a/1b safe. This plan is INDEPENDENTLY mergeable (default `normalization_confidence=1.0` never trips), but per §4 sequencing it ships LAST in the spec.
  - branch `feat/apollo-grader-node-recovery` off `origin/staging` (PR #63 derived-eq work in base)
---

## Overview

Spec §8 (Phase 1c). Today the ONLY hard abstention trigger is `unresolved_rate > 0.35` (`abstention.py:114`). Phases 1a/1b lower `unresolved_rate` by construction, so the messy derived-form-heavy / vague-condition attempts that correctly abstain today would start grading confidently. The fix is a SECOND hard brake that keys on resolution *quality* (the weakest-link `normalization_confidence`), not resolution *success*: an attempt resolving only via weak tiers (`fuzzy@0.80` / `llm@0.75`) is distrusted even when `unresolved_rate` is low.

Four moving parts, all additive and shadow-gated:
1. A new threshold `min_normalization_confidence = 0.85` + reason `REASON_LOW_NORMALIZATION_CONFIDENCE`.
2. `apply_abstention` gains a `normalization_confidence: float = 1.0` kwarg whose default never trips (existing callers/tests unaffected).
3. A pure helper `_normalization_confidence_over(findings, resolution)` extracted from `compute_normalization_confidence`, so nc can be computed BEFORE the frozen `AuditedGrade` exists.
4. **The D1 reorder** inside `build_audited_grade`: `_rewrite_findings` FIRST → compute nc over POST-rewrite findings → `apply_abstention(..., normalization_confidence=nc)` → construct `AuditedGrade`.

The persisted/returned nc value (`done_grading.py:265`, feeds `grader_confidence` in `learner_model/persistence.py:293`) MUST stay byte-identical — 1c only GATES on it, never changes it.

## Scope boundary (what this plan does NOT touch)

- **Grade-math, byte-identical:** `apollo/graph_compare/{core,scores,coverage,bisimilarity,soundness}.py`. Purity tests stay green: `test_grade_attempt_is_pure`, `test_sub_scores_pure_same_input_same_output`, `test_builder_is_pure_same_input_same_output`.
- **Live grade path:** `apollo/handlers/done.py:264-293`, `apollo/overseer/coverage.py` (LLM matcher — name collision with `graph_compare/coverage.py`, UNRELATED), `apollo/overseer/rubric.py`. Untouched.
- **The `unresolved_rate = 0.35` value:** spec §8 asks to re-justify it against the post-Phase-1 corpus. That is a CALIBRATION exercise (needs the labeled corpus, §10), OUT OF SCOPE here. Keep `0.35` byte-identical. Documented in Risks.
- **The 0.85 floor is NOT raised "to catch derived" (D2 / L8):** `derived@0.95` and `alias@0.92` sit ABOVE 0.85; at the starting value only `fuzzy@0.80` / `llm@0.75`-backed SCORED findings abstain. This is intended — the number is calibration-owned (spec §10). Do not relitigate.
- **`APOLLO_GRAPH_SIM_LIVE_ENABLED` stays OFF.** All work runs inside `APOLLO_GRAPH_SIM_SHADOW_ENABLED`. No student-facing grade change.

## Prior art (sibling modules)

- **Gate pattern** — `abstention.py:114-136`: each gate is `if <signal> <op> ABSTENTION_THRESHOLDS[...]: reasons.append(REASON_*)` (+ optional `suppressed.add(...)`). Only the `unresolved_rate` gate sets `abstained=True`. Match this exactly for the new gate.
- **Reason ordering** — `abstention.py:111-136` + pinned by `test_abstention.py:122-140`: reasons append in gate-declaration (source) order, deterministically. New reason gets a decided slot (right after `REASON_HIGH_UNRESOLVED`, since both set `abstained`).
- **Keyword-only signature** — `apply_abstention(*, ...)` (`abstention.py:83-91`): every param is keyword-only with defaults. New `normalization_confidence: float = 1.0` follows this (L3 — backward-compatible at all 3 call sites: `audited_grade.py:196`, tests, corpus fixture).
- **nc weakest-link semantics** — `normalization_confidence.py:38-65` + `test_normalization_confidence.py:42-50`: MIN over per-node method-cap confidence of resolved nodes backing a SCORED finding (`COVERED_NODE`/`CONTRADICTION`); neutral `1.0` when none. The helper extraction must preserve this byte-for-byte.
- **AuditedGrade reorder-safety** — `audited_grade.py:184-214`: the function body is free to reorder; `test_done_grading_unit.py:329` mocks `build_audited_grade` whole and only snapshots its `call_args.kwargs` (signature), so a BODY reorder with no new required kwarg is invisible to it (L5).

## Structural prep (from neighborhood scan)

Neighborhood scan of the three files in the change path:
- `abstention.py` — 143 lines, ~6 imports, 4 module functions. Clean. CBO/WMC well under thresholds.
- `audited_grade.py` — 215 lines, ~10 imports (5 from one package), 5 functions. Clean. The reorder REDUCES nothing/adds nothing structurally — it's a statement-order change + one new local + one new import.
- `normalization_confidence.py` — 66 lines, 3 imports, 1 public function. Clean. The extraction ADDS one private helper, keeps the public function as a thin delegator.

**No circular-import debt today**, but T3 INTRODUCES a latent cycle if done naively: `normalization_confidence.py` imports `AuditedGrade` from `audited_grade.py` (line 20); if `audited_grade.py` then imports the helper from `normalization_confidence.py`, that's a runtime cycle. **T3 breaks it preemptively** by moving the `AuditedGrade` import in `normalization_confidence.py` under `TYPE_CHECKING` (safe: `from __future__ import annotations` is already present at line 18, so the only use — the public function's parameter annotation — is a string at runtime). This is prerequisite work folded INTO T3, not a separate cleanup.

- [ ] Verify no cycle after T3: `apollo/grading` imports cleanly.
- Verify: `cd ai-ta-backend && .venv/Scripts/python.exe -c "import apollo.grading.audited_grade, apollo.grading.normalization_confidence"`

## DI / seam discovery findings

This is pure-Python module wiring, not NestJS DI. The relevant "injection" facts:
- `apply_abstention` is imported into `audited_grade.py:33-37` (from `apollo.grading.abstention`). Adding a kwarg with a default requires NO change at that call site UNLESS we choose to pass nc (T4 does pass it).
- `compute_normalization_confidence` is imported into `done_grading.py` (call at :265) and `test_normalization_confidence.py:13`. Its PUBLIC signature `(audited_grade, resolution)` and return value MUST be preserved — T3 keeps it as a thin delegator.
- The new helper `_normalization_confidence_over` is PRIVATE (leading underscore) and imported only by `audited_grade.py` (T4) + the public function (same module). No external consumer.
- Import direction after T3: `audited_grade.py → normalization_confidence.py` (helper, runtime) and `normalization_confidence.py → audited_grade.py` (AuditedGrade, TYPE_CHECKING-only). No runtime cycle.

## The D1 reorder (central seam) — design

**Current order in `build_audited_grade` (`audited_grade.py:184-214`):**
1. `_missing_entities` (:184)
2. `audit_missing` (:188, in try/except)
3. `apply_abstention(...)` (:196) — over `grade.findings` (PRE-rewrite) via `_misconception_confidences(grade.findings, ...)`
4. `_rewrite_findings(grade.findings, audit)` → `new_findings` (:205)
5. construct `AuditedGrade(findings=new_findings, abstained=abstention.abstained, ...)` (:207)

**Target order (D1):**
1. `_missing_entities` (unchanged)
2. `audit_missing` (unchanged)
3. `_rewrite_findings(grade.findings, audit)` → `new_findings` — **MOVED UP**, before abstention
4. `nc = _normalization_confidence_over(new_findings, resolution)` — **NEW**, over POST-rewrite findings
5. `apply_abstention(..., normalization_confidence=nc)` — gains the nc kwarg; `_misconception_confidences` argument stays `grade.findings` (UNCHANGED — see note)
6. construct `AuditedGrade(findings=new_findings, ...)` (unchanged construct)

**Why POST-rewrite (D1):** the audit upgrade turns a `missing_node` into `COVERED_NODE@0.75` (`audited_grade.py:105-111`), a SCORED kind. nc only counts SCORED-finding backers, so rewriting first changes which findings nc sees. The persisted nc at `done_grading.py:265` is ALREADY computed over post-rewrite findings (it reads `audited.findings`, which `build_audited_grade` already rewrote at :205). So computing nc internally over `new_findings` yields the IDENTICAL value the external call produces — T5 pins this.

**`_misconception_confidences` argument note:** it currently reads `grade.findings` (PRE-rewrite) at :199. The D1 reorder does NOT change that argument — leave it `grade.findings`. Rationale: the misconception gate keys on `CONTRADICTION` findings, which `_rewrite_findings` never touches (it only rewrites `MISSING_NODE`→`COVERED_NODE`). So `grade.findings` and `new_findings` carry identical CONTRADICTION findings; the misconception-gate output is byte-identical either way. Keeping it `grade.findings` minimizes diff and keeps the existing misconception-gate tests untouched. (Documented so the executor does not "tidy" it to `new_findings`.)

## Layered tasks (ORDER MATTERS)

Recommended commit order: **T5(RED) → T1 → T2 → T5(reason-order update) → T3 → T4 → T5(green)**. Below, each task lists its own RED test first per the TDD contract; in practice author the RED assertions, watch them fail, then make the minimal change. T1+T2 are coupled (gate needs the threshold+reason) and may land in one commit; T3+T4 are coupled (reorder needs the helper) and may land in one commit. Keep the whole change on `feat/apollo-grader-node-recovery`.

### T1 — Threshold + reason constant (abstention.py)

**(a) Exact file + insertion point.**
- `apollo/grading/abstention.py:31-35` — add a 4th key to `ABSTENTION_THRESHOLDS`:
  ```python
  "min_normalization_confidence": 0.85,  # nc < 0.85 -> abstained=True (Phase 1c; calibration-owned per spec §10)
  ```
  (`ABSTENTION_THRESHOLDS` is NOT exact-snapshotted by any test — L4 — so extending it is safe.)
- `apollo/grading/abstention.py:47` (right after `REASON_MISCONCEPTION_BANK_EMPTY`) — add the reason constant:
  ```python
  # Phase 1c (spec §8): the attempt resolved only via weak tiers (low
  # normalization_confidence) -> distrust even at low unresolved_rate.
  REASON_LOW_NORMALIZATION_CONFIDENCE = "normalization_confidence_below_threshold"
  ```

**(b) RED test first.** In `apollo/grading/tests/test_abstention.py` (new test, see T5-A):
`assert REASON_LOW_NORMALIZATION_CONFIDENCE` is importable and `ABSTENTION_THRESHOLDS["min_normalization_confidence"] == 0.85`. Fails at import (constant absent) → RED.

**(c) Minimal change.** Two literal additions above; no logic.

**(d) Verify.** `cd ai-ta-backend && .venv/Scripts/python.exe -m pytest apollo/grading/tests/test_abstention.py -q`

**Patch coverage:** the new dict key + constant are covered by the T5-A test importing/asserting them. 100% of T1's added lines covered.

### T2 — The gate (apply_abstention)

**(a) Exact file + insertion point.**
- `apollo/grading/abstention.py:83-91` — add a keyword-only param to `apply_abstention`, appended LAST in the signature (after `misconception_bank_empty: bool = False`):
  ```python
  normalization_confidence: float = 1.0,
  ```
  Default `1.0` never trips (1.0 is not < 0.85), so all 3 existing call sites + every existing test stay byte-behaviour-identical (L3).
- Gate body — insert BETWEEN the `unresolved_rate` block (`:114-116`) and the `min_parser_confidence` block (`:118`). This is the chosen deterministic slot (right after `REASON_HIGH_UNRESOLVED`, since both set `abstained`):
  ```python
  if normalization_confidence < ABSTENTION_THRESHOLDS["min_normalization_confidence"]:
      abstained = True
      reasons.append(REASON_LOW_NORMALIZATION_CONFIDENCE)
  ```
  Note: this gate ALSO sets `abstained=True` (the second trigger besides `unresolved_rate`). Use `abstained = True` (assignment), not `abstained = abstained or ...`, so the flag is set regardless of the unresolved gate; the reason list still records both reasons independently. Update the module docstring binding note (`abstention.py:9-15`) — "only the `unresolved_rate` gate sets it" — to also name the normalization-confidence gate.

**(b) RED test first** (T5-A in `test_abstention.py`):
```python
def test_low_normalization_confidence_abstains():
    out = _clean(normalization_confidence=0.80)  # below 0.85, unresolved_rate stays 0.0
    assert out.abstained is True
    assert REASON_LOW_NORMALIZATION_CONFIDENCE in out.abstention_reasons

def test_normalization_confidence_at_threshold_does_not_abstain():
    out = _clean(normalization_confidence=0.85)  # 0.85 is NOT < 0.85
    assert REASON_LOW_NORMALIZATION_CONFIDENCE not in out.abstention_reasons
    assert out.abstained is False

def test_default_normalization_confidence_does_not_abstain():
    out = _clean()  # default 1.0
    assert REASON_LOW_NORMALIZATION_CONFIDENCE not in out.abstention_reasons
```
First test fails (no gate) → RED. `_clean` (test_abstention.py:22) forwards `**overrides`, so `normalization_confidence=...` flows through with no helper change.

**(c) Minimal change.** Signature param + 3-line gate above + docstring note.

**(d) Verify.** `.venv/Scripts/python.exe -m pytest apollo/grading/tests/test_abstention.py -q`

**Patch coverage:** the gate's true-branch is covered by `test_low_normalization_confidence_abstains`; the false/at-threshold branch by the at-threshold + default tests. 100% of T2's added lines + both branches covered.

**Reason-order pin (L4) — MUST update in this task.** `test_abstention.py:122-140` snapshots the EXACT 5-tuple. The new reason's slot is right after `REASON_HIGH_UNRESOLVED`. Update `test_reasons_deterministic_order` to (i) add `normalization_confidence=0.0` to its `kwargs` and (ii) insert `REASON_LOW_NORMALIZATION_CONFIDENCE` into the asserted tuple in second position:
```python
    assert first == (
        REASON_HIGH_UNRESOLVED,
        REASON_LOW_NORMALIZATION_CONFIDENCE,
        REASON_LOW_PARSER_CONFIDENCE,
        REASON_TRANSCRIPT_AUDIT_FAILED,
        REASON_LOW_MISCONCEPTION_CONFIDENCE,
        REASON_REFERENCE_INVALID,
    )
```
and add the import of `REASON_LOW_NORMALIZATION_CONFIDENCE` to the test module's import block (`test_abstention.py:9-18`).

### T3 — Pure-helper extraction (normalization_confidence.py) + cycle break

**(a) Exact file + insertion point.** `apollo/grading/normalization_confidence.py`.
- **Cycle break first** — change line 20 `from apollo.grading.audited_grade import AuditedGrade` to a `TYPE_CHECKING` guard near the top:
  ```python
  from typing import TYPE_CHECKING
  if TYPE_CHECKING:
      from apollo.grading.audited_grade import AuditedGrade
  ```
  Safe at runtime: `from __future__ import annotations` (line 18) makes the only use — the public function's parameter annotation `audited_grade: AuditedGrade` — a string. (Confirmed line 20 is the sole `AuditedGrade` reference.)
- **Extract the helper** — refactor the body (`:49-65`) into a new module-private function that takes `findings` directly:
  ```python
  def _normalization_confidence_over(
      findings: tuple[Finding, ...],
      resolution: ResolutionResult,
  ) -> float:
      conf_by_node = {
          rn.node_id: rn.confidence
          for rn in resolution.resolved
          if rn.resolution == "resolved" and rn.confidence is not None
      }
      backing_confidences: list[float] = []
      for finding in findings:
          if finding.kind not in _SCORED_FINDING_KINDS:
              continue
          for node_id in finding.student_node_ids:
              if node_id in conf_by_node:
                  backing_confidences.append(conf_by_node[node_id])
      if not backing_confidences:
          return NORMALIZATION_CONFIDENCE_FLOOR_WHEN_NO_SCORED_NODES
      return min(backing_confidences)
  ```
  (Add `from apollo.graph_compare.findings import Finding` alongside the existing `FindingKind` import on line 21.)
- **Public fn becomes a thin delegator** — `compute_normalization_confidence(audited_grade, resolution)` body becomes:
  ```python
      return _normalization_confidence_over(audited_grade.findings, resolution)
  ```
  Signature, name, return value, docstring intent ALL preserved — this is a pure refactor of the public function (no value change).

**(b) RED test first** (T5-C in `test_normalization_confidence.py`): a test that calls the new helper directly and asserts it equals the public function for the SAME findings:
```python
def test_helper_matches_public_function():
    findings = (
        covered_finding_with_nodes("k.a", ("a1",)),
        covered_finding_with_nodes("k.b", ("b1",)),
    )
    resolution = resolution_with(resolved_nodes=(("a1", 0.92), ("b1", 0.75)))
    from apollo.grading.normalization_confidence import _normalization_confidence_over
    assert _normalization_confidence_over(findings, resolution) == 0.75
    assert compute_normalization_confidence(audited(findings), resolution) == 0.75
```
Fails at import of `_normalization_confidence_over` (absent) → RED. All EXISTING `test_normalization_confidence.py` tests (which call the public fn) must stay green — proof the delegation preserves the value.

**(c) Minimal change.** TYPE_CHECKING guard + helper extraction + 1-line delegator + 1 import. No value change.

**(d) Verify.** `.venv/Scripts/python.exe -m pytest apollo/grading/tests/test_normalization_confidence.py -q` AND the cycle check: `.venv/Scripts/python.exe -c "import apollo.grading.audited_grade, apollo.grading.normalization_confidence"`

**Patch coverage:** the helper body is fully exercised by every existing nc test (via the delegator) + the new direct-call test covers the helper symbol. 100%.

### T4 — D1 reorder wiring (audited_grade.py)

**(a) Exact file + insertion point.** `apollo/grading/audited_grade.py:196-214` (the orchestration tail of `build_audited_grade`).
- Add import: in the `from apollo.grading.normalization_confidence import (...)` block (create it; currently absent — `audited_grade.py` does not import nc today) import `_normalization_confidence_over`. **Order caution:** `normalization_confidence.py` imports `audited_grade` only under TYPE_CHECKING after T3, so this runtime import is one-directional and safe.
- Reorder the tail to (replacing :196-214):
  ```python
      new_findings = _rewrite_findings(grade.findings, audit)
      normalization_confidence = _normalization_confidence_over(new_findings, resolution)

      abstention = apply_abstention(
          unresolved_rate=unresolved_rate_of(resolution),
          min_parser_confidence=min_parser_confidence_of(student_nodes),
          misconception_confidences=_misconception_confidences(grade.findings, resolution),
          transcript_audit_failed=transcript_audit_failed,
          reference_invalid=reference_invalid,
          misconception_bank_empty=misconception_bank_empty,
          normalization_confidence=normalization_confidence,
      )

      return AuditedGrade(
          grade=grade,
          findings=new_findings,
          abstention_reasons=abstention.abstention_reasons,
          abstained=abstention.abstained,
          suppressed_event_kinds=abstention.suppressed_event_kinds,
          alias_candidates=audit.alias_candidates,
      )
  ```
  Net change vs current: `_rewrite_findings` MOVED above `apply_abstention`; nc computed over `new_findings`; the new `normalization_confidence=` kwarg passed. `build_audited_grade`'s SIGNATURE is unchanged (no new kwarg) → `test_done_grading_unit.py:329` stays green (L5). `_misconception_confidences(grade.findings, ...)` kept as-is (see "The D1 reorder" note).

**(b) RED test first** (T5-B in `test_audited_grade.py`): two tests.
1. **nc value preserved across reorder:**
   ```python
   def test_reorder_preserves_persisted_nc_value():
       # a covered finding backed by a strong node -> nc stays high, no 1c abstain
       grade = missing_grade(covered=("k.a",))   # covered_finding has no student_node_ids
       # construct via the real path then assert the EXTERNAL nc call matches internal
       out = build_audited_grade(
           grade, transcript="x", resolution=resolution_with(resolved=2),
           student_nodes=(), candidates=(candidate("k.a"),), audit_fn=notfound_audit_fn(),
       )
       from apollo.grading.normalization_confidence import compute_normalization_confidence
       # external recompute over the SAME audited grade == the value the gate saw internally
       assert compute_normalization_confidence(out, resolution_with(resolved=2)) == 1.0  # neutral floor, nothing scored-with-backer
       assert out.abstained is False
   ```
2. **1c fires through the real orchestrator** — a covered finding whose backing resolved node is weak (`fuzzy@0.80`) → internal nc 0.80 < 0.85 → `abstained=True` with the new reason, even though `unresolved_rate` is 0:
   ```python
   def test_weak_tier_backing_triggers_1c_abstain_end_to_end():
       grade = missing_grade()  # start empty, inject a covered-with-nodes finding
       grade = replace(grade, findings=(covered_finding_with_nodes("k.a", ("a1",)),))
       res = resolution_with(resolved_nodes=(("a1", 0.80),))  # one weak (fuzzy-cap) backer
       out = build_audited_grade(
           grade, transcript="x", resolution=res, student_nodes=(),
           candidates=(candidate("k.a"),), audit_fn=notfound_audit_fn(),
       )
       assert out.abstained is True
       assert REASON_LOW_NORMALIZATION_CONFIDENCE in out.abstention_reasons
   ```
   (`replace` from `dataclasses`; `covered_finding_with_nodes` + `resolution_with(resolved_nodes=...)` are existing builders. `notfound_audit_fn` leaves the covered finding intact — no upgrade interference.)
   First assertion fails before T4 wiring (gate not passed nc) → RED.

**(c) Minimal change.** Statement reorder + 1 import + 1 kwarg pass. No signature change, no grade-math change.

**(d) Verify.** `.venv/Scripts/python.exe -m pytest apollo/grading/tests/test_audited_grade.py apollo/handlers/tests/test_done_grading_unit.py -q`

**Patch coverage:** both new tests exercise the reordered tail; the nc-preservation test covers the neutral-floor path, the end-to-end test covers the abstain path. The reordered lines are all on the single happy path through `build_audited_grade` (already covered by existing tests too). 100% of T4's changed lines.

### T5 — Tests (consolidated)

Authored across the three test files alongside their tasks (above). Summary of additions:
- **T5-A `test_abstention.py`:** `test_low_normalization_confidence_abstains`, `test_normalization_confidence_at_threshold_does_not_abstain`, `test_default_normalization_confidence_does_not_abstain`; UPDATE `test_reasons_deterministic_order` (kwargs + asserted tuple); import the new reason constant.
- **T5-B `test_audited_grade.py`:** `test_reorder_preserves_persisted_nc_value`, `test_weak_tier_backing_triggers_1c_abstain_end_to_end`; import `REASON_LOW_NORMALIZATION_CONFIDENCE`, `dataclasses.replace`, `covered_finding_with_nodes`, `notfound_audit_fn`.
- **T5-C `test_normalization_confidence.py`:** `test_helper_matches_public_function` (direct helper call + delegation equality). Existing tests are the regression net proving the value is unchanged.
- **Full-suite regression gate:** `.venv/Scripts/python.exe -m pytest apollo/resolution apollo/grading apollo/graph_compare -q` must stay GREEN (baseline 429 passed). Plus the three named purity tests and `test_done_grading_unit.py` (L5) green.

## Per-task patch-coverage notes

`diff-cover coverage.xml --compare-branch=origin/staging --fail-under=95` over the changed lines:
- **T1:** 2 added lines (dict key, reason constant) — covered by T5-A asserting both. 100%.
- **T2:** 1 signature line + 3 gate lines + docstring (docstrings not counted) — both gate branches covered by T5-A. 100%.
- **T3:** TYPE_CHECKING guard (2-3 lines) + helper (~13 lines, all exercised by existing nc tests via delegation) + 1-line delegator + 1 import — direct-call test covers the helper symbol. The `TYPE_CHECKING:` block body is import-only; if diff-cover flags the guarded import as uncovered, mark it `# pragma: no cover` (standard for TYPE_CHECKING imports) and note it as the single documented exemption.
- **T4:** statement reorder (lines already on the covered happy path) + 1 import + 1 kwarg — both new tests cover the abstain and neutral-floor paths. 100%.
- **New `.py` files:** NONE. No new module → no fresh ruff/mypy file-creation gate; ruff + mypy still run on the CHANGED lines in the 3 edited modules (all typed: `float`, `tuple[Finding, ...]`, `ResolutionResult` — no `Any`).

## Transaction scope decisions

N/A — no DB writes, no multi-table operations, no external service calls. All four tasks are pure in-memory function edits. The persisted nc value (written by `persist_comparison_run` at `done_grading.py:270-279`) is UNCHANGED — same value, same column, same txn boundary (WU-4C1 owns it). No new persistence, no migration.

## Error contract decisions

N/A — no new exceptions, no new HTTP surface, no filter changes. Abstention is a value-object outcome (`Abstention` frozen dataclass), not an error path. The existing `TranscriptAuditUnavailableError` handling in `build_audited_grade` (`:189-194`) is untouched. No client-facing response shape changes (shadow-gated; never reaches the student grade).

## Drift reconciliation (owner doc)

Reconcile `ai-ta-backend/docs/architecture/apollo.md` IN THE SAME COMMIT as the implementation (drift contract; owns `apollo/**`, `graph_compare/**`, `grading/**`):
- Document the new abstention quality gate: `min_normalization_confidence = 0.85` threshold + `REASON_LOW_NORMALIZATION_CONFIDENCE` + that it is the SECOND trigger (besides `unresolved_rate`) that sets `abstained=True`.
- Document the D1 reorder: nc is computed over POST-rewrite findings inside `build_audited_grade`, feeds the gate, and the persisted value is unchanged.
- Note the pure-helper extraction (`_normalization_confidence_over`) and the TYPE_CHECKING cycle break.
- Bump `last_verified` from `2026-06-23` to the implementation date.
- No new glob needed (all edits are existing files under already-owned globs; no new module — unlike Phase 1a's `equation_alignment.py`).

## Files touched + tests added

**Source (3 files):**
- `apollo/grading/abstention.py` — `ABSTENTION_THRESHOLDS["min_normalization_confidence"]=0.85`; `REASON_LOW_NORMALIZATION_CONFIDENCE`; `apply_abstention` gains `normalization_confidence: float = 1.0` kwarg + the gate; docstring binding note.
- `apollo/grading/normalization_confidence.py` — TYPE_CHECKING guard for `AuditedGrade`; extracted `_normalization_confidence_over(findings, resolution)`; `compute_normalization_confidence` becomes a thin delegator (value unchanged); `Finding` import.
- `apollo/grading/audited_grade.py` — D1 reorder of `build_audited_grade` tail (`_rewrite_findings` → nc → `apply_abstention(normalization_confidence=nc)` → construct); import `_normalization_confidence_over`. Signature unchanged.

**Owner doc (1 file):** `ai-ta-backend/docs/architecture/apollo.md` — drift reconciliation + `last_verified` bump.

**Tests (3 files, all existing):**
- `apollo/grading/tests/test_abstention.py` — +3 tests; UPDATE `test_reasons_deterministic_order`; +1 import.
- `apollo/grading/tests/test_audited_grade.py` — +2 tests; +imports (`REASON_LOW_NORMALIZATION_CONFIDENCE`, `replace`, `covered_finding_with_nodes`, `notfound_audit_fn`).
- `apollo/grading/tests/test_normalization_confidence.py` — +1 test (`test_helper_matches_public_function`).

## Risks

- **[HIGH] nc-value drift (the one thing 1c must not do).** If the executor computes nc over `grade.findings` (PRE-rewrite) instead of `new_findings`, the gate's nc diverges from the persisted nc (`done_grading.py:265`, which is post-rewrite). T5-B's `test_reorder_preserves_persisted_nc_value` is the guard, but its strength depends on choosing a fixture where pre- vs post-rewrite findings DIFFER (an audit upgrade present). The provided fixtures use `notfound_audit_fn` (no upgrade), so pre==post — they prove the END-TO-END value but NOT the pre/post distinction. **Mitigation (add to T5-B):** one more test with `found_audit_fn` upgrading a `missing_node` to `COVERED_NODE@0.75` AND a resolution where that upgraded key's backer sits at 0.75 → assert the gate fires (nc 0.75 < 0.85) — proving nc saw the POST-rewrite covered finding. Without this, a pre-rewrite bug could pass.
- **[MEDIUM] `abstained = True` vs `abstained = abstained or ...`.** The plan specifies plain assignment `abstained = True`. If the executor writes `or`, behaviour is identical (both gates set True), but the plan's wording is the contract — flagged so review checks the reason list still records BOTH reasons when both gates fire (T5-A `test_reasons_deterministic_order` covers this with `unresolved_rate=0.9` + `normalization_confidence=0.0`).
- **[MEDIUM] TYPE_CHECKING coverage exemption.** The guarded import line may show as uncovered in diff-cover. The plan pre-authorizes `# pragma: no cover` on it (the one documented exemption per CLAUDE.md). If diff-cover counts the `if TYPE_CHECKING:` line itself, it executes at import (False branch) and is covered.
- **[LOW] `unresolved_rate = 0.35` re-justification deferred.** Spec §8 asks to re-derive 0.35 against the post-Phase-1 corpus. That needs the labeled §10 corpus (human gate, D4) — OUT OF SCOPE. Documented; 0.35 stays byte-identical. No code risk, only a spec line intentionally not closed here.
- **[LOW] Sequencing.** Per spec §4, 1c ships AFTER 0/1a/1b. This plan is independently mergeable (default nc=1.0 trips nothing), but landing it before 1a/1b means the brake is wired with nothing yet lowering `unresolved_rate` — harmless, just inert. Fine to land in any order relative to 1a/1b; the spec ordering is about the calibration story, not a code dependency.

## Deviations I'd allow the executor

- **Combine T1+T2 into one commit** (threshold+reason+gate are one logical change) and **T3+T4 into one commit** (helper+reorder are coupled). Keep T5 RED assertions authored before each.
- **Reason slot:** the plan fixes the new reason directly after `REASON_HIGH_UNRESOLVED`. If the executor finds a stronger ordering argument (e.g. grouping both `abstained`-setting gates is already the chosen rationale), any slot is acceptable PROVIDED `test_reasons_deterministic_order` is updated to match — the tuple just must be deterministic and pinned.
- **Helper name:** `_normalization_confidence_over` is a suggestion; any private, descriptive name is fine as long as the public `compute_normalization_confidence` signature/return is preserved and `audited_grade.py` imports the chosen symbol.
- **Extra negative test welcome:** the executor MAY add the `found_audit_fn` pre/post-rewrite discriminator test from Risks #1 — it is in fact RECOMMENDED, not optional, to close the HIGH risk.
- **Do NOT** raise 0.85, touch `unresolved_rate=0.35`, change `build_audited_grade`'s signature, change the persisted nc value, or touch any grade-math / live-grade file. These are hard stops, not deviations.
