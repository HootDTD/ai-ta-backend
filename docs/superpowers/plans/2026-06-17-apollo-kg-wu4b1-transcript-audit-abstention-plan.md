# WU-4B1 — Transcript audit (§6.4 step 12) + abstention gates (§6.6) → `AuditedGrade`

**Status:** plan (TDD-ordered, implementation not yet started)
**Branch:** `feat/apollo-kg-wu4b1-transcript-audit-abstention` (already checked out — do NOT branch/push/PR)
**Spec:** `docs/superpowers/specs/2026-06-10-apollo-kg-learner-model-architecture-decision.md` §3 (damper/caps), §6.3 (`transcript_audit.py`), §6.4 steps 12 & 14, §6.5 (CONTEXT ONLY — WU-4B2), §6.6 (abstention gates)
**Owner doc:** `docs/architecture/apollo.md` (owns `apollo/**` + `apollo/graph_compare/**`; this unit registers `apollo/grading/**`)
**Patch-coverage gate:** `diff-cover coverage.xml --compare-branch=feat/apollo-kg-wu4a2-simulation-scores --fail-under=95`

---

## 1. Scope & boundaries

### 1.1 What this unit IS
The downstream **grading orchestration layer** that imports the pure, IO-free `apollo/graph_compare/` score core and turns its `GradeResult` into an `AuditedGrade`:

1. **Transcript audit (§6.4 step 12 / §6.3 `transcript_audit.py`):** ONE batched Done-time LLM call over the simulator-flagged `missing_node` reference entities + the raw transcript. Each entity comes back with a supporting span or null. A found span upgrades the `missing_node` finding to a `covered`/`partial`-grade finding at confidence `≤ 0.75` and emits an `AliasCandidate`. A null leaves the `missing_node` finding intact. Any audit-infrastructure failure (timeout / error / JSON-parse failure / empty payload when entities were asked) raises `TranscriptAuditUnavailableError` — **never** "skip audit and emit the missing finding".
2. **Abstention gates (§6.6):** compute the abstention reason list + `abstained` flag + a per-event-kind **suppression set** (the kinds WU-4B2 must withhold) from `unresolved_rate`, `min` parser-confidence, misconception resolution confidence, the upstream reference-validation failure, and the transcript-audit failure.
3. **Handoff assembly:** `build_audited_grade(...)` runs the audit, applies the gates, rewrites the findings tuple with the audit upgrades, and returns one frozen `AuditedGrade`.

### 1.2 Package placement (RECON-verified)
**Create a NEW package `apollo/grading/`.** Do **NOT** extend `apollo/graph_compare/` — its `__init__` docstring asserts it is the PURE, IO-free score core (persists nothing; no Neo4j / Postgres / LLM). `grading` is the orchestration layer that *imports* `graph_compare`, exactly mirroring the split already in the codebase:

| Pure / IO-free core | Downstream orchestration that imports it |
|---|---|
| `apollo/resolution/` (matching only) | `apollo/knowledge_graph/resolution_store.py` (Neo4j writes) |
| `apollo/graph_compare/` (`grade_attempt` → `GradeResult`) | **`apollo/grading/` (this unit — audit + abstention)** |

Many small frozen-dataclass modules; immutable style (return new objects, never mutate the frozen inputs); each file < 800 lines.

### 1.3 OUT OF SCOPE (do NOT build here)
- **§6.5 finding→event conversion** — WU-4B2. This unit emits the *suppression set* (which event kinds to withhold) and the audit-rewritten findings; it does **not** produce events. (§6.5 read for CONTEXT ONLY.)
- **ALL Postgres persistence** — runs/findings writes, `abstention_reasons`/`abstained` column writes — WU-4B3. The `apollo_graph_comparison_runs` table already carries `abstained` (NOT NULL default false) and `abstention_reasons` (NOT NULL default `[]`) from migration 026; this unit only *produces* the `tuple[str, ...]` + bool that WU-4B3 will persist.
- **Event production / learner-model (§3) update** — WU-4B2 / WU-4B3.
- **The §8 alias-candidate teacher-approval QUEUE table** — emit `AliasCandidate` **value objects only**; the queue/persistence target is §8 / WU-3B2. Flag as a downstream follow-up; do NOT invent a queue table.
- **The DB read of the transcript from `apollo_messages`** — WU-4C threads it. Here the transcript is passed in as text.
- **NO migration** (026 shipped runs/findings; next free number stays 028). **NO score-math changes** (WU-4A is frozen). **Do NOT modify** the frozen input types `GradeResult`, `Finding`, `FindingKind`, `ResolutionResult`, `ResolvedNode`, `Node`, `Candidate`, or the resolution `METHOD_CONFIDENCE_CAP` map.

### 1.4 Test regime (binding)
Pure unit + LLM-mock ONLY. NO Testcontainers, NO Docker, NO live API. Every audit path injects `audit_fn` (a deterministic stub returning per-entity span-or-null, and a stub that raises to exercise `TranscriptAuditUnavailableError`). Because nothing lands under `database/migrations`, `tests/database`, or `apollo/knowledge_graph`, the real-infra CI gate correctly does NOT trigger. All grading tests live in `apollo/grading/tests/` (collected by `testpaths = tests apollo` in `pytest.ini`).

---

## 2. RECON facts grounded in real code

