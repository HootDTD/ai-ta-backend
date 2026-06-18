# WU-4B2 — Finding → Event conversion (§6.5 decision table)

**Branch:** `feat/apollo-kg-wu4b2-finding-to-event` (already checked out — do NOT
branch/switch/push/PR).
**Base for diff-cover:** `feat/apollo-kg-wu4b1-transcript-audit-abstention`.
**Owner doc:** `docs/architecture/apollo.md` (reconcile the `apollo/grading/`
row; `last_verified` already `2026-06-17` — keep/confirm).
**Spec:** `docs/superpowers/specs/2026-06-10-apollo-kg-learner-model-architecture-decision.md`
§2 (event shape, lines 240–271), §3 + line 424 (event weights / `corrected`
semantics), §6.4 step 16, §6.5 (the BINDING table, lines 757–780), §6.6
(abstention — context only; gates already computed by WU-4B1).

This is a PURE, table-driven unit: NO DB, NO LLM, NO Neo4j, NO containers, NO
migration. It converts the already-audited, already-abstention-tagged
`AuditedGrade` (frozen WU-4B1 output) into in-memory `LearnerEvent`s. Persistence
of those events is WU-5A; this unit only PRODUCES them.

---

## 1. Grounding facts (verified against real code 2026-06-17)

| Fact | Source file:line | Consequence for 4B2 |
|---|---|---|
| `AuditedGrade` carries `.grade` (GradeResult), `.findings` (tuple, ALREADY audit-upgraded), `.abstained`, `.suppressed_event_kinds` (frozenset of `'missing'`/`'misconception'`), `.abstention_reasons`, `.alias_candidates`. | `apollo/grading/audited_grade.py:59-74` | 4B2 reads `.findings`, `.abstained`, `.suppressed_event_kinds` ONLY. Frozen — never mutated. |
| Audit-upgraded findings are emitted as `kind=COVERED_NODE`, `confidence=0.75` (== `TRANSCRIPT_AUDIT_CONFIDENCE_CAP`), `message == AUDIT_UPGRADE_MESSAGE` (`"upgraded by transcript_audit"`), carrying `evidence_spans`. | `apollo/grading/audited_grade.py:56, 98-111` | A genuine `llm`-tier covered node ALSO has `confidence=0.75`. The table MUST key the audit-upgrade row on the `AUDIT_UPGRADE_MESSAGE` marker — NOT kind or confidence alone. Import the marker from `audited_grade`. |
| `Finding` (frozen): `kind`, `canonical_key`, `student_node_ids`, `reference_node_ids`, `evidence_spans`, `score`, `confidence`, `message`. `contradiction_finding` leaves `confidence=None`; `covered_finding` sets it; `missing_finding` sets `score=0.0`. | `apollo/graph_compare/findings.py:40-92` | Event `score`/`confidence`/`misconception_code`/`evidence_node_ids`/`reference_step_id` are derived from these fields. Coalesce `None` confidence defensively. |
| `FindingKind` StrEnum: `COVERED_NODE`, `MISSING_NODE`, `MATCHED_EDGE`, `MISSING_EDGE`, `UNSUPPORTED_EXTRA`, `CONTRADICTION`, `UNRESOLVED`, `ALTERNATIVE_PATH`. | `apollo/graph_compare/findings.py:27-37` | The decision table switches on `finding.kind`. `MATCHED_EDGE`/`MISSING_EDGE`/`ALTERNATIVE_PATH` → no event (diagnostic-only, same as `UNSUPPORTED_EXTRA`/`UNRESOLVED`). |
| `Candidate` carries `canonical_key`, `is_misconception`, `opposes_key` (`str | None`). | `apollo/resolution/candidates.py:56-73` | `build_opposes_map` maps each misconception candidate's `canonical_key → opposes_key`. |
| `MASTERY_EVENT_KINDS = ("covered", "missing", "partial", "misconception", "corrected")` — OPEN enum, documentation tuple. | `apollo/persistence/models.py:63` | `LearnerEventKind` value-set MUST equal this (asserted by a test, mirroring `test_finding_kind_unchanged`). |
| `apollo_mastery_events` columns: `event_kind`, `score`, `misconception_code`, `evidence_node_ids` (JSONB), `reference_step_id`. `parser_confidence`/`grader_confidence`/`prior_belief`/`posterior_belief`/`mastery_after` are filled at the BELIEF update. | spec §2 lines 240–268 | `LearnerEvent` carries `canonical_key`, `event_kind`, `score`, `confidence`, `misconception_code`, `evidence_node_ids`, `reference_step_id`. Do NOT add belief/parser fields — those are WU-5A. |
| `Node` (in-memory) has NO `created_at`/`turn_index` field. | `apollo/ontology/nodes.py` (only `build_node`; no temporal field) | Turn order CANNOT be read off the finding. It is an INJECTED `turn_order: Mapping[node_id, int]` keyword (proposal risk#1 locked decision). |
| Existing grading test scaffolding: `_builders.py`, `test_package_seam.py` (public-API + value-set parity test pattern). | `apollo/grading/tests/` | New tests mirror these. Reuse/extend `_builders.py` helpers (`covered_finding`, `contradiction_finding`, `missing_finding`, `candidate`, `missing_grade`). |

---

## 2. Files to create / edit

### CREATE `apollo/grading/event_model.py` (~70 lines)
The frozen value objects + the kind enum + the version constant. Kept separate
from `events.py` so the data shape is a tiny, dependency-light module (mirrors
`graph_compare/findings.py` carrying the type, `core.py` carrying the logic).

```python
from __future__ import annotations
from dataclasses import dataclass
from enum import StrEnum

# Bumped when the §6.5 mapping changes; WU-5A/persist reads it onto the event row
# provenance (NOT a DB column in v1 — carried for replay/version parity).
EVENT_CONVERSION_VERSION: str = "finding-to-event-v1"


class LearnerEventKind(StrEnum):
    """§2 mastery event_kind set. Value-set == models.MASTERY_EVENT_KINDS (asserted)."""
    COVERED = "covered"
    MISSING = "missing"
    PARTIAL = "partial"
    MISCONCEPTION = "misconception"
    CORRECTED = "corrected"


@dataclass(frozen=True)
class LearnerEvent:
    """One in-memory learner-model event (§6.4 step 16). Maps onto
    apollo_mastery_events columns; WU-5A persists it atomically with the belief
    update and fills parser/grader/belief columns (NOT this unit's concern)."""
    canonical_key: str
    event_kind: LearnerEventKind
    score: float | None = None
    confidence: float | None = None
    misconception_code: str | None = None       # misconception/corrected only
    evidence_node_ids: tuple[str, ...] = ()       # the finding's student_node_ids
    reference_step_id: str | None = None          # covered/missing — the ref node id
    diagnostic_flags: tuple[str, ...] = ()        # 'edge-gap' | 'mixed-understanding'
```

`diagnostic_flags` is a small immutable tuple carrying the §6.5 diagnostic-only
labels (`edge-gap` on a covered-with-missing-edge row; `mixed-understanding` on an
ambiguous-order partial). It is NOT a DB column — it is consumed by §6.8
diagnostics (WU-4C). Carrying it here keeps the table the single source of those
labels without leaking diagnostics logic into this unit.

### CREATE `apollo/grading/opposes.py` (~25 lines)
The opposes-map builder (its own module — a tiny pure adapter over `Candidate`,
exactly as `candidates.py` adapters are pure).

```python
from __future__ import annotations
from collections.abc import Mapping
from apollo.resolution.candidates import Candidate

def build_opposes_map(candidates: tuple[Candidate, ...]) -> Mapping[str, str]:
    """Each misconception candidate's canonical_key -> the entity it opposes.

    Only misconception candidates with a non-None opposes_key are included
    (§6.5: opposes-links make the conflict rows detectable). Returns an immutable
    dict; a non-misconception or opposes_key=None candidate contributes nothing."""
    return {
        c.canonical_key: c.opposes_key
        for c in candidates
        if c.is_misconception and c.opposes_key is not None
    }
```

### CREATE `apollo/grading/events.py` (~220 lines)
The §6.5 decision table — the single public callable + the module gate constant +
pure helpers. This is the unit's core. Module constants:

```python
# §6.5 row 2 calibration gate: the covered-with-edge-missing 'partial' variant is
# DISABLED in v1. Enabling it would let an edge gap halve a score = the §6.2
# Layer-3 bias the demotion rule forbids. DO NOT flip without edge-recall proof.
PARTIAL_EDGE_GAP_ENABLED: bool = False
```

Public signature (LOCKED):

```python
def convert_findings_to_events(
    audited_grade: AuditedGrade,
    *,
    opposes_map: Mapping[str, str],
    turn_order: Mapping[str, int],
) -> tuple[LearnerEvent, ...]:
    """Convert an AuditedGrade's findings into learner-model events per §6.5.

    PURE over the injected ``turn_order`` (node_id -> turn position, supplied by
    WU-4C from apollo_messages.turn_index / Neo4j created_at; by fixtures in
    tests). 4B2 NEVER queries apollo_messages or Neo4j.

    Honors WU-4B1's abstention outcome:
    - audited_grade.abstained is True  -> return () (no learner update at all).
    - otherwise DROP every event whose kind is in
      audited_grade.suppressed_event_kinds ('missing' drops missing events;
      'misconception' drops misconception events; 'corrected' is NOT
      'misconception' so it survives a misconception suppression).
    """
```

#### Decision-table algorithm (the §6.5 binding rows, in order)

1. **Abstention short-circuit:** `if audited_grade.abstained: return ()`.
2. **Group findings by entity.** The per-entity identity is `finding.canonical_key`.
   For each entity, partition its findings into `covered`, `missing`,
   `contradiction` buckets (edge / unsupported_extra / unresolved / matched_edge /
   missing_edge / alternative_path are skipped — diagnostic-only, no event).
   - A finding's "audit-upgraded" flag = `finding.kind == COVERED_NODE and
     finding.message == AUDIT_UPGRADE_MESSAGE` (marker-keyed, NOT confidence).
3. **Conflict detection (opposes-aware).** Two entities are an opposed pair when
   `opposes_map.get(misc_key) == covered_key` — i.e. a CONTRADICTION on a
   misconception entity `misc_key` and a COVERED_NODE on the entity it opposes
   (`covered_key`). Build the conflict resolution per opposed pair BEFORE emitting
   the plain per-entity events, so a conflicting covered/contradiction does not
   ALSO emit its standalone event (the conflict row REPLACES both).
4. **Per opposed pair, decide by turn order** (each finding's turn position =
   `min(turn_order[nid] for nid in finding.student_node_ids)`, defaulting to a
   sentinel `+inf` when a node id is absent — documented: the EARLIEST evidence
   node anchors the finding's turn; `min` chosen so a multi-turn restated claim is
   anchored to when the student FIRST asserted it):
   - contradiction turn `<` covered turn → **`corrected`** on the opposed entity
     (covered_key). `misconception_code = misc_key`; `score` from §3 line 424
     semantics (the corrected event's L-vector lives in WU-5A; the event itself
     carries `score = covered finding's score or 1.0`, `confidence = covered
     finding's confidence`); `evidence_node_ids` = covered + contradiction node ids
     merged; `reference_step_id` = covered finding's reference node id.
   - covered turn `<` contradiction turn → **`misconception`** (last position wins).
     `misconception_code = misc_key`, `score = 0.0`, `evidence_node_ids` =
     contradiction node ids, `confidence` = the contradiction's confidence (coalesce
     None → 1.0; WU-4B1 already gated low-confidence misconceptions out).
   - turn positions EQUAL / both sentinel (order ambiguous) → **`partial`** at low
     confidence (`confidence = AMBIGUOUS_ORDER_CONFIDENCE = 0.5`, a named module
     constant), `diagnostic_flags = ("mixed-understanding",)`, `score` = a low
     partial (`0.5`), `evidence_node_ids` = both findings' node ids merged.
   - The opposed entity keys (BOTH `misc_key` and `covered_key`) are now CONSUMED —
     their standalone findings are skipped in step 5.
