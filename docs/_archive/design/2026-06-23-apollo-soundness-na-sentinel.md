---
title: Apollo soundness N/A — closing the empty-bank fail-open (D5/D6)
status: design — implementation-ready
date: 2026-06-23
owner_doc: docs/architecture/apollo.md
branch_target: staging (PR into staging; NEVER ApolloV3)
migration: 031_apollo_soundness_applicable.sql (031 — 027..030 already exist on disk)
touches:
  - apollo/retired graph comparator/soundness.py
  - apollo/retired graph comparator/bisimilarity.py
  - apollo/retired graph comparator/scores.py
  - apollo/retired graph comparator/core.py
  - apollo/grading/persistence.py
  - apollo/grading/abstention.py
  - apollo/grading/audited_grade.py
  - apollo/handlers/done_grading.py
  - apollo/persistence/models.py
  - database/migrations/031_apollo_soundness_applicable.sql (new)
---

# Apollo soundness N/A — closing the empty-bank fail-open (D5/D6)

## 1. Problem statement

The Apollo graph-grading soundness sub-score (`soundness_score`) reports a
**verified-sound `1.0`** when, in fact, soundness was **never checked** because
the concept's misconception bank is empty/absent. This is a silent fail-open
(defects **D5/D6**): "no contradictions detected" is indistinguishable from
"there was nothing to detect contradictions against."

### The math is keyed on the student graph, not the bank

`soundness_score` only sees the student graph and counts `misc.*` nodes. Its own
docstring documents the vacuous-`1.0` branch — but that branch is keyed on the
*student graph* being empty, and is **blind to an empty bank**:

`ai-ta-backend/apollo/retired graph comparator/soundness.py:59-65`

```python
def soundness_score(student: CanonicalGraph) -> float:
    """``1 - contradiction_penalty(#contradictions)``.

    Unsupported extras and unresolved nodes contribute ZERO penalty (they are not
    contradictions). Empty student graph -> 0 contradictions -> 1.0 (vacuously
    sound; §6.1)."""
    return 1.0 - contradiction_penalty(len(contradiction_nodes(student)))
```

`contradiction_nodes` (`soundness.py:46-49`) returns S_norm nodes whose
`canonical_key` starts with `MISCONCEPTION_KEY_PREFIX` (`"misc."`, `soundness.py:34`).
With an empty bank, **no `misc.*` candidate is ever minted**, so zero contradiction
nodes resolve regardless of what the student said → `soundness = 1.0 - 0.0 = 1.0`.

### The empty-bank chain (upstream, invisible to the math)

`ai-ta-backend/apollo/handlers/done_grading.py:186-187` →
`apollo/overseer/misconception_bank.py:load_for_concept` → `_misconceptions_dict`:

```text
load_for_concept(db, concept_id=sess.concept_id)  -> []          # no rows for concept (or concept_id is None)
_misconceptions_dict([])                           -> {"misconceptions": []}
build_problem_candidates(..., {"misconceptions": []})            # candidates.py iterates empty list
   -> no misc.* candidate ever created
grade_attempt(student_canonical, reference_graph)  (done_grading.py:228)
   -> soundness_score(...) == 1.0      # FAIL-OPEN, silent, no warning
```

`done_grading.py:186` (`concept_id` is `nullable=True`, `models.py:240`) and
`misconception_bank.py` have **no guard and no warning** when the bank is empty.

### It poisons two more columns, not just soundness

1. **`scores.py:67` recomputes the same penalty inline** (it does NOT call
   `soundness_score`):

   `ai-ta-backend/apollo/retired graph comparator/scores.py:67`

   ```python
   contradiction = 1.0 - contradiction_penalty(len(contradiction_nodes(student)))
   ```

   → `SubScores.contradiction` (`scores.py:75`) → `GradeResult.contradiction_score`
   (`core.py:107`). So an empty bank silently writes a perfect `1.0` to BOTH
   `soundness_score` AND `contradiction_score`. **Any fix must touch both call
   sites or they diverge** (soundness=N/A but contradiction_score=1.0).

2. **`bisimilarity_score` folds the vacuous soundness upward.**

   `ai-ta-backend/apollo/retired graph comparator/bisimilarity.py:12-22`

   ```python
   def harmonic_mean(a: float, b: float) -> float:
       total = a + b
       if total == 0:
           return 0.0
       return 2 * a * b / total

   def bisimilarity_score(soundness: float, coverage: float) -> float:
       return harmonic_mean(soundness, coverage)
   ```

   With `soundness=1.0` vacuous and `coverage=1.0`, `bisimilarity =
   harmonic_mean(1.0, 1.0) = 1.0` → reads as "perfectly sound AND complete" for a
   concept that was never checkable.

### Persistence makes an honest N/A impossible today

- `apollo/persistence/models.py:538-540`: `coverage_score`, `soundness_score`,
  `bisimilarity_score` are `Column(Float, nullable=False)`.
- `database/migrations/026_apollo_learner_model.sql:189-191`: same three columns
  `REAL NOT NULL`.
- `bisimilarity.py:5-6` documents the NOT-NULL invariant as **load-bearing**: "a
  NaN written to a `REAL NOT NULL` column poisons downstream aggregates."
- `GradeResult.soundness_score` / `.bisimilarity_score` / `.contradiction_score`
  (`core.py:64,65,72`) and `RunRowSpec.soundness_score` / `.bisimilarity_score`
  (`persistence.py:59-60`) are bare **non-Optional `float`**.
- `contradiction_score` is the ONLY one already nullable end-to-end (`models.py:547`
  `nullable=True`; migration `026:198` `REAL`; `RunRowSpec.contradiction_score:
  float | None`, `persistence.py:67`). Proven by
  `tests/database/test_apollo_comparison_run_persistence.py:208-232`
  (`test_nullable_subscores_persist_null`).

### Blast radius today (latent, not yet user-visible)

- **No production reader** selects these columns back. The only consumers are
  `scripts/_macro_probe_report.py` (offline experiment harness) and the
  round-trip test. The student-facing rubric (`rubric_mapping.py:build_graph_sim_rubric`)
  is built from **findings + `misconception_scores`**, never from the `soundness_score`
  float, and is gated behind the OFF `APOLLO_GRAPH_SIM_LIVE_ENABLED` flag
  (`done.py:396-403`). So the defect is currently latent — but the wrong value is
  durably written and will mislead the first reader (teacher analytics, calibration,
  any AVG over the runs table).