- **Inputs (frozen, do not modify):**
  - `GradeResult` (`apollo/graph_compare/core.py:52`): `findings: tuple[Finding, ...]` + 10 `*_score` fields + `comparison_confidence` (==1.0 v1) + `comparison_version`. No `events` field by design.
  - `Finding` (`apollo/graph_compare/findings.py:40`): `kind: FindingKind`, `canonical_key`, `student_node_ids`, `reference_node_ids`, `evidence_spans`, `score`, `confidence`, `message` — ALL defaulted; frozen. `FindingKind.MISSING_NODE == "missing_node"`.
  - `ResolutionResult` (`apollo/resolution/result.py:41`): `resolved: tuple[ResolvedNode, ...]` (each `node_id, resolution, resolved_key, resolved_canon_key, method, confidence`) + `tier_counts: Mapping[str,int]` + `llm_calls`.
  - `Node.parser_confidence` (`apollo/ontology/nodes.py:48`): per-turn `float` in `[0,1]`, default 1.0. The in-memory `Node` has **no** `created_at` (turn order is NOT available here; that is WU-4B2's `created_at` concern).
- **`main_chat`** (`apollo/agent/_llm.py:76`) is **SYNC** and returns `str`; signature `(*, purpose:str, messages:list[dict], response_format:dict|None=None, temperature:float=0.0, model:str|None=None)`.
- **Wrapper shape to mirror EXACTLY:** `main_chat_adjudicator` (`apollo/resolution/adjudication.py:74`) — strict `response_format={"type":"json_object"}`, `try/except` that re-raises the already-named error and wraps any other `Exception` into a NAMED infra error with NO FALLBACK. The injectable-`adjudicator` test seam (`apollo/resolution/tests/test_adjudication.py`) is the template: patch the module-level `main_chat`, inject the real wrapper, assert `call_count == 1`, hallucination → hard error, transient → named infra error, named error re-raised verbatim.
- **RECON CORRECTION (the proposal was WRONG):** `METHOD_CONFIDENCE_CAP` (`apollo/resolution/candidates.py:35`) = `{exact:1.00, symbolic:0.98, alias:0.92, fuzzy:0.80, llm:0.75, unresolved:0.00}` — there is **NO** `transcript-audit` key. The audit-upgrade cap is **0.75** (same value as the `llm` tier). Define a NAMED module constant `TRANSCRIPT_AUDIT_CONFIDENCE_CAP = 0.75` in `apollo/grading/` — do **NOT** mutate the frozen resolution map. Likewise `TRANSCRIPT_AUDIT_METHOD = "transcript_audit"`.
- **Persistence columns already exist:** `GraphComparisonRun.abstained` (Boolean NOT NULL default false) and `.abstention_reasons` (`_JSONType` NOT NULL default `list`) — `apollo/persistence/models.py:512-513`. WU-4B3 writes them; this unit produces the values.
- **`FINDING_KINDS`** (`apollo/persistence/models.py:64`) mirrors `FindingKind` 1:1 (asserted elsewhere) — unchanged here.

---

## 3. Files to create / edit

### Create
| File | Role | Approx size |
|---|---|---|
| `apollo/grading/__init__.py` | Package docstring (mirrors `apollo/resolution/__init__.py`: "downstream orchestration that imports `graph_compare`; persists nothing here — that is WU-4B3") + the public re-exports (§5). | ~45 |
| `apollo/grading/transcript_audit.py` | `MissingEntity`, `AuditResult`, `AliasCandidate`, `TRANSCRIPT_AUDIT_CONFIDENCE_CAP`, `TRANSCRIPT_AUDIT_METHOD`, the `AuditFn` type alias, `main_chat_auditor` (the real injectable impl mirroring `main_chat_adjudicator`), and `audit_missing(...)`. | ~190 |
| `apollo/grading/abstention.py` | `ABSTENTION_THRESHOLDS` constant, `Abstention` frozen result, `apply_abstention(...)`. | ~150 |
| `apollo/grading/audited_grade.py` | `AuditedGrade` frozen handoff + `build_audited_grade(...)` orchestrator (runs audit → rewrites findings → applies gates → assembles). | ~150 |
| `apollo/grading/tests/__init__.py` | empty package marker | 1 |
| `apollo/grading/tests/_builders.py` | tiny frozen-fixture helpers (a `missing_finding`, a `ResolutionResult` with chosen tier mix, a deterministic span-or-null `audit_fn`, a raising `audit_fn`) — mirrors `graph_compare/tests/_builders.py`. | ~90 |
| `apollo/grading/tests/test_transcript_audit.py` | audit-path tests (§4.1). | ~220 |
| `apollo/grading/tests/test_abstention.py` | gate tests (§4.2). | ~190 |
| `apollo/grading/tests/test_audited_grade.py` | orchestration + handoff-shape tests (§4.3). | ~170 |
| `apollo/grading/tests/test_package_seam.py` | public-API re-export + value-set parity tests (§4.4). | ~60 |

### Edit
| File | Change |
|---|---|
| `apollo/errors.py` | Add `TranscriptAuditUnavailableError(ApolloError)` with `__init__(self, *, last_error: str)` (and `stage="transcript_audit"` attribute for symmetry with the other infra errors), NO FALLBACK, NOT HTTP-registered here (WU-4C registers it). Mirrors `ResolutionUnavailableError`. **`apollo/errors.py` is inside `apollo/**`** so it is in scope. |
| `docs/architecture/apollo.md` | Owner-doc reconcile (§6): add `apollo/grading/**` to the module map + the `owns:` frontmatter, register the new public callables/types/errors, bump `last_verified` to `2026-06-17`. |

> NOTE on scope-file list: the prompt's scope enumerates the four `apollo/grading/*.py` files, `apollo/grading/tests/**`, and `apollo.md`. The one **named error** (`TranscriptAuditUnavailableError`) must live in `apollo/errors.py` to follow the codebase's single-error-module convention (every `ResolutionUnavailableError` etc. lives there) — `apollo/errors.py` is under `apollo/**`, which the owner doc already owns, so it is in-scope and reconciled in the same commit. (Alternative considered: define the error inside `transcript_audit.py`. Rejected — it would split the NO-FALLBACK error registry the rest of Apollo relies on and break the api.py registration pattern WU-4C expects.)

---

## 4. Public signatures (the handoff contract)

All dataclasses `@dataclass(frozen=True)`; all collection fields are tuples; no in-place mutation anywhere.

### 4.1 `apollo/grading/transcript_audit.py`
```python
TRANSCRIPT_AUDIT_METHOD: str = "transcript_audit"
TRANSCRIPT_AUDIT_CONFIDENCE_CAP: float = 0.75  # == the llm tier cap; NOT a key in METHOD_CONFIDENCE_CAP

@dataclass(frozen=True)
class MissingEntity:
    """A simulator-flagged missing reference entity, fed to the batched audit."""
    canonical_key: str
    display_name: str
    aliases: tuple[str, ...] = ()

@dataclass(frozen=True)
class AliasCandidate:
    """A span-derived learned-alias candidate (anti-laundering, §6.3/§8).
    Value object ONLY — WU-3B2 owns the teacher-approval queue table; emitted
    here, persisted nowhere by this unit. Resolves at the transcript-audit cap
    (0.75), NEVER the alias tier (0.92), until a teacher approves it."""
    canonical_key: str
    span: str
    confidence: float = TRANSCRIPT_AUDIT_CONFIDENCE_CAP

@dataclass(frozen=True)
class AuditResult:
    """Outcome of ONE batched transcript audit over the missing entities."""
    upgraded_keys: frozenset[str]          # missing keys the audit FOUND a span for
    spans_by_key: Mapping[str, str]        # canonical_key -> quoted supporting span (found only)
    alias_candidates: tuple[AliasCandidate, ...]

# Injectable audit function: request -> {canonical_key: span_or_None}.
AuditReply = dict[str, str | None]
AuditFn = Callable[[AuditRequest], AuditReply]   # AuditRequest = frozen (entities, transcript)

def main_chat_auditor(request: "AuditRequest") -> AuditReply:
    """The real one-call auditor: a single sync main_chat (purpose
    'transcript_audit', strict json_object, temp 0.0). Mirrors
    main_chat_adjudicator: re-raises an already-named
    TranscriptAuditUnavailableError verbatim; wraps any other Exception (incl.
    json.JSONDecodeError) into TranscriptAuditUnavailableError. NO FALLBACK."""

def audit_missing(
    missing_entities: tuple[MissingEntity, ...],
    transcript: str,
    *,
    audit_fn: AuditFn | None = None,    # default = main_chat_auditor (live path; never fires in tests)
) -> AuditResult:
    """Run ONE batched audit (never per-entity). Empty missing_entities -> an
    empty AuditResult and NO call (mirrors adjudicate's empty-remainder short
    circuit). A returned key not in the asked set is ignored (defensive, logged).
    A None span = 'not found' (entity stays missing). A non-None span =
    'found' -> entity in upgraded_keys + spans_by_key + an AliasCandidate at
    TRANSCRIPT_AUDIT_CONFIDENCE_CAP. Any infra failure surfaces as
    TranscriptAuditUnavailableError (raised by main_chat_auditor; a custom
    audit_fn that raises the named error is re-raised verbatim)."""
```
**Failure-mode binding:** `audit_missing` does NOT catch `TranscriptAuditUnavailableError`. The *caller* (`build_audited_grade`) catches it and routes to the abstention gate (suppress-ALL-missing) — there is never a path that swallows the failure and emits the missing finding. The transcript is **chunked** if it exceeds a token budget (per-turn windows, entities re-asked per chunk, spans deduped) per §6.3 "Context budget"; v1 implements a simple character-window chunker with the budget as a named constant `AUDIT_TRANSCRIPT_CHAR_BUDGET` and merges per-chunk replies (a span found in ANY chunk wins). Chunking is invisible to `audit_fn` callers in tests (single short transcript = one chunk = one call).

### 4.2 `apollo/grading/abstention.py`
```python
ABSTENTION_THRESHOLDS = {
    "unresolved_rate": 0.35,            # > 0.35 -> no Layer-3 update (diagnostic-only)
    "min_parser_confidence": 0.6,       # MIN over turns < 0.6 -> suppress 'missing'
    "misconception_confidence": 0.8,    # < 0.8 -> withhold 'misconception'
}

# Reason strings persisted verbatim into abstention_reasons (WU-4B3). Named
# constants so WU-4B2/4B3 match against symbols, not magic strings.
REASON_HIGH_UNRESOLVED = "unresolved_rate_above_threshold"
REASON_LOW_PARSER_CONFIDENCE = "min_parser_confidence_below_threshold"
REASON_LOW_MISCONCEPTION_CONFIDENCE = "misconception_confidence_below_threshold"
REASON_REFERENCE_INVALID = "reference_graph_invalid"
REASON_TRANSCRIPT_AUDIT_FAILED = "transcript_audit_unavailable"

@dataclass(frozen=True)
class Abstention:
    abstention_reasons: tuple[str, ...]     # -> apollo_graph_comparison_runs.abstention_reasons (WU-4B3)
    abstained: bool                         # -> .abstained; True iff the unresolved-rate gate tripped
    suppressed_event_kinds: frozenset[str]  # event kinds WU-4B2 must withhold: subset of {"missing","misconception"}

def apply_abstention(
    *,
    unresolved_rate: float,
    min_parser_confidence: float,           # MIN over the attempt's turns (NEVER mean) — §6.6 binding
    misconception_confidences: tuple[float, ...] = (),  # one per contradiction finding considered
    transcript_audit_failed: bool = False,
    reference_invalid: bool = False,        # already raised upstream as ReferenceGraphInvalidError (WU-4A1)
) -> Abstention:
    """Apply the §6.6 gates and return the reasons + flags + suppression set.
    - unresolved_rate > 0.35           -> abstained=True, add REASON_HIGH_UNRESOLVED
                                          (no Layer-3 update; diagnostic-only run)
    - min_parser_confidence < 0.6      -> suppress 'missing', add REASON_LOW_PARSER_CONFIDENCE
    - transcript_audit_failed          -> suppress 'missing', add REASON_TRANSCRIPT_AUDIT_FAILED
    - any misconception_confidence<0.8 -> suppress 'misconception', add REASON_LOW_MISCONCEPTION_CONFIDENCE
                                          (the finding still persists for diagnostic review)
    - reference_invalid                -> add REASON_REFERENCE_INVALID (grading already blocked upstream;
                                          surfaced/recorded here, not re-raised)
    Pure: deterministic reason ordering (gate declaration order), no IO."""
```
**`abstained` semantics (binding):** `abstained=True` is reserved for the *no-Layer-3-update* run (the unresolved-rate gate). The `missing`/`misconception` suppressions are *partial* — they withhold specific event kinds but the run still updates Layer-3 for the rest, so they populate `suppressed_event_kinds` and `abstention_reasons` but do NOT by themselves set `abstained=True`. (`unresolved_rate > 0.35` is "no learner update at all"; `parser_confidence < 0.6` is "suppress `missing` only" — §6.6 distinguishes them.)

**Helper:** `unresolved_rate_of(resolution: ResolutionResult) -> float` (pure) — `unresolved_count / total` from `resolution.resolved` (count `resolution != "resolved"`), `0.0` for an empty attempt. `min_parser_confidence_of(nodes: Iterable[Node]) -> float` — `min(n.parser_confidence ...)`, default `1.0` for empty (so an empty attempt never false-trips). These live in `abstention.py` so the orchestrator can derive the gate inputs from the real upstream objects without WU-4B2 plumbing.

### 4.3 `apollo/grading/audited_grade.py`
```python
@dataclass(frozen=True)
class AuditedGrade:
    """The frozen WU-4B1 handoff artifact: a GradeResult with audit-upgraded
    findings + the abstention outcome + emitted alias candidates. WU-4B2 reads
    .findings + .suppressed_event_kinds to convert findings->events (§6.5);
    WU-4B3 persists .abstention_reasons + .abstained on the runs row."""
    grade: GradeResult                       # the WU-4A2 score-math result (unchanged scores)
    findings: tuple[Finding, ...]            # grade.findings with missing_node upgrades applied
    abstention_reasons: tuple[str, ...]
    abstained: bool
    suppressed_event_kinds: frozenset[str]
    alias_candidates: tuple[AliasCandidate, ...]

def build_audited_grade(
    grade: GradeResult,
    *,
    transcript: str,
    resolution: ResolutionResult,
    student_nodes: tuple[Node, ...],
    candidates: tuple[Candidate, ...] = (),   # supplies display_name + aliases for each missing key
    reference_invalid: bool = False,
    audit_fn: AuditFn | None = None,          # injected in every test; default = main_chat_auditor
) -> AuditedGrade:
    """Orchestrate §6.4 step 12 + step 14:
    1. Collect missing_node findings from grade.findings -> MissingEntity list
       (display_name/aliases looked up from `candidates` by canonical_key; a
       missing-from-candidates key falls back to its own key as display name).
    2. transcript_audit_failed = False; try audit_missing(...); on
       TranscriptAuditUnavailableError set transcript_audit_failed=True and
       audit_result = empty (NO span upgrades, NO alias candidates). The error
       is NOT re-raised past this boundary — it converts to the suppress-all-
       missing abstention reason (§6.6 binding: audit-infra failure suppresses
       ALL missing, NEVER emits them).
    3. Compute gate inputs: unresolved_rate_of(resolution),
       min_parser_confidence_of(student_nodes), misconception_confidences from
       the contradiction findings' confidence, transcript_audit_failed,
       reference_invalid. apply_abstention(...).
    4. Rewrite findings: for each missing_node whose key is in
       audit_result.upgraded_keys, REPLACE it with a NEW upgraded finding
       (kind stays MISSING_NODE? -> NO: emit an upgraded COVERED_NODE-shaped
       finding carrying method/confidence<=0.75 + the span as evidence_spans;
       see note below). Missing keys NOT found stay as-is. Immutable: build a
       NEW findings tuple, never mutate Finding instances.
    5. Return AuditedGrade(grade, new_findings, reasons, abstained,
       suppressed_event_kinds, alias_candidates)."""
```
**Upgraded-finding shape (decision, §6.5 row 4 "method = `transcript_audit`, confidence ≤ 0.75"):** an audit-found `missing_node` is replaced by a `Finding(kind=FindingKind.COVERED_NODE, canonical_key=key, evidence_spans=(span,), confidence=TRANSCRIPT_AUDIT_CONFIDENCE_CAP, message="upgraded by transcript audit")`. Carrying the audit method on the finding: since `Finding` has no `method` field (frozen, WU-4A2), the audit provenance rides in `message` + the capped `confidence` + the quoted `evidence_spans`; WU-4B2's decision table reads `confidence <= 0.75` + the `transcript_audit` message marker to grade it `partial`/`covered`. This keeps `Finding` un-modified (it is frozen and out of scope). The original `missing_node` finding is dropped from the rewritten tuple for that key (it is now covered) — a `missing` event can therefore never survive for an audit-found key (the §6.11 "parser misses a key sentence → NO false missing" fixture).

### 4.4 `apollo/grading/__init__.py` — public API (re-exports)
```python
__all__ = [
    "audit_missing", "AuditResult", "MissingEntity", "AliasCandidate",
    "TranscriptAuditUnavailableError",  # re-exported from apollo.errors for the grading surface
    "TRANSCRIPT_AUDIT_CONFIDENCE_CAP", "TRANSCRIPT_AUDIT_METHOD",
    "apply_abstention", "Abstention", "ABSTENTION_THRESHOLDS",
    "build_audited_grade", "AuditedGrade",
]
```

---

## 5. TDD-ordered implementation steps

> Strict RED→GREEN→REFACTOR per superpowers TDD. Write the test file (or the next test) FIRST, watch it fail for the right reason, then write the minimal code. No skip/xfail, no assert-nothing.

**Step 0 — package skeleton + error (RED guard).**
Write `apollo/grading/tests/test_package_seam.py::test_public_api_exports` asserting every name in §4.4 imports from `apollo.grading`. Run → ImportError (RED). Create `apollo/grading/__init__.py`, the three empty modules with just the dataclasses/constants, `apollo/grading/tests/__init__.py`, and add `TranscriptAuditUnavailableError` to `apollo/errors.py`. Run → GREEN.

**Step 1 — `transcript_audit.py` value types + caps.**
Tests for `AliasCandidate`/`AuditResult`/`MissingEntity` shapes + `TRANSCRIPT_AUDIT_CONFIDENCE_CAP == 0.75` + `TRANSCRIPT_AUDIT_METHOD == "transcript_audit"` + the parity assertion `TRANSCRIPT_AUDIT_CONFIDENCE_CAP == METHOD_CONFIDENCE_CAP["llm"]` and `"transcript_audit" not in METHOD_CONFIDENCE_CAP` (locks the RECON correction). RED → implement constants/dataclasses → GREEN.

**Step 2 — `audit_missing` happy/empty/found/not-found (mocked).**
Tests inject a deterministic `audit_fn`. Cover: empty entities → empty result + zero calls; one entity, span found → `upgraded_keys`/`spans_by_key`/`AliasCandidate@0.75`; one entity, `None` → not upgraded; an extra returned key not asked → ignored. RED → implement → GREEN.

**Step 3 — `audit_missing` failure surfaces the named error; `main_chat_auditor` mirrors the adjudicator.**
Inject an `audit_fn` that raises `TranscriptAuditUnavailableError` → it propagates (NOT swallowed). Patch `apollo.grading.transcript_audit.main_chat` (mirroring the adjudication test): a transient `RuntimeError` → `TranscriptAuditUnavailableError`; a malformed-JSON return → `TranscriptAuditUnavailableError`; a pre-named error → re-raised verbatim; a well-formed return → parsed reply; assert exactly one `main_chat` call. RED → implement `main_chat_auditor` + the try/except → GREEN.

**Step 4 — chunking.**
Test a transcript longer than `AUDIT_TRANSCRIPT_CHAR_BUDGET` with an `audit_fn` recording call count: entities re-asked per chunk, a span found in the 2nd chunk still appears in `upgraded_keys`, spans deduped. RED → implement the window chunker + per-chunk merge → GREEN.

**Step 5 — `abstention.py` gates.**
One test per gate row in §4.2 plus the helpers. Critically include the **MIN-not-mean** test (Step 7 below names it). RED → implement `apply_abstention` + helpers → GREEN.

**Step 6 — `build_audited_grade` orchestration.**
Behaviour fixtures (§4.3 / §6.11): parser-miss-then-audit-finds upgrade; high-unresolved abstain; audit-infra failure suppresses all missing; misconception-confidence withhold. RED → implement orchestrator → GREEN.

**Step 7 — REFACTOR + coverage.**
Run `pytest apollo/grading -q`, then `pytest --cov=apollo.grading --cov-report=xml` and `diff-cover coverage.xml --compare-branch=feat/apollo-kg-wu4a2-simulation-scores --fail-under=95`. Add targeted tests for any uncovered branch (defensive branches get a `# pragma: no cover` ONLY if genuinely unreachable, e.g. the "no member carries a node" defensive return — mirror `canonical.py`'s precedent). Reconcile the owner doc (§6).

---

## 6. Full test list

Each test asserts a real behaviour; external deps (LLM/network) are mocked by **injecting `audit_fn`** or by `unittest.mock.patch("apollo.grading.transcript_audit.main_chat", ...)`. NO container, NO live API.

### 6.1 `test_package_seam.py`
- `test_public_api_exports` — every name in §4.4 is importable from `apollo.grading`.
- `test_finding_kind_unchanged` — `apollo.grading` does NOT redefine `FindingKind`; it imports the frozen one (asserts `FindingKind is grading-imported FindingKind` / value-set parity with `models.FINDING_KINDS`).
- `test_audit_cap_parity` — `TRANSCRIPT_AUDIT_CONFIDENCE_CAP == 0.75 == METHOD_CONFIDENCE_CAP["llm"]` AND `"transcript_audit" not in METHOD_CONFIDENCE_CAP` (locks the RECON correction; no map mutation).
- `test_abstention_thresholds_shape` — `ABSTENTION_THRESHOLDS` has exactly the three keys at `0.35/0.6/0.8`.
- `test_error_is_apollo_error_not_http_registered` — `issubclass(TranscriptAuditUnavailableError, ApolloError)` and it is NOT in `api.py`'s registered handlers (WU-4C registers it).

### 6.2 `test_transcript_audit.py`
- `test_empty_missing_entities_no_call` — `audit_missing((), "...", audit_fn=spy)` returns an empty `AuditResult` and the spy is never called (mirrors `test_empty_remaining_makes_no_call`).
- `test_span_found_upgrades_key_and_emits_alias` — one entity, stub returns a span → key in `upgraded_keys`, `spans_by_key[key] == span`, exactly one `AliasCandidate(canonical_key=key, span=span, confidence=0.75)`.
- `test_alias_candidate_confidence_is_audit_cap_not_alias_tier` — emitted `AliasCandidate.confidence == 0.75`, explicitly `!= 0.92` (anti-laundering: never the alias tier).
- `test_span_none_leaves_key_missing` — stub returns `None` → key NOT in `upgraded_keys`, no alias candidate.
- `test_unasked_returned_key_ignored` — stub returns a key not in the asked set → ignored, not in result (defensive).
- `test_multiple_entities_partial_found` — three entities, stub finds two → exactly those two upgraded, two alias candidates, deterministic ordering.
- `test_injected_audit_fn_raise_propagates` — `audit_fn` raises `TranscriptAuditUnavailableError` → `audit_missing` propagates it (NOT swallowed, NO empty result).
- `test_main_chat_auditor_one_call` — patch `main_chat` returning valid JSON, inject `main_chat_auditor`, assert `main_chat.call_count == 1`.
- `test_main_chat_auditor_transient_failure_named` — patched `main_chat` raises `RuntimeError` → `TranscriptAuditUnavailableError` with `stage == "transcript_audit"`.
- `test_main_chat_auditor_malformed_json_named` — patched `main_chat` returns `"not json"` → `TranscriptAuditUnavailableError` (JSON-parse failure is an audit-infra failure).
- `test_main_chat_auditor_reraises_named_verbatim` — patched `main_chat` raises a pre-built `TranscriptAuditUnavailableError` → re-raised `is` the same object (mirrors `test_adjudicator_reraises_named_error_unwrapped`).
- `test_long_transcript_chunked_span_in_later_chunk` — transcript > `AUDIT_TRANSCRIPT_CHAR_BUDGET`; recording `audit_fn` asked per chunk; a span found only in the 2nd chunk still lands in `upgraded_keys`; result spans deduped.

### 6.3 `test_abstention.py`
- `test_high_unresolved_rate_abstains` — `unresolved_rate=0.5` → `abstained is True`, `REASON_HIGH_UNRESOLVED in abstention_reasons`. **(§6.11 high-unresolved fixture #2.)**
- `test_unresolved_rate_at_threshold_does_not_abstain` — `0.35` is NOT `> 0.35` → not abstained (boundary).
- `test_low_min_parser_confidence_suppresses_missing` — `min_parser_confidence=0.5` → `"missing" in suppressed_event_kinds`, `REASON_LOW_PARSER_CONFIDENCE` present, `abstained is False` (partial suppression, not full abstain).
- `test_parser_confidence_min_not_mean` — confidences `(0.95, 0.95, 0.95, 0.40)`: MIN `0.40 < 0.6` trips the gate, while the MEAN `0.8125 >= 0.6` would NOT. Asserts `min_parser_confidence_of(nodes)==0.40`, the gate fires, and `"missing"` is suppressed. **(Proves MIN, not mean — fixture #4.)**
- `test_audit_failure_suppresses_all_missing` — `transcript_audit_failed=True` → `"missing" in suppressed_event_kinds`, `REASON_TRANSCRIPT_AUDIT_FAILED` present. **(§6.6 audit-infra binding, fixture #3 half.)**
- `test_low_misconception_confidence_withholds` — `misconception_confidences=(0.7,)` (`<0.8`) → `"misconception" in suppressed_event_kinds`, `REASON_LOW_MISCONCEPTION_CONFIDENCE` present. **(Fixture #5 negative half.)**
- `test_high_misconception_confidence_does_not_withhold` — `misconception_confidences=(0.85,)` (`>=0.8`) → `"misconception" NOT in suppressed_event_kinds`, reason absent. **(Fixture #5 positive half — proves the threshold edge.)**
- `test_reference_invalid_recorded_not_raised` — `reference_invalid=True` → `REASON_REFERENCE_INVALID` present, no exception raised (already raised upstream by WU-4A1; surfaced/recorded here).
- `test_no_gates_clean_run` — all inputs clean → empty reasons, `abstained is False`, empty suppression set.
- `test_reasons_deterministic_order` — two calls with the same inputs → identical `abstention_reasons` tuple (gate-declaration order).
- `test_unresolved_rate_of_helper` — `ResolutionResult` with 1 unresolved of 4 → `0.25`; empty → `0.0`.
- `test_min_parser_confidence_of_empty_is_one` — empty node iterable → `1.0` (never false-trips an empty attempt).

### 6.4 `test_audited_grade.py`
- `test_parser_miss_audit_finds_span_upgrades_no_false_missing` — a `GradeResult` carrying one `missing_node` for `eq.continuity`; `audit_fn` returns a span for it; `candidates` supplies its display name. Asserts the rewritten `findings` contain a COVERED_NODE-shaped finding for `eq.continuity` with `confidence <= 0.75` and the span in `evidence_spans`; NO `missing_node` finding for that key survives; exactly one `AliasCandidate` emitted. **(§6.11 fixture #1 — the binding "parser misses a key sentence" case.)**
- `test_audit_found_key_confidence_capped` — the upgraded finding's `confidence == 0.75` exactly (cap honoured).
- `test_audit_not_found_key_stays_missing` — `audit_fn` returns `None` for the key → `missing_node` finding survives unchanged, no alias candidate.
- `test_audit_infra_failure_suppresses_all_missing_and_records_reason` — `audit_fn` raises `TranscriptAuditUnavailableError`; two `missing_node` findings present. Asserts: NO upgrade; `"missing" in suppressed_event_kinds`; `REASON_TRANSCRIPT_AUDIT_FAILED in abstention_reasons`; the named error was caught (the call returns an `AuditedGrade`, does NOT raise) AND was NOT silently swallowed (its reason is recorded — proof it was surfaced). **(§6.11 fixture #3 — full binding.)**
- `test_high_unresolved_run_abstains_findings_preserved` — `resolution` with `unresolved_rate > 0.35` → `abstained is True`, `REASON_HIGH_UNRESOLVED` present, and `findings` are still fully populated (an abstained run still carries its findings for the diagnostic + WU-4B3 persistence). **(§6.11 fixture #2 end-to-end.)**
- `test_misconception_low_confidence_withheld_in_grade` — a contradiction finding at `confidence=0.7` → `"misconception" in suppressed_event_kinds`; the contradiction finding still present in `findings` (persists for diagnostic review).
- `test_build_audited_grade_is_immutable` — the input `GradeResult` and its `Finding` objects are unchanged (identity-compared) after the call (no in-place mutation); the rewritten findings are a NEW tuple.
- `test_build_audited_grade_no_missing_nodes_noop` — a `GradeResult` with zero `missing_node` findings → `audit_fn` never called (no entities), clean abstention, findings unchanged.
- `test_audited_grade_carries_score_math_unchanged` — `audited.grade is grade` and the 10 `*_score` fields are untouched (WU-4B1 never re-grades).
- `test_default_audit_fn_is_main_chat_auditor` — with `audit_fn=None` and a patched `main_chat` returning a valid empty-spans reply, `build_audited_grade` drives the live wrapper (one `main_chat` call) — proves the default wiring without a live API.
- `test_missing_entity_display_name_falls_back_to_key` — a `missing_node` key absent from `candidates` → `MissingEntity.display_name == canonical_key` (no crash, no KeyError).

### 6.5 `_builders.py` (fixtures, not tests)
Helpers: `missing_grade(keys, *, contradictions=())` (a `GradeResult` with `missing_node` findings for `keys` + optional contradiction findings at chosen confidences, all other scores stubbed valid/non-NaN), `resolution_with(unresolved=0, resolved=0)` (a `ResolutionResult` with the chosen tier mix), `nodes_with_confidences(*vals)` (`Node`s via `build_node` at the given `parser_confidence`), `found_audit_fn(mapping)` / `notfound_audit_fn()` / `raising_audit_fn()` (the three deterministic stubs). Each builder returns a frozen object; the raising stub raises the named error.

---

## 7. Owner-doc updates (`docs/architecture/apollo.md`)

In the **same commit** as the code (drift contract):
1. **Frontmatter:** add `- apollo/grading/**` to `owns:` (already covered by `apollo/**`, but list it explicitly for discoverability, mirroring how `apollo/graph_compare/**` is listed). Bump `last_verified: 2026-06-17`.
2. **Module map table:** add a `apollo/grading/` row: "WU-4B1 — the §6 grading **orchestration** layer that IMPORTS `graph_compare`'s pure score core and turns a `GradeResult` into an `AuditedGrade`. `transcript_audit.py` = the §6.4-step-12 batched Done-time missing-node audit (ONE injectable `audit_fn` call, default `main_chat_auditor`; span found → upgrade to a covered-grade finding at `confidence ≤ 0.75` + an `AliasCandidate`; any infra failure → `TranscriptAuditUnavailableError`, NEVER 'emit missing'); `abstention.py` = the §6.6 hard gates (`apply_abstention` → reasons + `abstained` + the per-event-kind suppression set; `parser_confidence` is MIN over turns, never mean; the audit-upgrade cap is `0.75 == llm tier`, a NAMED constant, NOT a key added to the frozen `METHOD_CONFIDENCE_CAP`); `audited_grade.py` = `build_audited_grade` orchestrating step 12 + step 14 into the frozen `AuditedGrade` handoff. Persists NOTHING (runs/findings + `abstention_reasons`/`abstained` writes are WU-4B3); produces NO events (finding→event is WU-4B2); emits `AliasCandidate` value objects only (the §8 teacher-approval queue is WU-3B2)."
3. **Public interfaces / key entry points:** add `audit_missing`, `apply_abstention`, `build_audited_grade` with their signatures (§4) and the note that `audit_fn` defaults to the live `main_chat_auditor` but every test injects a stub (CI-safe, no live LLM).
4. **Core types:** add an **"Audited-grade types (`apollo/grading/`, WU-4B1)"** bullet documenting `AuditResult`, `MissingEntity`, `AliasCandidate`, `Abstention`, `AuditedGrade`, `ABSTENTION_THRESHOLDS`, `TRANSCRIPT_AUDIT_CONFIDENCE_CAP`/`TRANSCRIPT_AUDIT_METHOD`.
5. **NO FALLBACK conventions:** append `TranscriptAuditUnavailableError(last_error)` to the WU-3C/4A "named-but-not-HTTP-registered" list — raised by the transcript auditor; the orchestrator catches it at the audit boundary and converts it into the suppress-all-`missing` abstention reason (so a missing event can never be emitted from a failed audit), and WU-4C registers the HTTP handler.

---

## 8. Risks

1. **`Finding` has no `method` field (frozen, WU-4A2).** The audit upgrade therefore rides in `message` + capped `confidence` + `evidence_spans` rather than a typed method field. *Mitigation:* document the marker contract explicitly (§4.3) so WU-4B2's decision table reads it deterministically; assert the marker in `test_parser_miss_audit_finds_span_upgrades_no_false_missing`. (Do NOT add a field to the frozen `Finding` — out of scope, breaks WU-4A.)
2. **Turn order is unavailable here.** The in-memory `Node` has no `created_at`, and `GradeResult`/`Finding` carry none either; the §6.5 `corrected`/`misconception` *turn-order* rows are WU-4B2's problem (they read persisted `created_at`). *Mitigation:* this unit's misconception gate is confidence-only (§6.6), never order-dependent — no turn-order logic enters WU-4B1.
3. **`abstained` vs partial-suppression confusion.** Easy to wrongly set `abstained=True` on a `missing`-suppression. *Mitigation:* the binding semantics in §4.2 (only the unresolved-rate gate sets `abstained`) plus `test_low_min_parser_confidence_suppresses_missing` asserting `abstained is False`.
4. **Cap drift.** A future edit could resync the audit cap to the wrong tier. *Mitigation:* `test_audit_cap_parity` locks `0.75 == METHOD_CONFIDENCE_CAP["llm"]` and `"transcript_audit" not in METHOD_CONFIDENCE_CAP`.
5. **Chunking changing call count breaks the "≤1 call" mental model.** Unlike the resolver's strict 1-call adjudication, a chunked audit may make >1 `main_chat` call for a very long transcript. *Mitigation:* document that the §6.3 budget is "the call cannot blow context" (chunked), NOT "exactly one call"; tests assert one call for short transcripts and explicit per-chunk behaviour for long ones.
6. **Diff-cover base branch.** Must compare against `feat/apollo-kg-wu4a2-simulation-scores` (the WU-4B1 parent), not `origin/staging`. *Mitigation:* named explicitly in Step 7; the branch exists locally (verified).
7. **Patch-coverage on defensive branches.** The "unasked returned key ignored" / empty-iterable branches must be covered or `pragma`'d. *Mitigation:* dedicated tests (`test_unasked_returned_key_ignored`, `test_min_parser_confidence_of_empty_is_one`) cover them rather than pragma-ing.

---

## 9. Out-of-scope boundary checklist (re-stated for the implementer)

- [ ] NO finding→event conversion (§6.5 read for context only — WU-4B2).
- [ ] NO Postgres persistence; do NOT write `abstained`/`abstention_reasons`/runs/findings (WU-4B3). Produce the values only.
- [ ] NO event production / Layer-3 (§3) update.
- [ ] NO alias-candidate queue table; emit `AliasCandidate` value objects only (WU-3B2 / §8 follow-up).
- [ ] NO DB read of the transcript from `apollo_messages` (WU-4C threads it; here it's passed in).
- [ ] NO migration (026 shipped runs/findings; next free stays 028).
- [ ] NO score-math changes; do NOT touch `apollo/graph_compare/` or `apollo/resolution/`.
- [ ] Do NOT mutate the frozen `METHOD_CONFIDENCE_CAP`; the audit cap is a NEW named constant in `apollo/grading/`.
- [ ] Do NOT modify `GradeResult` / `Finding` / `FindingKind` / `ResolutionResult` / `Node` / `Candidate`.
- [ ] Inject `audit_fn` in every test — no live API, no Docker, no Testcontainers.