5. **Per remaining entity, emit the standalone event** by the first matching row:
   - **CONTRADICTION present** → `misconception` (s=0.0, `misconception_code =
     canonical_key`, `evidence_node_ids = student_node_ids`, confidence coalesced).
   - **MISSING_NODE present, NOT audit-upgraded** → `missing` (s=0.0,
     `reference_step_id` = the missing finding's reference node id; the audit was
     negative — a `missing` event REQUIRES a negative audit, satisfied because
     WU-4B1 already REWROTE audit-positive missings to COVERED_NODE upstream).
   - **audit-upgraded covered (marker)** → `covered` at the audit confidence
     (`<= 0.75`), `score = finding.score or finding.confidence` (a covered audit
     upgrade carries the capped confidence; `score` defaults to that confidence so
     the §3 `covered, s∈[0,1]` row lands mid-band → "shaky", never a false 1.0),
     `evidence_node_ids = student_node_ids`, `reference_step_id` = canonical_key.
     (The 4B1 spec note "partial OR covered" — v1 emits `covered` at low confidence;
     the `partial` split is deferred with the same calibration rationale as row 2.)
   - **plain COVERED_NODE** → `covered` (`score = finding.score or finding.confidence
     or 1.0` scaled by resolution confidence = the covered finding's `confidence`;
     `confidence = finding.confidence`, `evidence_node_ids = student_node_ids`,
     `reference_step_id = canonical_key`). When the entity ALSO has a missing-edge
     diagnostic available it would attach `diagnostic_flags=("edge-gap",)` — but in
     v1 the edge gap is NOT visible to this unit (edge findings carry no
     `canonical_key`, per `findings.py`), so the `edge-gap` flag is a documented
     no-op seam: row 2 stays `covered` and `PARTIAL_EDGE_GAP_ENABLED` stays False.
     This is asserted by the gate test.
