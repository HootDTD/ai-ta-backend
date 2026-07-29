# WU-4B3 — Runs+Findings Postgres Persistence (supersede) + normalization_confidence + reference_hash + §6.11 corpus

**Branch:** `feat/apollo-kg-wu4b3-runs-findings-persistence` (already checked out — do NOT branch/switch/push/PR)
**Base for diff-cover:** `feat/apollo-kg-wu4b2-finding-to-event`
**Spec:** `docs/superpowers/specs/2026-06-10-apollo-kg-learner-model-architecture-decision.md`
(§2 lines 278-320 schema + supersede; §6.4 step 15 persist-always; §3 line 431 grader_confidence; §6.9 Bernoulli capstone; §6.11 lines 897-938 corpus)
**Closes:** WU-4B (the persistence half of the §6 grading orchestration).

This unit is the PERSISTENCE seam + the §6.11 executable corpus, layered on the
already-frozen `apollo/grading/` (WU-4B1 audit/abstention, WU-4B2 finding→event)
and `apollo/retired graph comparator/` (WU-4A score core). **No new migration** — migration
026 already shipped `apollo_graph_comparison_runs`/`_findings` and the ORM
(`GraphComparisonRun` ~models.py:479, `GraphComparisonFinding` ~models.py:527).
`next_free` migration stays 028.

---

## 0. Recon summary (verified against real code)

| Fact | Source |
|---|---|
| ORM `GraphComparisonRun` / `GraphComparisonFinding` already exist | `apollo/persistence/models.py:479-556` |
| `GradeResult` has the 10 `*_score` fields named 1:1 to runs columns + `comparison_version` | `apollo/retired graph comparator/core.py:52-76` |
| `AuditedGrade` carries `grade`, `findings`, `abstention_reasons`, `abstained`, `suppressed_event_kinds`, `alias_candidates` | `apollo/grading/audited_grade.py:59-74` |
| `Finding{kind, canonical_key, student_node_ids, reference_node_ids, evidence_spans, score, confidence, message}` (NO edge-id tuples — edges diagnostic, message-only) | `apollo/retired graph comparator/findings.py:40-58` |
| `ReferenceGraph{nodes, edges, paths}` of frozen `CanonicalNode`/`CanonicalEdge`/`ReferencePathView` | `apollo/retired graph comparator/canonical.py:79-93` |
| Per-node method-cap confidence on `ResolvedNode.confidence`; caps `METHOD_CONFIDENCE_CAP` (exact 1.0 … llm 0.75 … unresolved 0.0) | `apollo/resolution/result.py:33-39`, `apollo/resolution/candidates.py:35-42` |
| `db_session` fixture = real pgvector pg16, `Base.metadata.create_all`, per-test rollback; Apollo ORM imports the SAME `Base` from `database.models` so Apollo tables ARE created | `tests/conftest.py:119-192`, `apollo/persistence/models.py:33` |
| runs.`user_id` has **no ORM FK** (auth.users is Supabase-managed, not in `Base.metadata`) → a free-form UUID persists fine under `db_session`; `attempt_id`/`search_space_id` DO have ORM FKs → need real `ProblemAttempt`/`SearchSpace` rows | `apollo/persistence/models.py:493-500` |
| `ProblemAttempt` needs an `ApolloSession` (FK `session_id`); `SearchSpace` seeded via ORM (`database.models.SearchSpace`) | `apollo/persistence/models.py:265-285`, `tests/database/test_resolution_resolves_to_postgres.py:28` |
| Mirror pattern: PURE `*_to_row` spec dataclasses + thin async write seam | `apollo/knowledge_graph/resolution_store.py` |
| Shared pure test builders (`missing_grade`, `resolution_with`, `resolved_nodes`, `found_audit_fn`, …) | `apollo/grading/tests/_builders.py` |
| `findings.FindingKind.value` set == `models.FINDING_KINDS` (parity already asserted) | `apollo/grading/tests/test_package_seam.py:63-69` |

---

## 1. Public API (new — all additive, zero changes to frozen modules)