**Goal:** make a never-checked soundness report **N/A**, not `1.0`; make
bisimilarity **renormalize to coverage** when soundness is N/A (never multiply by
`0.0`, never assume `1.0`); persist an authoritative "this scalar is coverage-only"
bit; and emit a diagnostic so the content-authoring gap is visible to ops.

---

## 2. Chosen N/A representation (and why)

### Decision — **hybrid, split by the layer boundary the code already enforces**

- **Pure score-math layer** (`soundness.py` / `bisimilarity.py` / `scores.py` /
  `core.py`): **Option A — honest `None`.** `soundness_score` and the
  `contradiction` sub-score become `float | None`; `None` means "bank empty →
  never checked." `bisimilarity_score` becomes `None`-aware and **renormalizes to
  coverage** (drops the soundness term) when soundness is `None`.
- **Persisted row + readers** (`models.py` / migration / `persistence.py`):
  **Option B — keep the three top-line columns numeric & `NOT NULL`, add a
  companion flag.** A new `soundness_applicable BOOLEAN NOT NULL DEFAULT true`
  carries the truth. When not applicable, the NOT-NULL `soundness_score` /
  `bisimilarity_score` columns persist the **coverage-only fallback value** (so
  every existing NOT-NULL aggregate stays safe), and `soundness_applicable=false`
  is the single bit that lets any reader tell a real `1.0` from a vacuous one. The
  already-nullable `contradiction_score` column persists **`NULL`** (most honest;
  zero migration for that column).
- **Abstention** records reason `misconception_bank_empty` (reuses the existing
  `abstention_reasons` JSONB column; zero migration), **reason-only — does NOT set
  `abstained=True`** (coverage and the other six dimensions are still valid; the
  run must still update Layer-3).

### Why this split

1. **The pure layer must carry honest `None`** because that is the only place
   "renormalize the harmonic mean" can be expressed before the value is frozen
   into `GradeResult` and copied 1:1 into the NOT-NULL row. If soundness stays a
   bare float here, the vacuous `1.0` is laundered into bisimilarity and is
   unrecoverable downstream.
2. **The row stays numeric + NOT-NULL + flagged** because (a) those columns are
   NOT-NULL today and three call sites + the macro-probe depend on it, (b) the
   renormalize-to-coverage rule guarantees they remain real, in-range, NaN-free,
   so the `bisimilarity.py:5-6` invariant is preserved, and (c) the project
   already uses exactly this "we couldn't fully check this" pattern
   (`abstained` / `abstention_reasons`, `models.py:549-550`, both NOT NULL).

### Rejected alternatives