6. **Suppression filter (last):** drop any produced event whose `event_kind.value`
   ∈ `audited_grade.suppressed_event_kinds`. `corrected` survives a `misconception`
   suppression (distinct value); `covered`/`partial` survive a `missing`
   suppression.
7. **Deterministic order:** sort the output by `(canonical_key, event_kind.value)`
   so identical inputs yield an equal tuple (mirrors `graph_compare` determinism).

Named module constants in `events.py`:
`PARTIAL_EDGE_GAP_ENABLED = False`, `AMBIGUOUS_ORDER_CONFIDENCE = 0.5`,
`AMBIGUOUS_ORDER_SCORE = 0.5`, `MIXED_UNDERSTANDING_FLAG = "mixed-understanding"`,
`EDGE_GAP_FLAG = "edge-gap"`, `DEFAULT_COVERED_SCORE = 1.0`.

### EDIT `apollo/grading/__init__.py` (extend re-exports — backward-compatible)
Add to imports + `__all__` (KEEP all existing names — backward-compat is required):
`convert_findings_to_events`, `LearnerEvent`, `LearnerEventKind`,
`EVENT_CONVERSION_VERSION`, `build_opposes_map`, `PARTIAL_EDGE_GAP_ENABLED`.
The existing 12 `__all__` entries stay untouched.