### `apollo/grading/normalization_confidence.py`
```python
def compute_normalization_confidence(
    audited_grade: AuditedGrade,
    resolution: ResolutionResult,
) -> float: ...
```
- **Definition (risk #2 resolved):** conservative **MIN (weakest-link)** over the
  per-node `ResolvedNode.confidence` of the resolved nodes that BACKED a *scored*
  finding — i.e. the evidence nodes of `covered_node` and `contradiction`
  findings (the §3 damper wants honest worst-case).
- Build `conf_by_node = {rn.node_id: rn.confidence for rn in resolution.resolved
  if rn.resolution == "resolved" and rn.confidence is not None}` (mirrors
  `audited_grade._misconception_confidences`).
- Collect the confidences of every node id appearing in a scored finding's
  `student_node_ids`. Return `min(...)`.
- **Empty-set rule (named calibration knob):** when no scored finding has a
  backing resolved node (e.g. a pure-missing or abstained-empty attempt), return
  `NORMALIZATION_CONFIDENCE_FLOOR_WHEN_NO_SCORED_NODES = 1.0` (no scored evidence
  ⇒ nothing to damp ⇒ neutral; NOT 0.0, which would falsely zero out the §3
  damper). Documented as a module constant with the rationale.
- **Scored-finding kinds knob:** `_SCORED_FINDING_KINDS = frozenset({COVERED_NODE,
  CONTRADICTION})` — a named constant so the "what backs a score" decision is
  one symbol, not scattered literals.
- Pure, immutable, no IO.

### `apollo/grading/reference_hash.py`
```python
def reference_graph_hash(reference_graph: ReferenceGraph) -> str: ...
```
- Deterministic **STABLE** hash over the `ReferenceGraph` AS GRADED. Build a
  sorted-canonical serialization (NOT `repr`, NOT `hash()` — both unstable across
  runs/PYTHONHASHSEED):
  - nodes: sorted list of `(canonical_key, str(node_type), symbolic or "")`
    (node identity is the canonical key + type + symbolic surface; evidence_spans
    are empty on R_norm and `source_node_ids` are reference step ids — exclude the
    step ids so renaming a step id without changing the graph shape keeps the hash
    stable; INCLUDE canonical_key which is the comparison identity).
  - edges: sorted list of `(str(edge_type), from_key, to_key)`.
  - paths: sorted list of each path's `canonical_keys` tuple.
- Serialize with `json.dumps(payload, sort_keys=True, separators=(",", ":"))`,
  then `hashlib.sha256(...).hexdigest()`. Prefix with a version tag
  `REFERENCE_HASH_VERSION = "refhash-v1"` → return `f"{REFERENCE_HASH_VERSION}:{digest}"`
  so a future serialization change is self-describing.
- **Stability** (same graph → same hash across replays) and **sensitivity**
  (any node/edge/path change → different hash) are the two binding properties
  (§2: old runs stay explainable; a teacher edit changes the hash).
- Pure, immutable, no IO.

### `apollo/grading/persistence.py`
```python
@dataclass(frozen=True)
class RunRowSpec:               # pure pre-DB value object (1:1 runs columns, NO id/created_at)
    attempt_id: int
    user_id: str
    search_space_id: int
    coverage_score: float
    soundness_score: float
    bisimilarity_score: float
    node_coverage_score: float
    edge_coverage_score: float
    scoping_score: float
    usage_score: float
    procedure_order_score: float
    dependency_score: float
    contradiction_score: float
    normalization_confidence: float
    abstained: bool
    abstention_reasons: tuple[str, ...]
    comparison_version: str
    reference_graph_hash: str

@dataclass(frozen=True)
class FindingRowSpec:           # pure pre-DB value object (1:1 findings columns, NO id/run_id/created_at)
    finding_kind: str
    entity_id: int | None       # None in v1 — see note below
    score: float | None
    confidence: float | None
    student_node_ids: tuple[str, ...]
    reference_node_ids: tuple[str, ...]
    student_edge_ids: tuple[str, ...]   # always () — edges are message-only
    reference_edge_ids: tuple[str, ...] # always ()
    evidence_spans: tuple[str, ...]
    message: str | None

def grade_to_run_spec(
    *, attempt_id: int, user_id: str, search_space_id: int,
    grade: GradeResult, audited: AuditedGrade,
    normalization_confidence: float, reference_graph_hash: str,
) -> RunRowSpec: ...

def finding_to_row_spec(finding: Finding) -> FindingRowSpec: ...

def findings_to_row_specs(findings: tuple[Finding, ...]) -> tuple[FindingRowSpec, ...]: ...

async def persist_comparison_run(
    db: AsyncSession, *, attempt_id: int, user_id: str, search_space_id: int,
    grade: GradeResult, audited: AuditedGrade,
    normalization_confidence: float, reference_graph_hash: str,
) -> int:   # returns the persisted run_id
    ...
```

**Mapping (`grade_to_run_spec`):**
- 10 `*_score` fields copied 1:1 from `grade` (the GradeResult is the score
  authority).
- `abstained` + `abstention_reasons` from `audited` (the AuditedGrade is the
  abstention authority — NOT recomputed).
- `comparison_version` from `grade.comparison_version`.
- `normalization_confidence` + `reference_graph_hash` passed in (computed by the
  two helper modules at the call site / convenience wrapper).
- **Source-of-findings note:** persist `audited.findings` (the audit-REWRITTEN
  set — an audit-upgraded missing→covered must persist as the covered it became,
  carrying the span + capped confidence + `AUDIT_UPGRADE_MESSAGE`). `grade.findings`
  is the PRE-audit set; persisting it would lose the audit upgrade. This is the
  one subtle correctness point — a dedicated test pins it.

**Mapping (`finding_to_row_spec`):**
- `finding_kind = finding.kind.value` (StrEnum → plain string column).
- `score`/`confidence`/`message` copied (nullable).
- `student_node_ids`/`reference_node_ids`/`evidence_spans` → list-ified for the
  `_JSONType` column (tuples → lists; JSONB stores arrays).
- `student_edge_ids`/`reference_edge_ids` → always `()` (Finding has no edge-id
  fields; edges ride in `message`, per the frozen `findings.py` docstring).
- `entity_id = None` in v1: `Finding` carries the `canonical_key` *string*, not
  the `apollo_kg_entities.id` surrogate; resolving key→id is a join WU-4B3 does
  NOT own (it would need the candidate set's `canon_key`, which is not on a
  Finding). Persist NULL and document it — `entity_id ON DELETE SET NULL` already
  tolerates NULL; the canonical_key survives in the message/ids for diagnostics.
  (Explicitly called out so it is a decision, not a silent gap.)

**`persist_comparison_run` (the async write seam) — SUPERSEDE in ONE transaction:**
1. Compute the `RunRowSpec` + `FindingRowSpec`s (pure).
2. `DELETE FROM apollo_graph_comparison_runs WHERE attempt_id = :aid AND
   comparison_version = :ver` — the prior run (if any) is removed; its findings
   CASCADE-drop (FK `ON DELETE CASCADE`). This is the §2 supersede: a legitimate
   retry must NEVER hit the `UNIQUE(attempt_id, comparison_version)` crash.
3. Insert the new `GraphComparisonRun` ORM row; `flush()` to get `run_id`.
4. Bulk-insert the `GraphComparisonFinding` rows with `run_id` set.
5. Do **NOT** `commit()` — the caller (WU-4C `done.py`) owns the transaction
   boundary (§6.4 step 15 commits run+findings in one txn; events+update are a
   SEPARATE txn in WU-5A). Under the test harness the `db_session` savepoint +
   rollback gives isolation; under prod the caller commits. The function flushes
   so the `run_id` is real and FK-valid within the open transaction.
   - Rationale doc-string: persistence is **delete-then-reinsert within the
     caller's transaction**; atomicity of supersede is guaranteed by both
     statements sharing one transaction (no autocommit between DELETE and INSERT).
- Persist **ALWAYS** — including abstained runs (`abstained=true`, findings still
  written). No early-return on abstention.
- Immutable: builds new spec objects, never mutates inputs.

### `apollo/grading/__init__.py` (extend re-exports — backward-compat)
Add to imports + `__all__` (the existing 18 names stay, additive only):
`persist_comparison_run`, `RunRowSpec`, `FindingRowSpec`, `grade_to_run_spec`,
`finding_to_row_spec`, `findings_to_row_specs`, `compute_normalization_confidence`,
`NORMALIZATION_CONFIDENCE_FLOOR_WHEN_NO_SCORED_NODES`, `reference_graph_hash`,
`REFERENCE_HASH_VERSION`.

---

## 2. Files to create / edit

| Path | Action | Notes |
|---|---|---|
| `apollo/grading/normalization_confidence.py` | CREATE | MIN-over-scored-findings + floor knob (<120 lines) |
| `apollo/grading/reference_hash.py` | CREATE | sorted-canonical sha256 + version tag (<90 lines) |
| `apollo/grading/persistence.py` | CREATE | specs + mapping + async supersede seam (<260 lines) |
| `apollo/grading/__init__.py` | EDIT | additive re-exports only |
| `apollo/grading/fixtures/__init__.py` | CREATE | package marker |
| `apollo/grading/fixtures/corpus.py` | CREATE | the 12 §6.11 fixtures as pure data + the §6.9 Bernoulli row (data only, no DB) |
| `apollo/grading/tests/test_normalization_confidence.py` | CREATE | pure (no container) |
| `apollo/grading/tests/test_reference_hash.py` | CREATE | pure |
| `apollo/grading/tests/test_persistence_specs.py` | CREATE | pure mapping (no container) |
| `apollo/grading/tests/test_package_seam_wu4b3.py` | CREATE | re-export parity |
| `apollo/grading/tests/test_corpus_e2e.py` | CREATE | §6.11 chain assertions, findings+events, NO container (deterministic resolution + injected audit_fn) |
| `tests/database/test_apollo_comparison_run_persistence.py` | CREATE | **real-PG gate** — round-trip + supersede + abstained-persist + FK |
| `tests/database/test_apollo_comparison_corpus_persistence.py` | CREATE | **real-PG gate** — the persistence-touching §6.11 fixtures + Bernoulli capstone persisted |
| `docs/architecture/apollo.md` | EDIT | owner-doc reconcile; bump `last_verified: 2026-06-18` |

All new files stay <800 lines; split `corpus.py` if it approaches the limit
(data tables are compact — one builder per fixture).

---

## 3. The §6.11 corpus design (`apollo/grading/fixtures/corpus.py`)

Each fixture is a frozen `CorpusFixture` value object carrying everything the
chain needs and everything to assert:
```python
@dataclass(frozen=True)
class CorpusFixture:
    name: str
    grade: GradeResult                      # the pre-audit score core output (built via _builders)
    resolution: ResolutionResult            # deterministic-tier resolution (no live resolver)
    student_nodes: tuple[Node, ...]         # for the parser-confidence abstention input
    candidates: tuple[Candidate, ...]       # closed candidate set for missing-entity lookup
    audit_fn: AuditFn                        # injected deterministic stub (found/notfound/raising)
    opposes_map: dict[str, str]             # for finding→event conflict detection
    turn_order: dict[str, int]              # injected node_id→turn position
    reference_graph: ReferenceGraph         # for the reference_graph_hash
    expected_finding_kinds: tuple[str, ...] # sorted multiset of audited finding kinds
    expected_event_kinds: tuple[str, ...]   # sorted multiset of produced event kinds
    persists: bool                          # True ⇒ exercised on REAL PG
```

The 12 §6.11 rows + the §6.9 Bernoulli capstone, each mapping its spec line to
asserted findings AND events. Built by composing the existing `_builders.py`
helpers (covered/missing/contradiction findings, `resolution_with`,
`found_audit_fn`/`notfound_audit_fn`/`raising_audit_fn`) so the corpus REUSES the
upstream unit fixtures that already encode a §6.11 row (4A2 scores, 4B1
audit/abstention, 4B2 events) rather than re-deriving them.

The chain each fixture runs (no live LLM, no Neo4j, no resolver call — resolution
is pre-built deterministic):
```
build_audited_grade(grade, transcript=..., resolution=..., student_nodes=...,
                    candidates=..., audit_fn=fixture.audit_fn)
  → convert_findings_to_events(audited, opposes_map=..., turn_order=...)
```
plus, for `persists=True` fixtures, `persist_comparison_run(db, ...)`.

Fixture → spec line mapping (assert kinds, not transcripts):

| Fixture (spec §6.11 / §6.9) | Asserts (findings) | Asserts (events) | persists |
|---|---|---|---|
| valid alternative path (energy, not Bernoulli) | covered via path B, zero false missing, `alternative_path` finding present | covered ×N, no missing | no |
| correct answer, thin explanation | low coverage, high soundness, no contradiction | covered (few), no misconception | no |
| wrong answer, mostly-correct concepts | covered + a `contradiction` on the final relation | covered + misconception | no |
| polar near-miss ("pressure increases with speed") | resolves to `misc.*` → contradiction (NOT covered on the lexically-close ref key) | misconception | no |
| conflict: misconception first, then correct | covered+contradiction opposed pair, contradiction earlier | `corrected` | no |
| conflict: correct first, then misconception | opposed pair, covered earlier | `misconception` (last position wins) | no |
| vague pronouns ("it increases there") | `unresolved` finding, no event-bearing finding | () (counts toward abstention) | no |
| nonstandard notation / paraphrase | covered at alias/symbolic tier | covered | no |
| **parser misses a key sentence** | audit upgrades missing→covered ≤0.75, NO false missing | covered ≤0.75 | **YES** |
| reference omits a valid stated assumption | `unsupported_extra`, zero soundness penalty | () | no |
| misconception not in `misc.*` | `unsupported_extra` (honest non-detection) | () | no |
| **high-unresolved-rate (>0.35)** | findings persisted, `abstained=True` | () (no Layer-3 update) | **YES** |
| **§6.9 Bernoulli capstone** | covered ×2 + partial (velocity) + missing (assumptions/solve) | covered ×2, partial, missing | **YES** |

(Event-kind assertions use the audited-then-converted result; the abstained
fixture asserts `events == ()` AND that the run row still persists.)

---

## 4. TDD-ordered test list (RED → GREEN, real tests first)

> Discipline: write each test, run it RED (target symbol missing / wrong), then
> implement the minimal code to GREEN. No `skip`, no `xfail`, no
> assert-nothing. The real-PG tests must RUN green, not skip (Docker is up).

### A. PURE mapping/compute tests (no container)

**`test_normalization_confidence.py`** (imports `_builders`):
1. `test_min_over_scored_finding_backers` — grade with two covered findings whose
   backing resolved nodes are at 0.92 and 0.75 → returns **0.75** (the
   weakest-link discriminator; THE binding assertion).
2. `test_single_high_confidence_node` — one covered, backer at 0.98 → 0.98.
3. `test_contradiction_node_counts_as_scored` — a contradiction whose evidence
   node is at 0.80 and a covered at 0.92 → 0.80 (contradiction backers ARE in the
   scored set).
4. `test_unsupported_extra_does_not_lower` — an `unsupported_extra` node at 0.10
   alongside a covered at 0.92 → 0.92 (extras are NOT scored ⇒ excluded).
5. `test_unresolved_node_excluded` — an `unresolved` finding (no resolved backer)
   does not pull the min to 0.0.
6. `test_no_scored_findings_returns_floor` — a pure-missing grade (no covered/
   contradiction backers) → `NORMALIZATION_CONFIDENCE_FLOOR_WHEN_NO_SCORED_NODES`
   (== 1.0); asserts the constant value too.
7. `test_pure_no_mutation` — inputs unchanged after the call (frozen-dataclass
   inputs; assert equality of a copy).
*Mocks:* none — pure function over `_builders` value objects.

**`test_reference_hash.py`**:
8. `test_same_graph_same_hash` — build a `ReferenceGraph` twice (independent
   construction), assert equal hashes (stability across replays). Run under a
   fresh subprocess-independent `PYTHONHASHSEED` is unnecessary because the impl
   uses `json.dumps(sort_keys=True)` not builtin `hash()` — but ALSO assert the
   hash is identical to a hard-coded golden digest to pin the serialization.
9. `test_edited_node_changes_hash` — add/rename one node's `canonical_key` → hash
   differs (sensitivity).
10. `test_edited_edge_changes_hash` — change one edge endpoint → hash differs.
11. `test_edited_path_changes_hash` — reorder/extend a `ReferencePathView` → hash
    differs.
12. `test_node_order_independence` — same nodes in a different tuple order → SAME
    hash (sorted-canonical, order-independent).
13. `test_hash_is_version_prefixed` — result starts with `REFERENCE_HASH_VERSION + ":"`.
*Mocks:* none.

**`test_persistence_specs.py`** (pure spec mapping, no DB):
14. `test_grade_to_run_spec_maps_all_ten_scores` — every `*_score` field copied
    1:1 from a `GradeResult` (assert each of the 10).
15. `test_run_spec_takes_abstention_from_audited` — `abstained` +
    `abstention_reasons` come from the `AuditedGrade`, not recomputed; a grade
    with abstained=True/reasons=(...) round-trips onto the spec.
16. `test_run_spec_carries_norm_conf_and_hash` — the two passed-in scalars land
    on the spec verbatim; `comparison_version` from `grade.comparison_version`.
17. `test_finding_to_row_spec_kind_is_string` — `FindingKind` → `.value` plain
    string; covers covered/missing/contradiction/unresolved/unsupported_extra/
    matched_edge/missing_edge/alternative_path (loop over all 8 kinds).
18. `test_finding_row_spec_jsonb_lists` — `student_node_ids`/`reference_node_ids`/
    `evidence_spans` tuples become lists; `student_edge_ids`/`reference_edge_ids`
    are always `[]`; `score`/`confidence`/`message`/`entity_id` nullable preserved.
19. `test_persist_uses_audited_findings_not_grade` — an audit-upgraded
    missing→covered finding is present in `findings_to_row_specs(audited.findings)`
    as `covered_node` carrying the span + `AUDIT_UPGRADE_MESSAGE`, while the
    pre-audit `grade.findings` still shows `missing_node` (pins the
    audited-not-grade source decision).
20. `test_specs_are_frozen` — `RunRowSpec`/`FindingRowSpec` reject attribute
    assignment (`dataclasses.FrozenInstanceError`).
*Mocks:* none — pure mapping over `_builders` objects.

**`test_package_seam_wu4b3.py`**:
21. `test_public_api_exports_wu4b3` — the new names are in `grading.__all__` and
    `hasattr(grading, name)`; the existing WU-4B1/4B2 names still present
    (backward-compat).
22. `test_finding_row_spec_kinds_cover_models_finding_kinds` — every value in
    `models.FINDING_KINDS` is producible as a `FindingRowSpec.finding_kind`
    (no kind silently unmappable).
*Mocks:* none.

**`test_corpus_e2e.py`** (the §6.11 chain — pure, deterministic, no container):
23. `test_corpus_findings_match_spec[fixture]` — parametrized over all 13 corpus
    fixtures: run `build_audited_grade` → assert the audited finding-kind multiset
    == `fixture.expected_finding_kinds`.
24. `test_corpus_events_match_spec[fixture]` — same parametrize: run
    `convert_findings_to_events` → assert the event-kind multiset ==
    `fixture.expected_event_kinds`.
25. `test_bernoulli_capstone_events` — the §6.9 row explicitly: covered ×2 +
    partial + missing (the named worked example, asserted on its own so a
    regression is legible).
26. `test_high_unresolved_abstains_no_events` — the abstention fixture →
    `audited.abstained is True` AND `convert_findings_to_events(...) == ()`.
27. `test_polar_near_miss_resolves_to_misc_not_reference` — asserts the
    contradiction is keyed on a `misc.*` key, never the lexically-close reference
    key (the §6.11 anti-false-positive row).
*Mocks:* deterministic `audit_fn` stubs (`found_audit_fn`/`notfound_audit_fn`/
`raising_audit_fn` from `_builders`), pre-built `ResolutionResult`s (no resolver
call), injected `turn_order`/`opposes_map`. NO live LLM, NO Neo4j, NO PG.

### B. REAL-PG round-trip tests — `tests/database/test_apollo_comparison_run_persistence.py`

> `pytestmark = pytest.mark.integration`. Uses the `db_session` fixture (real
> pgvector pg16, `Base.metadata.create_all`, per-test rollback). A
> `_seed_attempt(db)` helper creates the FK chain: `SearchSpace` (ORM) →
> `ApolloSession` → `ProblemAttempt`, returns `(attempt_id, search_space_id)`;
> `user_id` is a free-form UUID string (runs.user_id has no ORM FK under
> `create_all`). These tests MUST run green (not skip) with Docker up.

28. `test_round_trip_all_columns` — `persist_comparison_run(...)` then read the
    `GraphComparisonRun` back by id: assert all 10 scores, `normalization_confidence`,
    `abstained`, `abstention_reasons` (JSONB list round-trips), `comparison_version`,
    `reference_graph_hash`, the nullable sub-scores, `attempt_id`/`user_id`/
    `search_space_id`. Assert the findings rows persisted with correct
    `finding_kind`, JSONB id lists, `evidence_spans`, nullable `score`/`confidence`/
    `message`, and `student_edge_ids`/`reference_edge_ids == []`.
29. `test_abstention_reasons_jsonb_roundtrip` — persist a run whose
    `abstention_reasons` is a multi-element tuple → reads back as the same list
    (JSONB array fidelity).
30. `test_nullable_subscores_persist_null` — a finding/run with a NULL sub-score
    persists and reads back `None` (column nullability honored).
31. `test_supersede_deletes_prior_run_and_findings` — persist run A at
    `(attempt_id, "graph-compare-v1")` (capture `run_id_a` + finding count), then
    persist run B at the SAME pair → assert: only ONE run row exists for that
    pair, its id == `run_id_b != run_id_a`, run A's findings are gone (CASCADE),
    run B's findings present, NO `IntegrityError`/UNIQUE crash. **This is the §2
    supersede binding.**
32. `test_supersede_is_atomic_single_transaction` — assert the DELETE+INSERT
    happen without an intermediate commit: within one `db_session` the second
    `persist_comparison_run` leaves a consistent single-row state (no orphaned
    findings from run A — query `apollo_graph_comparison_findings` joined on the
    surviving run only).
33. `test_abstained_run_still_persists` — an `AuditedGrade` with `abstained=True`
    persists a run with `abstained=true` AND its findings (persist-ALWAYS, §6.4
    step 15). Assert the run row exists and findings count > 0.
34. `test_fk_integrity_real_rows` — `persist_comparison_run` with the seeded real
    `attempt_id`/`search_space_id` succeeds; a bogus `attempt_id` (no such row)
    raises `IntegrityError` on flush (FK enforced) — wrapped in a savepoint so the
    session stays usable / or asserted via `pytest.raises(IntegrityError)` on a
    fresh sub-transaction.
35. `test_returns_run_id` — the return value equals the persisted row's `id`
    (re-query confirms).
*Mocks:* none beyond the deterministic `_builders` inputs; the DB is real.

### C. REAL-PG §6.11 corpus persistence — `tests/database/test_apollo_comparison_corpus_persistence.py`

> Same harness/marker. Drives the persistence-touching corpus fixtures through
> the FULL chain then persists, asserting the persisted run + findings + the
> produced events.

36. `test_parser_miss_persists_upgraded_covered` — the "parser misses a key
    sentence" fixture: chain → persist → read back; assert a `covered_node`
    finding row at `confidence <= 0.75` carrying the span + `AUDIT_UPGRADE_MESSAGE`,
    NO `missing_node` row for that key, and `convert_findings_to_events` yields a
    covered ≤0.75 event (NO false missing).
37. `test_high_unresolved_persists_run_no_events` — the >0.35 abstention fixture:
    chain → persist → assert the run row persisted with `abstained=true` + the
    `unresolved_rate_above_threshold` reason, findings persisted, and
    `convert_findings_to_events(...) == ()` (run persisted, NO events — the §6.11
    binding for this row).
38. `test_bernoulli_capstone_persisted` — the §6.9 worked example: chain →
    persist → read back the run + all findings; assert covered ×2 / partial /
    missing findings are present in the persisted rows and the produced events
    match (covered ×2, partial, missing). The capstone proves the whole
    resolve→grade→audit→event→persist chain end-to-end on real PG.
*Mocks:* deterministic resolution + injected `audit_fn` (no live LLM); DB real.

**Coverage note:** every changed Python line in the three new `apollo/grading/`
modules is exercised by the pure tests (A) which run without Docker; the SQL
write seam's behavior is covered by the real-PG tests (B/C). diff-cover measures
the Python lines — the supersede DELETE, the spec mapping, the floor knob, the
hash serialization, and `persist_comparison_run`'s body are all hit. Target:
**≥95% patch coverage vs `feat/apollo-kg-wu4b2-finding-to-event`**.

---

## 5. Verification commands (run before claiming done)

```bash
# Pure + package tests
pytest apollo/grading/tests -v --tb=short
# Real-PG gate (Docker up; MUST be all-passed, real-PG GREEN-not-skipped)
pytest tests/database/test_apollo_comparison_run_persistence.py \
       tests/database/test_apollo_comparison_corpus_persistence.py -v --tb=short
# Full DB suite sanity (the gate the verifier runs)
pytest tests/database -v
# Patch coverage
pytest --cov=apollo.grading --cov-report=xml apollo/grading/tests tests/database
diff-cover coverage.xml --compare-branch=feat/apollo-kg-wu4b2-finding-to-event --fail-under=95
```
Confirm the real-PG tests show `PASSED` (not `SKIPPED`) in the output — a skip is
a FAIL of the gate.

---

## 6. Owner-doc update (`docs/architecture/apollo.md`)

Drift contract — same commit as the code. Set `last_verified: 2026-06-18`
(already 2026-06-18 in frontmatter; re-affirm). Extend the WU-4B grading API
listing (near lines 79/94, after the `GradeResult`/`Finding` entry) with:

- **`apollo.grading.persist_comparison_run(db, *, attempt_id, user_id,
  search_space_id, grade, audited, normalization_confidence, reference_graph_hash)
  → run_id`** (WU-4B3; §6.4 step 15 — persists the comparison run + one findings
  row per `audited.findings` Finding in ONE transaction; SUPERSEDE on a re-run at
  the same `(attempt_id, comparison_version)` (DELETE prior run → CASCADE its
  findings → reinsert; the UNIQUE never crashes a legit retry); persists ALWAYS,
  even on abstained runs (`abstained=true`); does NOT commit — the caller (WU-4C
  `done.py`) owns the transaction boundary). Pure mapping seams
  `grade_to_run_spec` / `finding_to_row_spec` / `findings_to_row_specs` +
  the frozen `RunRowSpec` / `FindingRowSpec` value objects (DB-free, 1:1 column
  mapping; `entity_id` is NULL in v1 — the canonical_key→entity id join is not a
  WU-4B3 concern; `student_edge_ids`/`reference_edge_ids` always `[]` — edges are
  message-only diagnostics).
- **`apollo.grading.compute_normalization_confidence(audited_grade, resolution)
  → float`** (WU-4B3; §3 damper input — the conservative MIN (weakest-link) over
  the per-node method-cap `ResolvedNode.confidence` of the resolved nodes backing
  a SCORED finding (covered/contradiction); returns
  `NORMALIZATION_CONFIDENCE_FLOOR_WHEN_NO_SCORED_NODES = 1.0` when no scored
  finding has a resolved backer. Named calibration knob; feeds
  `grader_confidence = normalization_confidence × comparison_confidence`, §3 line 431).
- **`apollo.grading.reference_graph_hash(reference_graph) → str`** (WU-4B3; a
  deterministic STABLE sorted-canonical sha256 over the `ReferenceGraph` AS
  GRADED — nodes (key+type+symbolic) + edges + path key-tuples, order-independent;
  version-prefixed `REFERENCE_HASH_VERSION = "refhash-v1"`. Stable across replays,
  changes when the teacher edits the reference so §2's "old runs stay
  explainable" holds).
- Note the **§6.11 executable corpus** (`apollo/grading/fixtures/corpus.py`): 12
  adversarial fixtures + the §6.9 Bernoulli capstone, each shipping expected
  findings AND expected events, driving
  `build_audited_grade → convert_findings_to_events` (deterministic resolution +
  injected `audit_fn`, no live LLM); the persistence-touching fixtures
  (parser-miss, high-unresolved, Bernoulli) run on REAL PG.
- Reconcile the §2 row: the runs/findings tables are now WRITTEN by WU-4B3 (no
  longer "nothing reads/writes these tables yet").

---

## 7. Risks

| # | Risk | Mitigation |
|---|---|---|
| 1 | `db_session` harness has no `auth.users` table, so a `user_id` FK would fail | runs.`user_id` has **no ORM FK** under `Base.metadata` (Supabase-managed) — a free-form UUID persists fine; verified at `models.py:493`. Do NOT add an `auth.users` stub to the `create_all` path. |
| 2 | normalization_confidence semantics ambiguity (mean vs min; which findings count) | RESOLVED: MIN (weakest-link) over covered+contradiction backers; floor=1.0 when none. Two discriminator tests (0.92+0.75→0.75; extras excluded). Documented as a named knob. |
| 3 | `reference_graph_hash` instability from builtin `hash()` / `repr` / dict order | Use `json.dumps(sort_keys=True)` + sha256 over a sorted-canonical payload; golden-digest test pins it; order-independence test guards dict/tuple ordering. |
| 4 | Supersede crash on legit retry if the UNIQUE fires | DELETE-then-insert in ONE transaction (no commit between); explicit `test_supersede_deletes_prior_run_and_findings` asserts no `IntegrityError`. |
| 5 | Persisting `grade.findings` instead of `audited.findings` would lose the audit upgrade | `persist_comparison_run` reads `audited.findings`; `test_persist_uses_audited_findings_not_grade` + `test_parser_miss_persists_upgraded_covered` pin it. |
| 6 | JSONB tuple↔list round-trip fidelity (`_JSONType` = JSONB on PG) | Map tuples→lists in the spec; `test_abstention_reasons_jsonb_roundtrip` + round-trip test assert array fidelity on real PG. |
| 7 | `entity_id` left NULL could look like a gap | Explicit decision (canonical_key→id join is not WU-4B3's; `ON DELETE SET NULL` tolerates NULL); documented in code + owner doc. |
| 8 | Real-PG tests skip (gate fails as skip) instead of run | Docker is up (pgvector pg16 confirmed); no `@skip`/`xfail`; verifier asserts PASSED-not-SKIPPED. |
| 9 | diff-cover base branch | `feat/apollo-kg-wu4b2-finding-to-event` exists (verified `git rev-parse`). |

---

## 8. Out of scope (DO NOT build in this unit)

- `apollo_mastery_events` / `apollo_learner_state` writes — **WU-5A** (events are
  PRODUCED by 4B2, PERSISTED atomically with the belief update by 5A).
- The 3-state Bayesian belief update / §3 update math — **WU-5A**.
- `done.py` wiring + the `turn_order` SOURCE query (from `apollo_messages`) — **WU-4C**
  (the corpus INJECTS `turn_order`; it does not query it).
- §6.7 shadow / §6.8 diagnostics — **WU-4C**.
- A new migration (026 already shipped the tables + ORM; `next_free` stays 028).
- Any change to `apollo/retired graph comparator/**` (frozen, WU-4A) or the WU-4B1/4B2
  modules `abstention.py`/`audited_grade.py`/`transcript_audit.py`/`events.py`/
  `event_model.py`/`opposes.py` (frozen). `__init__.py` is touched for ADDITIVE
  re-exports only.
- HTTP registration of any error in `apollo/api.py` — **WU-4C**.
- `compute_normalization_confidence` is NOT called from `persist_comparison_run`'s
  required signature (it is passed `normalization_confidence: float`); the helper
  is exported for WU-4C to call at the `done.py` call site. (A convenience that
  wires it is fine but NOT required — keep the persist seam taking the scalar so
  it stays pure-mappable.)