| Alternative | Why rejected |
|---|---|
| **(C) Sentinel `-1.0` in the float column** | `bisimilarity.py:5-6` exists specifically because a poison value in a `REAL NOT NULL` column corrupts every downstream `AVG`/`harmonic_mean`. `harmonic_mean(-1.0, coverage)` yields a negative/sign-flipped bisimilarity and silently corrupts the macro-probe aggregates. Not acceptable. |
| **Pure-A all the way down (nullable `soundness_score`/`bisimilarity_score` columns)** | Those two columns are `REAL NOT NULL` (`models.py:538-540`, `026:189-191`) and the invariant is load-bearing. Relaxing to nullable forces every future aggregate to be NULL-aware and lets a `None` leak into `harmonic_mean` as a `TypeError` (no None-guard today). Strictly riskier than a dedicated flag. |
| **Naive "N/A → `0.0`" coercion into the harmonic mean** | `harmonic_mean(0.0, coverage)` → product 0 → bisimilarity 0 → a perfect student scored **0**. The `a+b==0 → 0.0` guard does NOT catch this (with coverage>0, `a+b != 0`, it sails through to a real-but-wrong number). This is precisely the poisoning the design must avoid. |
| **Naive "N/A → `1.0`" (today's silent behavior)** | Inflates a never-checked answer to a fake-perfect harmonic mean. This IS the D5/D6 defect. |
| **Guard inside `soundness.py` that does IO / logs** | `grade_attempt` and the score-math are PURE/no-IO/frozen (`core.py:6-7`, guarded by `test_grade_attempt_is_pure`, `test_core.py:245`). Detection/logging must live upstream in the orchestrator (`done_grading.retired graph simulation`), and the fact is **passed into** the pure functions as a bool. |
| **Compute emptiness inside the math** | The math cannot see the bank — the bank list lives only in `done_grading` (`entries`, line 186). Threading a DB-derived fact in as a plain bool keeps purity; doing the load inside the math would break it. |

### Why renormalize-to-coverage is the correct N/A bisimilarity

The top-line bisimilarity is the **harmonic mean of two factors** (soundness,
coverage). When one factor is unavailable, the harmonic mean of the single
remaining factor **is that factor**. So bisimilarity collapses to coverage. This
is the mathematically correct "renormalize over available dimensions" for a
2-term harmonic mean — not `0.0` (zeros a good answer), not `1.0` (inflates a
never-checked one).

**Worked example** — student covers everything, bank empty:

- **Today:** `soundness=1.0` (vacuous), `bisimilarity=harmonic_mean(1.0,1.0)=1.0`
  → reads "perfectly sound AND complete." Misleading.
- **New:** `soundness=None`, `bisimilarity=coverage=1.0`, **but
  `soundness_applicable=false`** persisted. Same number, now truthful: "this `1.0`
  reflects coverage only; soundness was never checked."
- **Counter-case avoided:** naive N/A→0.0 → `harmonic_mean(0.0,1.0)=0.0` → a
  perfect student scored 0. Renormalize-to-coverage makes it `1.0`, correct.

---

## 3. Concrete code diffs

> These are the proposed diffs for the implementation PR. They are reproduced
> here in the design doc; **the real source is NOT edited by this doc.**

### 3.1 `apollo/retired graph comparator/soundness.py` — gate on `bank_applicable`, return `None`

```diff
--- a/apollo/retired graph comparator/soundness.py
+++ b/apollo/retired graph comparator/soundness.py
@@ -56,9 +56,21 @@ def contradiction_penalty(n: int) -> float:
     return min(1.0, n * CONTRADICTION_UNIT_PENALTY)


-def soundness_score(student: CanonicalGraph) -> float:
-    """``1 - contradiction_penalty(#contradictions)``.
-
-    Unsupported extras and unresolved nodes contribute ZERO penalty (they are not
-    contradictions). Empty student graph -> 0 contradictions -> 1.0 (vacuously
-    sound; §6.1)."""
-    return 1.0 - contradiction_penalty(len(contradiction_nodes(student)))
+def soundness_score(
+    student: CanonicalGraph, *, bank_applicable: bool = True
+) -> float | None:
+    """``1 - contradiction_penalty(#contradictions)``, or ``None`` when the
+    misconception bank was empty/absent for this concept (D5/D6).
+
+    ``bank_applicable=False`` short-circuits to ``None`` BEFORE counting
+    contradiction nodes: with no bank, zero ``misc.*`` nodes resolve regardless
+    of what the student said, so the count is meaningless and a ``1.0`` would be
+    a fail-open "verified sound" that was NEVER checked. ``None`` means downstream
+    must EXCLUDE soundness (renormalize bisimilarity to coverage), never read it
+    as ``0.0`` or ``1.0``.
+
+    Unsupported extras and unresolved nodes still contribute ZERO penalty (they
+    are not contradictions). An EMPTY STUDENT GRAPH with an applicable bank ->
+    0 contradictions -> 1.0 (vacuously sound; §6.1) — a legitimately different
+    case from an empty BANK, and intentionally still 1.0."""
+    if not bank_applicable:
+        return None
+    return 1.0 - contradiction_penalty(len(contradiction_nodes(student)))
```

### 3.2 `apollo/retired graph comparator/bisimilarity.py` — `None`-aware, renormalize to coverage

```diff
--- a/apollo/retired graph comparator/bisimilarity.py
+++ b/apollo/retired graph comparator/bisimilarity.py
@@ -17,6 +17,18 @@ def harmonic_mean(a: float, b: float) -> float:
     return 2 * a * b / total


-def bisimilarity_score(soundness: float, coverage: float) -> float:
-    """The top-line bisimilarity: ``harmonic_mean(soundness, coverage)``."""
-    return harmonic_mean(soundness, coverage)
+def bisimilarity_score(soundness: float | None, coverage: float) -> float:
+    """``harmonic_mean(soundness, coverage)`` — or, when soundness is N/A
+    (``None``; the misconception bank was empty, D5/D6), the coverage-only
+    fallback ``bisimilarity == coverage``.
+
+    Rationale (§6.1 + the N/A rule): the harmonic mean of a SINGLE available
+    dimension is that dimension. We must NOT substitute ``soundness=1.0``
+    (inflates a never-checked answer to a fake-perfect harmonic mean) nor
+    ``soundness=0.0`` (zeros a good answer via the product — and ``a+b==0`` does
+    NOT catch it when coverage>0). Coverage-only is the only honest top-line when
+    soundness was never checked. The result is still NaN-free and in [0, 1], so
+    the ``REAL NOT NULL`` column stays safe."""
+    if soundness is None:
+        return coverage
+    return harmonic_mean(soundness, coverage)
```

### 3.3 `apollo/retired graph comparator/scores.py` — second penalty site changes in lockstep

```diff
--- a/apollo/retired graph comparator/scores.py
+++ b/apollo/retired graph comparator/scores.py
@@ -52,7 +52,7 @@ class SubScores:
     scoping: float
     usage: float
     procedure_order: float
     dependency: float
-    contradiction: float
+    contradiction: float | None


 def compute_sub_scores(
     student: CanonicalGraph,
     reference: ReferenceGraph,
     winning_path: PathCoverage,
+    *,
+    bank_applicable: bool = True,
 ) -> SubScores:
-    """Compute all 7 sub-scores over the two canonical graphs + the winning path."""
-    contradiction = 1.0 - contradiction_penalty(len(contradiction_nodes(student)))
+    """Compute all 7 sub-scores over the two canonical graphs + the winning path.
+
+    ``bank_applicable=False`` (empty/absent misconception bank, D5/D6) makes the
+    ``contradiction`` sub-score ``None`` — the SAME N/A the top-line soundness
+    carries, so the two never diverge (this is the second, independent penalty
+    call site; it does NOT delegate to ``soundness_score``)."""
+    contradiction: float | None = (
+        None
+        if not bank_applicable
+        else 1.0 - contradiction_penalty(len(contradiction_nodes(student)))
+    )
     return SubScores(
         node_coverage=winning_path.score,
         edge_coverage=_edge_coverage(student, reference, edge_type=None),
         scoping=_edge_coverage(student, reference, edge_type=EdgeType.SCOPES),
         usage=_edge_coverage(student, reference, edge_type=EdgeType.USES),
         procedure_order=_procedure_order(student, reference, winning_path),
         dependency=_dependency(student, reference),
         contradiction=contradiction,
     )
```

### 3.4 `apollo/retired graph comparator/core.py` — widen fields, add `soundness_applicable`, thread the fact (stay pure)

```diff
--- a/apollo/retired graph comparator/core.py
+++ b/apollo/retired graph comparator/core.py
@@ -52,6 +52,7 @@ class GradeResult:
     """The frozen handoff artifact. The 10 ``*_score`` fields are named 1:1 to the
     ``apollo_graph_comparison_runs`` columns so WU-4B persists with no reshaping.
+    ``soundness_applicable`` is False iff the misconception bank was empty/absent
+    for this concept (D5/D6): then ``soundness_score`` / ``contradiction_score``
+    are ``None`` and ``bisimilarity_score`` is the coverage-only fallback.

     ``comparison_confidence`` is the score-math's own value (1.0 in v1); the
     persisted confidence column is ``normalization_confidence`` (supplied by
@@ -61,16 +62,17 @@ class GradeResult:
     by this name. There is intentionally NO ``events`` field — finding->event
     conversion is WU-4B (§6.5)."""

     coverage_score: float
-    soundness_score: float
+    soundness_score: float | None
     bisimilarity_score: float
     node_coverage_score: float
     edge_coverage_score: float
     scoping_score: float
     usage_score: float
     procedure_order_score: float
     dependency_score: float
-    contradiction_score: float
+    contradiction_score: float | None
     comparison_confidence: float
+    soundness_applicable: bool = True
     findings: tuple[Finding, ...]
     comparison_version: str = COMPARISON_VERSION


 def grade_attempt(
-    student_canonical: CanonicalGraph, reference_graph: ReferenceGraph
+    student_canonical: CanonicalGraph,
+    reference_graph: ReferenceGraph,
+    *,
+    bank_applicable: bool = True,
 ) -> GradeResult:
     """Grade a student's canonical graph against the reference (§6.4 10/11/13).

     Pure: coverage (max-over-paths), soundness (contradictions-only), the 7
     sub-scores, and bisimilarity, plus the in-memory finding set. No external IO.
+
+    ``bank_applicable`` is a PLAIN BOOL fact supplied by the caller
+    (``done_grading.retired graph simulation``) — never an IO call, so purity (and
+    ``test_grade_attempt_is_pure``) is preserved. ``False`` => soundness N/A.
     """
     # Step 10 — coverage (max over declared paths).
     coverage, winning_path, _ = coverage_result(student_canonical, reference_graph)
     # Step 11 — soundness (contradictions only; None when bank empty -> N/A).
-    soundness = soundness_score(student_canonical)
+    soundness = soundness_score(student_canonical, bank_applicable=bank_applicable)
     # Sub-scores.
-    sub = compute_sub_scores(student_canonical, reference_graph, winning_path)
+    sub = compute_sub_scores(
+        student_canonical, reference_graph, winning_path, bank_applicable=bank_applicable
+    )
     # Step 13 — bisimilarity (harmonic mean; coverage-only fallback when N/A).
     bisimilarity = bisimilarity_score(soundness, coverage)

     findings = _emit_findings(student_canonical, reference_graph, winning_path)

     return GradeResult(
         coverage_score=coverage,
         soundness_score=soundness,
         bisimilarity_score=bisimilarity,
         node_coverage_score=sub.node_coverage,
         edge_coverage_score=sub.edge_coverage,
         scoping_score=sub.scoping,
         usage_score=sub.usage,
         procedure_order_score=sub.procedure_order,
         dependency_score=sub.dependency,
         contradiction_score=sub.contradiction,
         comparison_confidence=1.0,  # v1 binding
+        soundness_applicable=bank_applicable,
         findings=findings,
         comparison_version=COMPARISON_VERSION,
     )
```

> Note on dataclass field order: `findings` and `comparison_version` already have
> defaults; `soundness_applicable: bool = True` is inserted before them (all
> trailing fields keep defaults, so the frozen dataclass stays valid). All
> existing positional constructions go through the `_builders.missing_grade`
> factory and the keyword-style `GradeResult(...)` in `core.py` — both updated.

### 3.5 `apollo/grading/persistence.py` — carry the flag; coerce NOT-NULL column to coverage fallback

```diff
--- a/apollo/grading/persistence.py
+++ b/apollo/grading/persistence.py
@@ -55,9 +55,11 @@ class RunRowSpec:
     attempt_id: int
     user_id: str
     search_space_id: int
     coverage_score: float  # top-line 3 are NOT NULL (§2 schema)
-    soundness_score: float
+    soundness_score: float  # NOT NULL: holds the coverage-only fallback when N/A
     bisimilarity_score: float
+    soundness_applicable: bool  # False => soundness_score/contradiction_score are N/A
     node_coverage_score: float | None  # the 7 sub-scores are nullable (§2 schema)
     edge_coverage_score: float | None
     scoping_score: float | None
     usage_score: float | None
     procedure_order_score: float | None
     dependency_score: float | None
     contradiction_score: float | None
     normalization_confidence: float  # NOT NULL (§2 schema)
     abstained: bool
     abstention_reasons: tuple[str, ...]
     comparison_version: str
     reference_graph_hash: str
@@ -104,11 +106,21 @@ def grade_to_run_spec(
     """Map a ``GradeResult`` (scores) + ``AuditedGrade`` (abstention) onto a
     ``RunRowSpec``. The 10 ``*_score`` fields copy 1:1 from ``grade``;
     ``abstained`` / ``abstention_reasons`` come from ``audited`` (NOT recomputed);
-    the two scalars + ``comparison_version`` (off ``grade``) land verbatim."""
+    the two scalars + ``comparison_version`` (off ``grade``) land verbatim.
+
+    N/A coercion (D5/D6): ``GradeResult.soundness_score`` may be ``None`` (bank
+    empty). The ``soundness_score`` COLUMN is ``REAL NOT NULL``, so we persist the
+    coverage-only fallback (the same scalar ``bisimilarity_score`` already carries
+    via renormalization) and record the truth in ``soundness_applicable=False``.
+    ``contradiction_score`` is nullable, so its ``None`` persists as SQL NULL —
+    the most honest representation, no coercion needed."""
+    soundness_for_column = (
+        grade.soundness_score if grade.soundness_score is not None else grade.coverage_score
+    )
     return RunRowSpec(
         attempt_id=attempt_id,
         user_id=user_id,
         search_space_id=search_space_id,
         coverage_score=grade.coverage_score,
-        soundness_score=grade.soundness_score,
+        soundness_score=soundness_for_column,
         bisimilarity_score=grade.bisimilarity_score,
+        soundness_applicable=grade.soundness_applicable,
         node_coverage_score=grade.node_coverage_score,
         edge_coverage_score=grade.edge_coverage_score,
         scoping_score=grade.scoping_score,
         usage_score=grade.usage_score,
         procedure_order_score=grade.procedure_order_score,
         dependency_score=grade.dependency_score,
         contradiction_score=grade.contradiction_score,
         normalization_confidence=normalization_confidence,
         abstained=audited.abstained,
         abstention_reasons=audited.abstention_reasons,
         comparison_version=grade.comparison_version,
         reference_graph_hash=reference_graph_hash,
     )
@@ -159,6 +171,7 @@ def _run_orm_from_spec(spec: RunRowSpec) -> GraphComparisonRun:
         coverage_score=spec.coverage_score,
         soundness_score=spec.soundness_score,
         bisimilarity_score=spec.bisimilarity_score,
+        soundness_applicable=spec.soundness_applicable,
         node_coverage_score=spec.node_coverage_score,
         edge_coverage_score=spec.edge_coverage_score,
         scoping_score=spec.scoping_score,
         usage_score=spec.usage_score,
         procedure_order_score=spec.procedure_order_score,
         dependency_score=spec.dependency_score,
         contradiction_score=spec.contradiction_score,
         normalization_confidence=spec.normalization_confidence,
         abstained=spec.abstained,
         abstention_reasons=list(spec.abstention_reasons),
         comparison_version=spec.comparison_version,
         reference_graph_hash=spec.reference_graph_hash,
     )
```

### 3.6 `apollo/grading/abstention.py` — new reason + reason-only gate

```diff
--- a/apollo/grading/abstention.py
+++ b/apollo/grading/abstention.py
@@ -39,6 +39,7 @@ REASON_HIGH_UNRESOLVED = "unresolved_rate_above_threshold"
 REASON_LOW_PARSER_CONFIDENCE = "min_parser_confidence_below_threshold"
 REASON_LOW_MISCONCEPTION_CONFIDENCE = "misconception_confidence_below_threshold"
 REASON_REFERENCE_INVALID = "reference_graph_invalid"
 REASON_TRANSCRIPT_AUDIT_FAILED = "transcript_audit_unavailable"
+REASON_MISCONCEPTION_BANK_EMPTY = "misconception_bank_empty"
@@ -79,6 +80,7 @@ def apply_abstention(
     *,
     unresolved_rate: float,
     min_parser_confidence: float,
     misconception_confidences: tuple[float, ...] = (),
     transcript_audit_failed: bool = False,
     reference_invalid: bool = False,
+    misconception_bank_empty: bool = False,
 ) -> Abstention:
     """Apply the §6.6 gates and return the reasons + flags + suppression set.

     - ``unresolved_rate > 0.35``      -> ``abstained=True``, REASON_HIGH_UNRESOLVED
                                          (no Layer-3 update; diagnostic-only run)
     - ``min_parser_confidence < 0.6`` -> suppress ``missing``,
                                          REASON_LOW_PARSER_CONFIDENCE
     - ``transcript_audit_failed``     -> suppress ``missing``,
                                          REASON_TRANSCRIPT_AUDIT_FAILED
     - any ``misconception_confidence < 0.8`` -> suppress ``misconception``,
                                          REASON_LOW_MISCONCEPTION_CONFIDENCE
                                          (the finding still persists for review)
     - ``reference_invalid``           -> REASON_REFERENCE_INVALID (grading already
                                          blocked upstream; surfaced here, not
                                          re-raised)
+    - ``misconception_bank_empty``    -> REASON_MISCONCEPTION_BANK_EMPTY (D5/D6:
+                                         soundness was N/A; reason-only, does NOT
+                                         set ``abstained`` — coverage + the other
+                                         six dimensions are still valid, so the
+                                         run still updates Layer-3)

     Pure + deterministic reason ordering (gate-declaration order)."""
     reasons: list[str] = []
     suppressed: set[str] = set()

     abstained = unresolved_rate > ABSTENTION_THRESHOLDS["unresolved_rate"]
     if abstained:
         reasons.append(REASON_HIGH_UNRESOLVED)

     if min_parser_confidence < ABSTENTION_THRESHOLDS["min_parser_confidence"]:
         reasons.append(REASON_LOW_PARSER_CONFIDENCE)
         suppressed.add(_SUPPRESS_MISSING)

     if transcript_audit_failed:
         reasons.append(REASON_TRANSCRIPT_AUDIT_FAILED)
         suppressed.add(_SUPPRESS_MISSING)

     if any(
         c < ABSTENTION_THRESHOLDS["misconception_confidence"] for c in misconception_confidences
     ):
         reasons.append(REASON_LOW_MISCONCEPTION_CONFIDENCE)
         suppressed.add(_SUPPRESS_MISCONCEPTION)

     if reference_invalid:
         reasons.append(REASON_REFERENCE_INVALID)

+    if misconception_bank_empty:
+        reasons.append(REASON_MISCONCEPTION_BANK_EMPTY)
+
     return Abstention(
         abstention_reasons=tuple(reasons),
         abstained=abstained,
         suppressed_event_kinds=frozenset(suppressed),
     )
```

### 3.7 `apollo/grading/audited_grade.py` — forward the new kwarg

```diff
--- a/apollo/grading/audited_grade.py
+++ b/apollo/grading/audited_grade.py
@@ -163,6 +163,7 @@ def build_audited_grade(
     grade: GradeResult,
     *,
     transcript: str,
     resolution: ResolutionResult,
     student_nodes: tuple[Node, ...],
     candidates: tuple[Candidate, ...] = (),
     reference_invalid: bool = False,
+    misconception_bank_empty: bool = False,
     audit_fn: AuditFn | None = None,
 ) -> AuditedGrade:
     """Orchestrate §6.4 step 12 + step 14 into the frozen :class:`AuditedGrade`.

     ``audit_fn`` defaults to the live :func:`main_chat_auditor`; every test
-    injects a deterministic stub (CI-safe, no live LLM)."""
+    injects a deterministic stub (CI-safe, no live LLM).
+
+    ``misconception_bank_empty`` (D5/D6) is forwarded verbatim to
+    :func:`apply_abstention` so the ``misconception_bank_empty`` reason is
+    recorded on the run (reason-only; does not force ``abstained``)."""
     missing = _missing_entities(grade.findings, candidates)

     transcript_audit_failed = False
     try:
         audit = audit_missing(missing, transcript, audit_fn=audit_fn)
     except TranscriptAuditUnavailableError:
         transcript_audit_failed = True
         audit = AuditResult(upgraded_keys=frozenset(), spans_by_key={}, alias_candidates=())

     abstention = apply_abstention(
         unresolved_rate=unresolved_rate_of(resolution),
         min_parser_confidence=min_parser_confidence_of(student_nodes),
         misconception_confidences=_misconception_confidences(grade.findings, resolution),
         transcript_audit_failed=transcript_audit_failed,
         reference_invalid=reference_invalid,
+        misconception_bank_empty=misconception_bank_empty,
     )
```

### 3.8 `apollo/handlers/done_grading.py` — detection, warn-log, thread into grade + audit

```diff
--- a/apollo/handlers/done_grading.py
+++ b/apollo/handlers/done_grading.py
@@ -184,8 +184,21 @@ async def retired graph simulation(
     # ---- Steps 1-4: assemble inputs + the step-4 raw-graph gate (no writes) ----
     entries = await load_for_concept(db, concept_id=sess.concept_id)  # type: ignore[arg-type]
     misconceptions = _misconceptions_dict(entries)

+    # D5/D6 — soundness applicability. An empty/absent misconception bank (no rows
+    # for the concept, or a NULL concept_id which can never have a bank) means no
+    # misc.* candidate is ever minted, so a "0 contradictions -> 1.0" soundness
+    # would be FAIL-OPEN ("verified sound" that was never checked). Detect HERE
+    # (the only non-test caller of load_for_concept; the list is already in hand)
+    # and thread the fact into the PURE grader + the abstention reason. Detection
+    # stays in the orchestrator so the score-math remains IO/log-free (purity +
+    # test_grade_attempt_is_pure).
+    bank_applicable = bool(entries) and sess.concept_id is not None
+    if not bank_applicable:
+        _LOG.warning(
+            "soundness_not_applicable_empty_bank",
+            extra={
+                "concept_id": sess.concept_id,
+                "attempt_id": int(attempt.id),
+                "search_space_id": int(sess.search_space_id),
+            },
+        )
+
     specs = await load_entity_specs(
         db,
         search_space_id=int(sess.search_space_id),
         concept_id=sess.concept_id,  # type: ignore[arg-type]  # nullable col, bound at grade time
     )
@@ -226,7 +239,7 @@ async def retired graph simulation(

         # Step 8 — grade (pure).
-        grade = grade_attempt(student_canonical, reference_graph)
+        grade = grade_attempt(student_canonical, reference_graph, bank_applicable=bank_applicable)

         # Step 9 — transcript audit (live auditor; suppress-all-missing on infra
         # failure is handled INSIDE build_audited_grade).
         transcript = await _read_transcript(db, attempt_id=int(attempt.id))
         student_nodes = tuple(student_graph.nodes)
         audited = build_audited_grade(
             grade,
             transcript=transcript,
             resolution=resolution,
             student_nodes=student_nodes,
             candidates=inputs.candidates,
             reference_invalid=False,
+            misconception_bank_empty=not bank_applicable,
             audit_fn=main_chat_auditor,
         )
```

### 3.9 `apollo/persistence/models.py` — new flag column

```diff
--- a/apollo/persistence/models.py
+++ b/apollo/persistence/models.py
@@ -538,7 +538,12 @@ class GraphComparisonRun(Base):
     coverage_score = Column(Float, nullable=False)
     soundness_score = Column(Float, nullable=False)
     bisimilarity_score = Column(Float, nullable=False)
+    # D5/D6: False iff the misconception bank was empty/absent for this concept.
+    # Then soundness_score / bisimilarity_score hold the COVERAGE-ONLY fallback
+    # (NOT NULL invariant preserved, bisimilarity.py:5-6) and contradiction_score
+    # is NULL. Readers MUST consult this before trusting/averaging soundness.
+    soundness_applicable = Column(
+        Boolean, nullable=False, server_default=text("true"), default=True
+    )
     node_coverage_score = Column(Float, nullable=True)
     edge_coverage_score = Column(Float, nullable=True)
     scoping_score = Column(Float, nullable=True)
     usage_score = Column(Float, nullable=True)
     procedure_order_score = Column(Float, nullable=True)
     dependency_score = Column(Float, nullable=True)
     contradiction_score = Column(Float, nullable=True)
```

> `Boolean` and `text` are already imported in `models.py` (used by `abstained =
> Column(Boolean, nullable=False, server_default=text("false"), default=False)`,
> line 549) — no new imports.

---

## 4. Persistence change — migration 031

**Migration number:** the on-disk migrations top out at
`030_apollo_autoprovisioning.sql` (027, 028, 029, 030 all already exist). The next
free number is **031**. (The CHOSEN DESIGN's "027" was based on a stale max of 026;
corrected here against the actual directory.)

One **additive, backfill-safe** column. `DEFAULT true` backfills every existing
row as "applicable" — correct, because all historical runs were computed with the
old always-numeric path. No existing column is relaxed; the NOT-NULL invariant on
`soundness_score` / `bisimilarity_score` is **kept** (the renormalize-to-coverage
rule guarantees those stay real, in-range, NaN-free).

`database/migrations/031_apollo_soundness_applicable.sql` (new):

```sql
-- 031_apollo_soundness_applicable.sql
-- D5/D6 — soundness N/A on an empty misconception bank.
-- Design: docs/design/2026-06-23-apollo-soundness-na-sentinel.md
--
-- Adds a single authoritative flag distinguishing a VERIFIED-sound score from a
-- never-checked one (the bank was empty/absent for the concept). When false:
--   * soundness_score / bisimilarity_score hold the COVERAGE-ONLY fallback value
--     (NOT NULL kept; the harmonic mean renormalizes to coverage, never 0.0/1.0);
--   * contradiction_score is NULL (already nullable; no change needed).
--
-- DEPLOY-TIME RECONCILIATION (read before applying anywhere — DO NOT auto-apply):
--   * On-disk migrations top out at 030_apollo_autoprovisioning.sql; 031 is next free.
--   * Applied to LOCAL Docker Postgres ONLY by agents. Rehearsal on the TEST
--     Supabase project then prod is a human/CI step.
--
-- ROLLBACK: dropping soundness_applicable loses the verified-vs-N/A distinction;
-- the numeric columns are unaffected (they always held an in-range scalar). Safe
-- direction: after rollback every row reads as if applicable (pre-031 behavior).

BEGIN;

ALTER TABLE apollo_graph_comparison_runs
    ADD COLUMN IF NOT EXISTS soundness_applicable BOOLEAN NOT NULL DEFAULT true;

COMMENT ON COLUMN apollo_graph_comparison_runs.soundness_applicable IS
    'D5/D6: false iff the misconception bank was empty/absent for the concept; '
    'then soundness_score/bisimilarity_score are the coverage-only fallback and '
    'contradiction_score is NULL. Readers must check this before trusting soundness.';

COMMIT;
```

**Why not relax the existing columns to nullable?** Covered in §2 — NOT-NULL is
load-bearing (`bisimilarity.py:5-6`); a flag + numeric fallback is strictly safer
and backward-compatible with the offline `scripts/_macro_probe_report.py`.

---

## 5. Test plan (>=95% patch coverage, LOCAL docker postgres only)

All new/changed lines are cheap to cover. Pure-math and pure-orchestration tests
need **no DB**; the persistence round-trip uses the existing `db_session`
pgvector Testcontainer (`tests/conftest.py`, re-exported via `apollo/conftest.py`)
which **`pytest.skip`s cleanly when Docker is down** and NEVER touches a remote
Supabase project (honors the CLAUDE.md DB contract). No migration policy×role
enumeration applies — the migration is a single additive NOT-NULL-with-default
column (line-coverage-measured Python only).

Run gate:

```bash
cd ai-ta-backend
pytest --cov --cov-report=xml apollo tests/database/test_apollo_comparison_run_persistence.py
diff-cover coverage.xml --compare-branch=origin/staging --fail-under=95
```

### Case matrix (the required enumeration)

| # | Case | Asserts | File | DB? |
|---|---|---|---|---|
| 1 | **Empty bank → N/A (NOT 1.0)** at the math layer | `soundness_score(student, bank_applicable=False) is None` | `test_soundness.py` | no |
| 2 | **Non-empty bank, 0 contradictions → REAL 1.0** | `soundness_score(student, bank_applicable=True) == 1.0` AND default `soundness_score(student) == 1.0` (existing cases unchanged) | `test_soundness.py` | no |
| 3 | **Non-empty bank, contradictions still penalize** | existing `test_single_contradiction_penalized` / `test_two_contradictions_floor_zero` unchanged (regression guard) | `test_soundness.py` | no |
| 4 | **Contradiction sub-score N/A in lockstep** | `compute_sub_scores(..., bank_applicable=False).contradiction is None`; `bank_applicable=True` → real value | `test_scores.py` | no |
| 5 | **Bisimilarity when soundness is N/A** | `bisimilarity_score(None, 0.8) == 0.8`; `bisimilarity_score(None, 0.0) == 0.0`; both `not math.isnan(...)` | `test_bisimilarity.py` | no |
| 6 | **Bisimilarity unchanged for real soundness** | existing `test_bisimilarity_score_delegates_to_harmonic_mean` (regression) | `test_bisimilarity.py` | no |
| 7 | **`grade_attempt` propagates N/A + flag** | `bank_applicable=False` → `g.soundness_score is None`, `g.contradiction_score is None`, `g.bisimilarity_score == g.coverage_score`, `g.soundness_applicable is False`; `True` → all numeric, flag True | `test_core.py` | no |
| 8 | **`grade_attempt` stays pure** | existing `test_grade_attempt_is_pure` (regression — proves the bool arg did not introduce IO) | `test_core.py` | no |
| 9 | **Abstention reason present / `abstained` unchanged** | `apply_abstention(..., misconception_bank_empty=True)` → `REASON_MISCONCEPTION_BANK_EMPTY in reasons` AND `abstained is False`; `False` → reason absent | `test_abstention.py` | no |
| 10 | **`build_audited_grade` forwards the kwarg** | with a stub `audit_fn` (`notfound_audit_fn`), `misconception_bank_empty=True` → reason in `audited.abstention_reasons`, `audited.abstained is False` | `test_audited_grade.py` | no |
| 11 | **Detection wiring — empty `entries` → `bank_applicable=False`** | patch `grade_attempt` + `build_audited_grade` (mocks); with `load_for_concept → []` assert both called with `bank_applicable=False` / `misconception_bank_empty=True`; assert `_LOG.warning("soundness_not_applicable_empty_bank", ...)` emitted (caplog) | `test_done_grading_unit.py` | no |
| 12 | **Detection wiring — non-empty `entries` → `bank_applicable=True`** | `load_for_concept → [entry]` → both called with `bank_applicable=True` / `misconception_bank_empty=False`; NO warning | `test_done_grading_unit.py` | no |
| 13 | **Detection wiring — `concept_id is None` → N/A** | even with non-empty list semantics, a `None` `concept_id` forces `bank_applicable=False` (third branch of `bool(entries) and sess.concept_id is not None`) | `test_done_grading_unit.py` | no |
| 14 | **Persistence round-trip of N/A** | `grade.soundness_score=None`, `contradiction_score=None`, `soundness_applicable=False`, `bisimilarity_score=coverage` → run row has `soundness_applicable is False`, `contradiction_score IS NULL`, `soundness_score == coverage` (fallback), `abstention_reasons` contains `"misconception_bank_empty"` | `test_apollo_comparison_run_persistence.py` | **yes (pgvector)** |
| 15 | **Persistence round-trip of applicable (regression)** | applicable grade → `soundness_applicable is True`, `soundness_score`/`contradiction_score` numeric (existing round-trip extended with the flag default) | `test_apollo_comparison_run_persistence.py` | **yes (pgvector)** |

### New / changed test snippets (illustrative, not exhaustive)

`apollo/retired graph comparator/tests/test_soundness.py` (+ case 1; existing 1.0 cases kept):

```python
def test_empty_bank_soundness_is_na_not_one():
    # D5/D6: empty/absent misconception bank -> N/A (None), NOT vacuous 1.0.
    student = snorm(nodes=(cnode("eq.a"),))
    assert soundness_score(student, bank_applicable=False) is None
    # the SAME student WITH a bank is a real 1.0 (0 contradictions):
    assert soundness_score(student, bank_applicable=True) == 1.0
```

`apollo/retired graph comparator/tests/test_bisimilarity.py` (+ case 5):

```python
def test_bisimilarity_na_soundness_renormalizes_to_coverage():
    assert bisimilarity_score(None, 0.8) == 0.8          # coverage-only fallback
    r = bisimilarity_score(None, 0.0)
    assert r == 0.0 and not math.isnan(r)                # still NaN-free, in-range
```

`apollo/retired graph comparator/tests/test_core.py` (+ case 7):

```python
def test_grade_attempt_empty_bank_is_na_and_flagged():
    g = grade_attempt(student_canonical, reference_graph, bank_applicable=False)
    assert g.soundness_score is None
    assert g.contradiction_score is None
    assert g.soundness_applicable is False
    assert g.bisimilarity_score == g.coverage_score      # renormalized to coverage
```

`apollo/grading/tests/test_abstention.py` (+ case 9):

```python
def test_misconception_bank_empty_reason_is_reason_only():
    out = apply_abstention(
        unresolved_rate=0.0, min_parser_confidence=1.0, misconception_bank_empty=True
    )
    assert REASON_MISCONCEPTION_BANK_EMPTY in out.abstention_reasons
    assert out.abstained is False                        # coverage still updates Layer-3
```

`tests/database/test_apollo_comparison_run_persistence.py` (+ case 14, on
`db_session`; mirrors `test_nullable_subscores_persist_null` and uses
`dataclasses.replace` on a `_grade_with_findings()` grade):

```python
async def test_na_soundness_round_trip(db_session):
    import dataclasses
    attempt_id, search_space_id = await _seed_attempt(db_session)
    grade, graded = _grade_with_findings()
    grade = dataclasses.replace(
        grade,
        soundness_score=None,
        contradiction_score=None,
        soundness_applicable=False,
        bisimilarity_score=grade.coverage_score,  # renormalized
    )
    graded = dataclasses.replace(
        graded, abstention_reasons=("misconception_bank_empty",)
    )
    run_id = await persist_comparison_run(
        db_session, attempt_id=attempt_id, user_id=_USER_ID,
        search_space_id=search_space_id, grade=grade, audited=graded,
        normalization_confidence=1.0, reference_graph_hash=_REF_HASH,
    )
    run = (await db_session.execute(
        select(GraphComparisonRun).where(GraphComparisonRun.id == run_id)
    )).scalar_one()
    assert run.soundness_applicable is False
    assert run.contradiction_score is None
    assert run.soundness_score == grade.coverage_score   # NOT-NULL fallback
    assert "misconception_bank_empty" in run.abstention_reasons
```

### Patch-coverage accounting (every changed line is hit)

- `soundness.py` new `if not bank_applicable: return None` — cases 1, 7, 14.
- `bisimilarity.py` new `if soundness is None: return coverage` — cases 5, 7, 14.
- `scores.py` ternary both arms — case 4 (both), 7.
- `core.py` widened fields + threaded args + `soundness_applicable=...` — cases 7, 8.
- `persistence.py` `soundness_for_column` both branches + new spec/ORM field —
  case 14 (None branch), case 15 (not-None branch).
- `abstention.py` new constant + gate (true/false) — case 9.
- `audited_grade.py` new kwarg + forward — case 10.
- `done_grading.py` `bank_applicable` expr (3 branches: empty list, None concept,
  populated) + `_LOG.warning` — cases 11, 12, 13.
- `models.py` new column — exercised by cases 14, 15 (`create_all`).
- migration 031 — applied by the local Docker harness when the suite runs against
  a migration-driven DB; the ORM `create_all` path covers the column for the
  Testcontainer tests.

**Coverage exemptions:** none expected. If `_LOG.warning`'s `extra=` dict is
flagged as a partially-covered branch by the formatter, cover it explicitly in
case 11 via `caplog`. No changed line is exempted.

---

## 6. Rollout note (branching model)

Per `CLAUDE.md`: **`ApolloV3` is LIVE/prod — never branch off it, never commit to
it.** All work cuts from **`staging`** and PRs back into **`staging`**.

1. `git fetch origin && git switch staging && git pull` (inside `ai-ta-backend/`,
   which is its own git repo).
2. `git switch -c fix/apollo-soundness-na-empty-bank` (off `staging`).
3. Implement §3 diffs + §4 migration. Update the owner doc
   `docs/architecture/apollo.md` (the `soundness_score` / `bisimilarity` /
   `apollo_graph_comparison_runs` interface descriptions and the empty-bank
   behavior) in the SAME commit, and bump its `last_verified` to `2026-06-23`
   (drift-prevention contract).
4. Run the §5 gate locally (Docker up): `pytest --cov --cov-report=xml ...` then
   `diff-cover coverage.xml --compare-branch=origin/staging --fail-under=95`.
   **Agents do NOT apply migration 031 to any remote Supabase project** — local
   Docker Postgres only. Rehearsal on the TEST project then prod is a human/CI step.
5. Open a PR **into `staging`** (never `ApolloV3`). PR description: link this doc,
   list the 9 source files + new migration 031, state the >=95% patch result, and
   note that no UI changes are involved (the rubric is honest-by-omission and
   gated OFF; no teacher/student surface reads these columns yet).
6. CI integration job enforces the 95% patch gate (`ai-ta-backend/.github/workflows/ci.yml`).
7. Promotion to prod is a SEPARATE `staging → ApolloV3` PR, plus the human/CI
   migration-031 apply (TEST rehearsal → prod) — out of scope for this branch.

---

## 7. Files / functions changed (exhaustive)

| File | Change |
|---|---|
| `apollo/retired graph comparator/soundness.py:59-65` | `soundness_score(student, *, bank_applicable=True) -> float \| None`; `None` when not applicable |
| `apollo/retired graph comparator/bisimilarity.py:20-22` | `bisimilarity_score(soundness: float \| None, coverage)`; `None → return coverage` |
| `apollo/retired graph comparator/scores.py:47-76` | `SubScores.contradiction: float \| None`; `compute_sub_scores(..., *, bank_applicable=True)`; `contradiction → None` when not applicable |
| `apollo/retired graph comparator/core.py:52-111` | `GradeResult.soundness_score: float \| None`, `.contradiction_score: float \| None`, new `.soundness_applicable: bool = True`; `grade_attempt(..., *, bank_applicable=True)` threads into both call sites; stays pure |
| `apollo/grading/persistence.py:50-72, 94-127, 156-178` | `RunRowSpec.soundness_applicable: bool`; `grade_to_run_spec` coerces NOT-NULL `soundness_score` to coverage fallback when `None`, copies the flag; `_run_orm_from_spec` writes the new column |
| `apollo/grading/abstention.py:39-43, 79-131` | new `REASON_MISCONCEPTION_BANK_EMPTY`; new `misconception_bank_empty: bool=False` reason-only gate (does NOT set `abstained`) |
| `apollo/grading/audited_grade.py:163-195` | `build_audited_grade(..., misconception_bank_empty=False)` forwards to `apply_abstention` |
| `apollo/handlers/done_grading.py:186-187, 228, 234` | `bank_applicable = bool(entries) and sess.concept_id is not None`; `_LOG.warning("soundness_not_applicable_empty_bank", ...)`; pass into `grade_attempt` + `build_audited_grade` |
| `apollo/persistence/models.py:538-547` | add `soundness_applicable = Column(Boolean, nullable=False, server_default=text("true"), default=True)` |
| `database/migrations/031_apollo_soundness_applicable.sql` (new) | `ALTER TABLE apollo_graph_comparison_runs ADD COLUMN IF NOT EXISTS soundness_applicable BOOLEAN NOT NULL DEFAULT true;` |
| `apollo/grading/rubric_mapping.py` | **NO change** — rubric is findings-driven + honest-by-omission; confirmed |

**Tests touched:** `test_soundness.py` (+1), `test_bisimilarity.py` (+1 with two
asserts), `test_scores.py` (+1), `test_core.py` (+1; keep `test_grade_attempt_is_pure`),
`test_abstention.py` (+1), `test_audited_grade.py` (+1),
`test_done_grading_unit.py` (+3 wiring branches incl. the warn-log),
`tests/database/test_apollo_comparison_run_persistence.py` (+1 N/A round-trip; extend
1 existing for the flag default). Builders: extend
`apollo/grading/tests/_builders.py::missing_grade` and any positional
`GradeResult(...)` construction to pass `soundness_applicable` (defaulted, so most
call sites are unaffected).