### EDIT `docs/architecture/apollo.md` (owner-doc reconcile)
- Extend the `apollo/grading/` module-map row (line 38): add
  `events.py`, `event_model.py`, `opposes.py` to the file list and append a
  **WU-4B2** clause describing `convert_findings_to_events`, the §6.5 table,
  `LearnerEvent`/`LearnerEventKind`, `build_opposes_map`, the injected
  `turn_order` seam, the `PARTIAL_EDGE_GAP_ENABLED=False` calibration gate, the
  abstention/suppression honoring, and the out-of-scope boundary (persistence =
  WU-5A; turn_order SOURCE query = WU-4C).
- Confirm `last_verified: 2026-06-17` (already set — re-affirm in the same edit;
  if a reviewer needs the literal change, no-op confirmation noted in commit body).

### CREATE test files (see §4) under `apollo/grading/tests/`.

**File-size check:** all four source files well under the 800-line limit;
`events.py` (~220) is the largest. Immutable/frozen throughout.

---

## 3. Public signatures (LOCKED — backward-compat preserved)

```python
# event_model.py
EVENT_CONVERSION_VERSION: str
class LearnerEventKind(StrEnum): COVERED, MISSING, PARTIAL, MISCONCEPTION, CORRECTED
@dataclass(frozen=True) class LearnerEvent: canonical_key, event_kind, score,
    confidence, misconception_code, evidence_node_ids, reference_step_id, diagnostic_flags

# opposes.py
def build_opposes_map(candidates: tuple[Candidate, ...]) -> Mapping[str, str]

# events.py
PARTIAL_EDGE_GAP_ENABLED: bool  # = False
def convert_findings_to_events(
    audited_grade: AuditedGrade, *,
    opposes_map: Mapping[str, str],
    turn_order: Mapping[str, int],
) -> tuple[LearnerEvent, ...]
```

No existing public signature changes — `__init__.py` only GAINS names. WU-4B1's
`build_audited_grade`/`AuditedGrade`/`apply_abstention`/etc remain exported
unchanged.

---

## 4. Test plan (TDD — write tests FIRST, RED before GREEN)

Two new test files + builder extensions. NO skips, NO xfail, NO tautologies. All
pure: zero DB/LLM/Neo4j/network. Construct `AuditedGrade` either via the real
WU-4B1 `build_audited_grade` (for the marker-keying row, to PROVE the marker is
real) or directly as a frozen literal (for the pure table rows). Extend
`_builders.py` with: `audited(findings, *, abstained=False, suppressed=())` →
frozen `AuditedGrade` literal; `covered_finding_with_nodes(key, nids, conf)`;
`misc_candidate(key, opposes)`; `turn_order_of(...)` dict helper.

### File `apollo/grading/tests/test_event_model.py`

| Test | Asserts | Mocking |
|---|---|---|
| `test_learner_event_kind_matches_mastery_event_kinds` | `{k.value for k in LearnerEventKind} == set(MASTERY_EVENT_KINDS)` (mirrors `test_finding_kind_unchanged`). The value-set can never drift from §2. | none (pure import) |
| `test_learner_event_is_frozen` | mutating a `LearnerEvent` field raises `FrozenInstanceError`; defaults are empty/None per shape. | none |
| `test_event_conversion_version_constant` | `EVENT_CONVERSION_VERSION == "finding-to-event-v1"` (single source of truth; pins the version string). | none |

### File `apollo/grading/tests/test_opposes.py`

| Test | Asserts | Mocking |
|---|---|---|
| `test_build_opposes_map_maps_misconception_to_opposed` | a misconception `Candidate(opposes_key="eq.bernoulli")` → `{misc_key: "eq.bernoulli"}`. | `_builders.misc_candidate` |
| `test_build_opposes_map_skips_non_misconception_and_none_opposes` | a non-misconception candidate and a misconception with `opposes_key=None` contribute nothing → empty/partial dict. | builders |
| `test_build_opposes_map_is_immutable_mapping` | the returned mapping equals expected dict; built from a tuple of candidates with no mutation of inputs. | builders |

### File `apollo/grading/tests/test_events.py` (the §6.5 decision table — ONE discriminating test per binding row)

All construct an explicit `turn_order` + `opposes_map` where relevant. `audited()`
builds the `AuditedGrade`. Default `abstained=False`, `suppressed=()` unless the
row tests them.

**Row-by-row (the §6.5 table):**

| Test | Row | Asserts | Construction |
|---|---|---|---|
| `test_covered_node_with_edges_emits_covered` | row 1 | one `covered` event, `score == covered.confidence` (scaled by resolution confidence), `reference_step_id == key`, `evidence_node_ids == student_node_ids`. | plain `covered_finding(key, confidence=0.98)`; `turn_order={}` (irrelevant). |
| `test_covered_node_with_edge_missing_stays_covered_not_partial` | row 2 | the event is `covered`, NOT `partial`; AND `PARTIAL_EDGE_GAP_ENABLED is False` (assert the gate constant explicitly so flipping it is a test failure). | covered finding + a `missing_edge` diagnostic finding (no canonical_key) in the same grade; prove the edge gap does NOT downgrade. |
| `test_missing_node_audit_negative_emits_missing_s0` | row 3 | one `missing` event, `score == 0.0`, `reference_step_id` = the missing finding's reference node id, NO covered event. | `missing_finding(key)` (a genuine negative-audit missing — WU-4B1 left it as MISSING_NODE because the audit found nothing). |
| `test_missing_node_audit_span_emits_covered_keyed_on_marker` | row 4 | the audit-upgraded finding (built by REAL `build_audited_grade` with a `found_audit_fn`) yields a `covered` (NOT `missing`) event at `confidence <= 0.75`; AND a control: a genuine `llm`-tier `covered_finding(key, confidence=0.75)` WITHOUT the marker is still `covered` but the discriminator is that a `MISSING_NODE`-origin finding only becomes covered via the marker — assert the path keys on `AUDIT_UPGRADE_MESSAGE`, not confidence (swap the message to something else and the finding would route as plain covered). | build via WU-4B1 `build_audited_grade(grade_with_missing, transcript=..., resolution=resolution_with(...), student_nodes=..., candidates=..., audit_fn=found_audit_fn({key: "span"}))` — proves marker-keying end-to-end with NO live LLM. |
| `test_contradiction_emits_misconception_s0` | row 5 | one `misconception` event, `score == 0.0`, `misconception_code == key`, `evidence_node_ids == student_node_ids`. | `contradiction_finding(misc_key, student_node_ids=("n1",))`. |
| `test_conflict_contradiction_earlier_covered_later_emits_corrected` | row 6 | ONE `corrected` event on the OPPOSED entity (covered_key), `misconception_code == misc_key`, NO standalone misconception/covered for the pair. | contradiction on `misc.x` nodes `("c1",)` + covered on `eq.y` nodes `("v1",)`; `opposes_map={"misc.x":"eq.y"}`; `turn_order={"c1":1,"v1":2}`. |
| `test_conflict_covered_earlier_contradiction_later_emits_misconception` | row 7 | ONE `misconception` event (last position wins), `misconception_code == misc_key`, NO corrected. | SAME findings as above; `turn_order={"v1":1,"c1":2}` — proves SWAPPING turn order FLIPS the event (last-position-wins discriminates). |
| `test_conflict_ambiguous_order_emits_partial_with_mixed_flag` | row 8 | ONE `partial` event, `confidence == AMBIGUOUS_ORDER_CONFIDENCE (0.5)`, `diagnostic_flags == ("mixed-understanding",)`. | same findings; `turn_order={"c1":1,"v1":1}` (equal) → ambiguous. |
| `test_unsupported_extra_emits_no_event` | row 9 | `convert_findings_to_events(...) == ()` for a grade whose only finding is `unsupported_extra`. | `Finding(kind=UNSUPPORTED_EXTRA, canonical_key="x", student_node_ids=("s1",))`. |
| `test_unresolved_emits_no_event` | row 10 | `() ` for an `unresolved`-only grade (counts toward abstention, already consumed by 4B1). | `Finding(kind=UNRESOLVED, student_node_ids=("u1",))`. |
| `test_edge_and_alternative_path_findings_emit_no_event` | rows 9/10 extension | matched_edge / missing_edge / alternative_path findings produce no events (diagnostic-only, like unsupported/unresolved). | findings of those three kinds → `()`. |

**Abstention + suppression (the §6.6 honoring 4B2 owns):**

| Test | Asserts | Construction |
|---|---|---|
| `test_abstained_grade_returns_empty` | `audited(..., abstained=True)` → `()` regardless of findings (the unresolved_rate gate = diagnostic-only run; NO learner update at all). | a grade with a covered + a missing finding; `abstained=True`. |
| `test_suppressed_missing_drops_missing_keeps_covered` | with `suppressed=frozenset({"missing"})` a `missing` event is DROPPED but a `covered` event survives. | grade with one covered + one missing finding. |
| `test_suppressed_missing_keeps_corrected_and_covered` | a `missing` suppression does NOT drop a `corrected` event (distinct kind). | conflict→corrected setup + a missing finding; assert corrected survives, missing dropped. |
| `test_suppressed_misconception_drops_misconception_not_corrected` | with `suppressed=frozenset({"misconception"})` the standalone `misconception` event is dropped, but a `corrected` event SURVIVES ('corrected' is NOT 'misconception'). | one standalone contradiction (→misconception, dropped) + one conflict pair resolving to corrected (survives). |
| `test_suppression_applies_after_conflict_resolution` | a misconception suppression drops a conflict-row `misconception` (covered-earlier-contradiction-later) — the last-position-wins event is a `misconception` and IS subject to suppression. | conflict pair, `turn_order` covered-first; `suppressed={"misconception"}` → `()`. |

**Determinism + purity:**

| Test | Asserts |
|---|---|
| `test_output_is_deterministically_ordered` | two findings on keys `b`,`a` both covered → events sorted `(a, b)`; calling twice yields equal tuples. |
| `test_inputs_are_not_mutated` | the input `AuditedGrade.findings` tuple, `opposes_map`, and `turn_order` are unchanged after the call (identity + equality check). |
| `test_turn_position_uses_min_over_student_node_ids` | a finding spanning nodes `("n2","n1")` with `turn_order={"n1":1,"n2":5}` anchors to turn 1 (earliest assertion) — drives a conflict decision that would flip if `max` were used. |
| `test_missing_turn_order_node_treated_as_ambiguous_sentinel` | a conflict where a node id is absent from `turn_order` → ambiguous → `partial`+mixed flag (defensive sentinel path, exercises the `+inf` default branch). |

**Package seam (extend existing `test_package_seam.py`):**

| Test | Asserts |
|---|---|
| `test_public_api_exports` (EXTEND) | the new names are in `grading.__all__` and importable: `convert_findings_to_events`, `LearnerEvent`, `LearnerEventKind`, `EVENT_CONVERSION_VERSION`, `build_opposes_map`, `PARTIAL_EDGE_GAP_ENABLED`. Existing 12 still present (backward-compat). |
| `test_learner_event_kind_value_set_parity` (in test_event_model, cross-ref) | `LearnerEventKind` value-set == `models.MASTERY_EVENT_KINDS` (the §2 parity lock). |

**Coverage target:** 95% patch (diff-cover vs
`feat/apollo-kg-wu4b1-transcript-audit-abstention`) AND 100% branch coverage on
the `events.py` decision table. Every table row + every abstention/suppression
branch + the `min`/sentinel turn-position branches + the marker-vs-confidence
discriminator is hit by a named test above. No row relies on a default-coalesce
that lacks a test.

Run:
```bash
pytest apollo/grading/tests/ -v --tb=short \
  --cov=apollo.grading.events --cov=apollo.grading.event_model \
  --cov=apollo.grading.opposes --cov-branch --cov-report=xml
diff-cover coverage.xml \
  --compare-branch=feat/apollo-kg-wu4b1-transcript-audit-abstention --fail-under=95
```

---

## 5. Owner-doc update (drift contract)

`docs/architecture/apollo.md`, the `apollo/grading/` row (line 38). Append after
the WU-4B1 clause:

> **WU-4B2 ships §6.4 step 16 (finding→event, §6.5 decision table).**
> `event_model.py`: the frozen `LearnerEvent` (maps onto `apollo_mastery_events`
> columns — `canonical_key`/`event_kind`/`score`/`confidence`/`misconception_code`/
> `evidence_node_ids`/`reference_step_id`; parser/grader/belief columns are WU-5A's
> at the belief update, NOT here) + `LearnerEventKind` StrEnum (value-set ==
> `models.MASTERY_EVENT_KINDS`, asserted) + `EVENT_CONVERSION_VERSION`.
> `opposes.py`: `build_opposes_map(candidates) -> {misc_key: opposed_key}` from each
> misconception `Candidate.opposes_key` (§6.5: opposes-links make the conflict rows
> detectable). `events.py`: `convert_findings_to_events(audited_grade, *,
> opposes_map, turn_order) -> tuple[LearnerEvent, ...]` applies the BINDING §6.5
> table — covered(+edges)→covered scaled by resolution confidence; covered+edge-
> missing stays `covered` (the `partial` variant is calibration-gated OFF behind
> `PARTIAL_EDGE_GAP_ENABLED=False` — an edge gap must never halve a score, §6.2);
> missing_node+audit-negative→`missing` s=0.0; the audit-upgrade row keys on the
> `AUDIT_UPGRADE_MESSAGE` marker (NOT kind/confidence — a genuine llm-tier covered
> also sits at 0.75)→`covered` ≤0.75; contradiction→`misconception` s=0.0;
> CONFLICT rows use `opposes_map` + the INJECTED `turn_order` (node_id→turn
> position; supplied by WU-4C from `apollo_messages.turn_index`/Neo4j `created_at`,
> by fixtures in tests — 4B2 stays PURE over it, never queries Postgres/Neo4j):
> contradiction-earlier+covered-later→`corrected`; covered-earlier+contradiction-
> later→`misconception` (last position wins); ambiguous order→`partial` +
> `mixed-understanding` flag. `unsupported_extra`/`unresolved`/edge/alternative_path
> →NO event. Honors WU-4B1's abstention: `abstained` →`()` (no update at all);
> else drops events whose kind ∈ `suppressed_event_kinds` (`corrected` survives a
> `misconception` suppression — distinct kind). Produces in-memory events ONLY —
> PERSISTENCE (atomic write with the 3-state Bayesian belief update) is **WU-5A**;
> the `turn_order` SOURCE query + Done wiring + §6.7 shadow/§6.8 diagnostics are
> **WU-4C**; runs/findings persistence is **WU-4B3**.

Keep `last_verified: 2026-06-17` (already current).

---

## 6. Risks & mitigations

| # | Risk | Mitigation |
|---|---|---|
| 1 | **Marker collision** — a genuine `llm`-tier covered node and an audit-upgrade both sit at `confidence=0.75`. Keying the audit row on confidence would misroute. | Key the audit-upgrade row on `finding.message == AUDIT_UPGRADE_MESSAGE` (imported from `audited_grade`). `test_missing_node_audit_span_emits_covered_keyed_on_marker` builds the upgrade via REAL `build_audited_grade` and proves marker-keying. |
| 2 | **Turn-order ambiguity → wrong event.** Picking `min` vs `max` over `student_node_ids` changes conflict outcomes; a missing node id could KeyError. | Locked: per-finding turn = `min(turn_order[nid] ...)` (anchor to first assertion), absent id → `+inf` sentinel → ambiguous (`partial`). Two tests (`min`-discriminator, missing-sentinel) lock both. |
| 3 | **Conflict double-emit** — a covered/contradiction pair could emit BOTH the conflict event AND its standalone events. | Conflict resolution CONSUMES both entity keys before standalone emission (step 3→5). `test_conflict_*` assert exactly ONE event for the pair. |
| 4 | **Suppression vs conflict ordering** — applying suppression before conflict resolution could wrongly drop a `corrected`. | Suppression is the LAST step (step 6), filtering FINAL event kinds. `corrected` ≠ `misconception` so it survives. `test_suppressed_misconception_drops_misconception_not_corrected` locks it. |
| 5 | **Edge-gap flag is a no-op seam in v1** — edge findings carry no `canonical_key`, so this unit cannot attach `edge-gap` to the right entity. | Documented: `PARTIAL_EDGE_GAP_ENABLED=False`, `edge-gap` is a declared-but-dormant flag; row 2 stays `covered`. `test_covered_node_with_edge_missing_stays_covered_not_partial` asserts the gate constant is False. |
| 6 | **Value-set drift** between `LearnerEventKind` and `MASTERY_EVENT_KINDS`. | Parity test (mirrors `test_finding_kind_unchanged`). |
| 7 | **Backward-compat break** in `__init__.py`. | Only ADD to `__all__`; `test_public_api_exports` keeps asserting the existing 12 names. |
| 8 | **Branch-coverage gap** on a defensive coalesce. | 100% branch coverage required on `events.py`; every `or`/sentinel/`if` has a dedicated test. No untested defensive branch ships. |

---

## 7. Out-of-scope (explicit boundaries for WU-4B2)

- **Event PERSISTENCE** — writing `apollo_mastery_events` atomically with the
  belief update is **WU-5A**. 4B2 only PRODUCES in-memory events.
- **The 3-state Bayesian belief update** (likelihood vectors §3, decay, damper) —
  **WU-5A**. 4B2 does not compute `prior_belief`/`posterior_belief`/`mastery_after`/
  `parser_confidence`/`grader_confidence`.
- **Runs/findings persistence** + `abstention_reasons`/`abstained` DB writes —
  **WU-4B3**.
- **`done.py` wiring + the `turn_order` SOURCE query** (apollo_messages.turn_index
  / Neo4j created_at) — **WU-4C**. 4B2 takes `turn_order` as an injected keyword.
- **§6.7 shadow / §6.8 diagnostics** — **WU-4C**. 4B2 only carries the
  diagnostic flags as data.
- **NO migration**; **NO modification** of `graph_compare` (frozen WU-4A) or the
  WU-4B1 modules `transcript_audit.py`/`abstention.py`/`audited_grade.py`
  (frozen). 4B2 IMPORTS `AUDIT_UPGRADE_MESSAGE` + `AuditedGrade` from
  `audited_grade`, `Candidate` from `resolution.candidates`, `Finding`/`FindingKind`
  from `graph_compare.findings`, `MASTERY_EVENT_KINDS` from `persistence.models` —
  read-only.

---

## 8. TDD execution order (RED → GREEN → REFACTOR)

1. Extend `_builders.py` (`audited()`, `misc_candidate()`, node/turn helpers).
2. Write `test_event_model.py` → RED (no `event_model.py`).
3. Create `event_model.py` → GREEN. Confirm parity test passes.
4. Write `test_opposes.py` → RED. Create `opposes.py` → GREEN.
5. Write `test_events.py` row tests (covered, missing, contradiction) → RED.
   Create `events.py` with the standalone-emission path → GREEN those rows.
6. Add the audit-marker row test → RED. Add marker-keying → GREEN.
7. Add the conflict + turn-order tests → RED. Add conflict resolution + the
   `min`/sentinel turn logic → GREEN. Prove last-position-wins flips on swap.
8. Add abstention/suppression tests → RED. Add the short-circuit + filter → GREEN.
9. Add determinism/purity tests → GREEN (sort + no-mutation already in place).
10. Extend `test_package_seam.py`; wire `__init__.py` re-exports → GREEN.
11. Reconcile `docs/architecture/apollo.md` (grading row).
12. Run pytest + diff-cover (≥95% patch, 100% branch on `events.py`). REFACTOR for
    clarity while green. Confirm files <800 lines, all frozen/immutable.
