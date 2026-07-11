# Apollo Misconception Detector — Implementation Plan (TDD)

**Date:** 2026-07-08
**Spec (authoritative for WHAT):** `docs/_archive/specs/2026-07-08-apollo-misconception-detector-design.md`
**Owner doc (reconcile on landing):** `docs/architecture/apollo.md`
**Branch:** `feat/apollo-misconception-detector` (off `origin/staging`)
**New package:** `apollo/overseer/misconception_detector/`
**Coverage contract:** `diff-cover coverage.xml --compare-branch=origin/staging --fail-under=95`

> This plan is the frozen build contract. Every locus below was RE-VERIFIED against the
> current staging tree on 2026-07-08 (see "Loci verification" — line numbers here are the
> *actual current* lines, not the spec's stale `feat/apollo-clarification-v2-ranker` refs).

---

## 0. Loci verification (current tree, not the spec's branch)

| Symbol | File | Current lines | Confirmed shape |
|---|---|---|---|
| `handle_done` | `apollo/handlers/done.py` | 349-677 | `compute_coverage` @396; `_attempt_misconception_scores`+`compute_rubric` @403-410; `student_response` dict @473-497; `write_artifacts` call @652-662 |
| `build_llm_artifact` | `apollo/grading/artifact_build.py` | 360-501 | `misconception_penalty = 0.0` @419; `composite = _round_like_composite(overall/100)` @420-423; `"misconceptions": []` @489; `abstention` dict @468-480 |
| `_BATCH_BINARY_PROMPT` / `_batch_binary_match` | `apollo/overseer/coverage.py` | 81-122 / 125-196 | sign-flip clause @103-104 "Sign flips and algebraic rearrangements are equivalent."; equation is a `_BINARY_TYPES` member @305; call site `_binary_task` @371-379 |
| `compute_rubric` | `apollo/overseer/rubric.py` | 79-180 | `AXIS_WEIGHTS` @39-44 (incl. `misconception_corrected` 0.05); absent-axis redistribution @150-157 |
| `_symbolic_equiv` / `match_symbolic` | `apollo/resolution/tiers.py` | 165-185 / 188-207 | sign-exact `simplify(a-b)==0`; `_extended_locals` @128-140, `_zero_form` @143-163, `student_surface_text` @61-86 |
| `match_equation_alignment` | `apollo/resolution/equation_alignment.py` | 121-168 | derived tier, cap 0.95, reuses `_extended_locals`/`_zero_form`/`student_surface_text` |
| `is_misconception_key` / `contradiction_nodes` | `apollo/graph_compare/soundness.py` | 41-43 / 46-49 | prefix `misc.` |
| `match_by_embedding` / `load_for_concept` | `apollo/overseer/misconception_bank.py` | 91-171 / 71-88 | returns `list[tuple[MisconceptionEntry, float]]`; SQLite-bypassed (raw pgvector SQL) |
| `record_observations_from_canonical` / `_signature_for` | `apollo/emergent/store.py` | 102-157 / 52-59 | reads `misconceptions[]` + `node_ledger[status=="misconception"]`; idempotent ON CONFLICT(attempt_id,signature); does NOT commit |
| `write_artifacts` (emergent writer call) | `apollo/handlers/artifact_writer.py` | 221-241 | gated by `emergent_misconceptions_enabled()`; own failure domain |
| `emergent_misconceptions_enabled` | `apollo/emergent/config.py` | 37-42 | `_TRUTHY = {"1","true","yes","on"}` |
| `main_chat` | `apollo/agent/_llm.py` | 76-95 | **returns str only — NO logprob** (see R1) |
| `generate_diagnostic` | `apollo/overseer/diagnostic.py` | 46-90 | template for LLM module: direct `OpenAI()`, `model: str\|None`, try/except soft-fail |
| reference graph / centrality | `apollo/schemas/problem.py::to_kg_graph` | 113-194 | edges: `DEPENDS_ON` (@139-157), `USES` proc→eq (@159-174), `PRECEDES` proc chain (@176-192). `KGGraph.incoming/outgoing/neighbors/topological_order` in `apollo/ontology/graph.py` |
| `Misconception` (bank) | `apollo/persistence/models.py` | 227-251 | `id, concept_id, code, description, confusion_pair_a/b, trigger_phrases, probe_question, rt_steps` |
| `MisconceptionObservation` (ledger) | `apollo/persistence/models.py` | 956-998 | `search_space_id, concept_id, signature, user_id, attempt_id, confidence, opposes, evidence_span, source`; UNIQUE(attempt_id, signature) |
| `Message` (raw utterances) | `apollo/persistence/models.py` | 343-363 | `attempt_id, role, content (Text), turn_index, message_metadata`. Student turns feed `bank_pattern` |
| composite/bands (A4) | `apollo/grading/composite.py` @73-90 / `apollo/projections/scorecard.py` @95-102,195-200 / `apollo/overseer/rubric.py` `LETTER_BANDS`+`score_to_letter` @47-63 | `composite = w_n*nc + w_e*ec - p*mp` on the artifact; **two distinct band systems**: the RUBRIC uses LETTER bands (`LETTER_BANDS`/`score_to_letter`, `rubric.py`) and is what `student_response.rubric.overall.{score,letter}` carries; only the SCORECARD PROJECTION (`scorecard.py`) uses NAMED bands (Strong 0.85 / Proficient 0.70 / Developing 0.50) derived from `scores.composite`. Do not conflate the two. |

**Drift found vs spec:** spec cited `build_llm_artifact` penalty @427 / `misconceptions:[]` @497 / composite @420-431 — actual are @419 / @489 / @420-423. Spec cited `soundness.is_misconception_key` @41-49 — actual `is_misconception_key` @41-43, `contradiction_nodes` @46-49. All other cited loci match within a few lines. **No structural drift; signatures below are safe to freeze.**

---

## 1. Design invariants (must hold in every task)

1. **Default-OFF flag `APOLLO_MISCONCEPTION_DETECTOR`.** When OFF, `handle_done` and
   `build_llm_artifact` are byte-identical to today (penalty 0.0, `misconceptions: []`,
   composite unchanged). This is the hard regression guard.
2. **Detector only caps or subtracts — never adds credit.** Coverage/rubric are unchanged inputs.
3. **Immutable value objects** (`@dataclass(frozen=True)`, tuples not lists in fields). Merge
   returns new objects; never mutate `coverage`/`rubric`/`DetectionResult` in place.
4. **DI seam on every LLM touch** (`JudgeFn` Protocol) so CI runs with zero live OpenAI calls.
5. **Soft-fail on the LLM path** — a judge crash returns `clear`, never breaks grading
   (mirrors `diagnostic.py` @82-87).
6. **Many small files** (200-400 lines). One responsibility per module.
7. **95% patch coverage** on changed lines vs `origin/staging`.

---

## 2. Frozen value objects (`apollo/overseer/misconception_detector/types.py`)

```python
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Literal

Verdict = Literal["clear", "needs_clarification", "misconception", "wrong"]
DetectorSource = Literal["sympy_veto", "bank_pattern", "judge"]

@dataclass(frozen=True)
class ConceptFinding:
    """One per-concept misconception signal from ONE detector tier. Immutable."""
    concept_key: str            # reference node canonical_key / node_id the finding attaches to
    verdict: Verdict
    confidence: float           # 0..1; for judge this is verdict_token_prob (see R1)
    severity: float             # w(centrality) * confidence, filled by merge (0.0 pre-merge)
    evidence_span: str          # student surface text that triggered it ("" if none)
    signature: str              # "misc.<code>" if bank-matched else "unkeyed:<concept_id>"
    source: DetectorSource
    corroborated: bool          # set True by merge when >=2 tiers agree or a deterministic veto

@dataclass(frozen=True)
class DetectionResult:
    """Grader-agnostic detector output. Immutable; `per_concept` is a tuple."""
    per_concept: tuple[ConceptFinding, ...] = field(default_factory=tuple)

    @property
    def is_empty(self) -> bool:
        return len(self.per_concept) == 0

@dataclass(frozen=True)
class MergeOutcome:
    """The merge stage's product: the live penalty + the ledger-feed rows + ceiling flag."""
    misconception_penalty: float          # Σ severity over corroborated findings, clamped
    misconceptions: tuple[dict, ...]       # artifact `misconceptions[]` rows (canonical_key/evidence_span/confidence/opposes)
    ceiling_applied: bool                  # a central corroborated misconception → caps the artifact composite below the named Strong scorecard band (A4)
    ledger_findings: tuple[ConceptFinding, ...]  # gate-cleared corroborated findings for the emergent store
```

Constants live in `config.py` (see Task 1), never inline literals.

---

## 3. The flag (`apollo/overseer/misconception_detector/config.py`)

```python
FLAG_ENV = "APOLLO_MISCONCEPTION_DETECTOR"           # default OFF
_TRUTHY = frozenset({"1", "true", "yes", "on"})

def detector_enabled() -> bool:
    return os.environ.get(FLAG_ENV, "").strip().lower() in _TRUTHY

# calibration knobs — env-overridable, conservative defaults (favor FN over FP; R3)
TAU_FIRE            = _float_env("APOLLO_MISC_TAU_FIRE", 0.85)             # verdict-token prob gate (A1)
TAU_FIRE_VERBALIZED = _float_env("APOLLO_MISC_TAU_FIRE_VERBALIZED", 0.90)  # verbalized-confidence gate (A1; stricter — no logprob backing)
SEVERITY_CLAMP    = _float_env("APOLLO_MISC_SEVERITY_CLAMP", 0.30)  # max total penalty
CENTRALITY_W_MIN  = _float_env("APOLLO_MISC_CENTRALITY_MIN", 0.30)  # peripheral node weight floor
CEILING_COMPOSITE = _float_env("APOLLO_MISC_CEILING", 0.84)    # cap when a central misc fires (< Strong 0.85)
BANK_SIM_FLOOR    = _float_env("APOLLO_MISC_BANK_SIM", 0.80)   # bank_pattern min cosine similarity
```

Reader location: `apollo/overseer/misconception_detector/config.py::detector_enabled`.
Pattern mirrors `apollo/emergent/config.py` (call-time read, `_TRUTHY` set) and
`apollo/grading/composite.py::_env_float`.

---

## 4. Module map (all under `apollo/overseer/misconception_detector/`)

| Module | Purpose | Test file (colocated in `apollo/overseer/tests/`) |
|---|---|---|
| `types.py` | immutable VOs (§2) | `test_misconception_detector_types.py` |
| `config.py` | flag + calibration constants (§3) | `test_misconception_detector_config.py` |
| `centrality.py` | pure: reference graph → `{node_id: centrality 0..1}` | `test_misconception_detector_centrality.py` |
| `sympy_veto.py` | Tier-1 deterministic equation sign-veto (reuses `tiers._symbolic_equiv`) | `test_misconception_detector_sympy_veto.py` |
| `bank_pattern.py` | Tier-1 CBM bank match vs raw student utterances (reuses `misconception_bank`) | `test_misconception_detector_bank_pattern.py` |
| `judge.py` | Tier-2 comparative LLM judge behind `JudgeFn` Protocol | `test_misconception_detector_judge.py` |
| `gate.py` | corroboration + τ_fire gate → dock vs clarification-route | `test_misconception_detector_gate.py` |
| `merge.py` | severity-weighted subtract + anti-dilution ceiling → `MergeOutcome` | `test_misconception_detector_merge.py` |
| `detector.py` | `detect_misconceptions(...)` orchestrator (parallel tiers, DI on judge) | `test_misconception_detector_detector.py` |
| `apply.py` | pure: `MergeOutcome` → adjusted composite/rubric/artifact fields | `test_misconception_detector_apply.py` |

`__init__.py` re-exports `detect_misconceptions`, `detector_enabled`, `DetectionResult`,
`MergeOutcome`, `apply_penalty`.

---

## 5. Public signatures (frozen)

### 5.1 `centrality.py`
```python
def compute_centrality(reference_graph: KGGraph) -> dict[str, float]:
    """Pure. Per reference node_id, a 0..1 centrality score derived ONLY from the
    reference graph already built for the attempt:
      raw = a*downstream_dependents + b*uses_membership + c*precedes_depth
    downstream_dependents = #nodes reachable via incoming DEPENDS_ON (a node many
      others depend on is central); uses_membership = node is an equation linked by
      >=1 USES edge OR a procedure_step; precedes_depth = position in the PRECEDES chain
      normalized to [0,1]. Min-max normalized across nodes; a lone node → 1.0.
    Floors at CENTRALITY_W_MIN so a peripheral node still carries SOME weight.

    **Cycle safety (A6, binding):** `.topological_order(EdgeType.PRECEDES, ...)` can
    raise `ValueError` on a cyclic PRECEDES subgraph (malformed/adversarial reference
    graph). This call MUST be wrapped in `try/except ValueError` — on failure, fall
    back to uniform centrality (or DEPENDS_ON-only centrality, dropping the
    precedes_depth term) for the affected nodes rather than propagating the
    exception. A cyclic reference graph must NEVER crash the grade."""
```
Reuses `KGGraph.incoming(node_id, EdgeType.DEPENDS_ON)`, `.outgoing(..., USES)`,
`.topological_order(EdgeType.PRECEDES, node_type="procedure_step")` (guarded per A6). No LLM, no IO.

### 5.2 `sympy_veto.py`
```python
def detect_sign_veto(
    student_graph: KGGraph,
    reference_graph: KGGraph,
    *,
    bank_entries: tuple[MisconceptionEntry, ...] = (),
) -> tuple[ConceptFinding, ...]:
    """Deterministic Tier-1. For each student equation node:
      - if it is sign-EXACT-equivalent to a reference equation → no finding (correct);
      - elif it matches a pre-authored sign/direction MUTANT of a bank equation
        (reuses tiers._symbolic_equiv against the mutant string) → a `misconception`
        finding, source='sympy_veto', signature='misc.<code>', confidence=1.0,
        corroborated=True (deterministic).
      - else no finding (honest non-detection).
    Pure + deterministic; every SymPy call wrapped try/except→non-match (mirrors
    tiers._symbolic_equiv @182-185). Mutants are read off MisconceptionEntry.
    trigger_phrases entries prefixed 'eq:' (offline authoring, §5 no schema change)."""
```

### 5.3 `bank_pattern.py`
```python
async def detect_bank_pattern(
    db: AsyncSession,
    *,
    concept_id: int | None,
    student_utterances: tuple[str, ...],
    embed_fn: EmbedFn,               # DI seam: (str) -> list[float]; offline stub in tests
    bank_entries: tuple[MisconceptionEntry, ...],
) -> tuple[ConceptFinding, ...]:
    """Tier-1 precision second opinion. Embeds each raw student utterance and runs
    misconception_bank.match_by_embedding (Postgres) OR an in-memory cosine over
    `bank_entries` (SQLite/test — match_by_embedding is pgvector-only, bypassed on
    SQLite per its docstring @105-107). A hit >= BANK_SIM_FLOOR yields a
    `misconception` finding, source='bank_pattern', signature='misc.<code>',
    confidence=similarity, evidence_span=utterance, corroborated=False (needs a
    2nd signal to dock). Abstains (no finding) on no match or empty bank."""
```
`EmbedFn = Protocol[[str], list[float]]`. On SQLite the caller passes `bank_entries`
(from `load_for_concept`) so the pure cosine path runs offline.

### 5.4 `judge.py`

**Forced JSON output shape (A1, binding):** the response schema is one row per
graded concept, and EVERY row MUST include a `"confidence": float` field in `[0, 1]`
in addition to the verdict — this is the verbalized-confidence fallback the gate
uses when no token-level logprob is available:

```json
{"concepts": [{"concept_key": "...", "verdict": "clear|needs_clarification|misconception|wrong", "evidence_span": "...", "confidence": 0.0}]}
```

```python
class JudgeFn(Protocol):
    def __call__(self, *, system: str, user: str) -> JudgeRaw: ...

@dataclass(frozen=True)
class JudgeRaw:
    content: str                     # JSON string; each concept row carries "confidence" (A1)
    verdict_token_prob: float | None # None when logprobs unavailable (R1)

def make_openai_judge(model: str | None = None) -> JudgeFn:
    """Production JudgeFn. This is a NEW call path — it does NOT go through
    apollo.agent._llm.main_chat (that helper returns a plain str with no logprob
    access and is unsuitable here). Instead it calls
    `client.chat.completions.create(..., logprobs=True, top_logprobs=5,
    temperature=0.0, response_format=json_object)` DIRECTLY, then walks
    `resp.choices[0].logprobs.content` looking for the token(s) that carry the
    verdict value to derive `verdict_token_prob` (see R1 for the exact walk).
    Falls back to `None` when logprobs are absent/unwalkable — gate.py then reads
    the per-concept `confidence` field from the parsed JSON instead (A1). This
    single live `client.chat.completions.create` line is the ONLY documented
    coverage exemption in this module (§9); its logprob-walk branch is unit-tested
    against a fabricated `resp` object — no network call in tests."""

def judge_concepts(
    *,
    problem_text: str,
    concepts: tuple[JudgeConceptInput, ...],   # (concept_key, correct_belief, bank_entries)
    judge_fn: JudgeFn,
) -> tuple[ConceptFinding, ...]:
    """One batched comparative call. Prompt places the student's answer side-by-side
    with (a) the correct-belief statement and (b) the concept's bank entries, forced
    to the 4-way {clear|needs_clarification|misconception|wrong} output + evidence_span
    + confidence (A1) per concept. Returns one ConceptFinding per graded concept
    (source='judge', confidence=verdict_token_prob when the raw call supplied one,
    else the parsed per-concept `confidence` field). Malformed output → all-`clear`
    findings (soft-fail; a judge crash must never break grading)."""
```

### 5.5 `gate.py`

**τ selection (A1, binding):** there are TWO calibration constants in `config.py`
— `TAU_FIRE` (0.85, for the token-probability path) and `TAU_FIRE_VERBALIZED`
(0.90, for the verbalized-confidence fallback path, deliberately stricter since
verbalized confidence tends to run overconfident — see R1). `gate.py` selects
between them PER judge finding by inspecting whether that finding's origin
`JudgeRaw.verdict_token_prob` was `None`: `None` → the finding's `confidence` came
from the verbalized field, so gate against `TAU_FIRE_VERBALIZED`; a real
token-probability → gate against `TAU_FIRE`.

```python
def gate_findings(
    findings: tuple[ConceptFinding, ...],
    *,
    tau_fire: float = TAU_FIRE,
    tau_fire_verbalized: float = TAU_FIRE_VERBALIZED,
) -> tuple[ConceptFinding, ...]:
    """Corroboration + confidence gate. Group findings by concept_key. A concept
    DOCKS (finding kept as verdict='misconception', corroborated=True) iff:
      - any deterministic source='sympy_veto' finding is present (self-corroborated), OR
      - >=2 independent sources agree AND the judge finding (if any) has
        confidence >= tau_fire (token-prob path) or >= tau_fire_verbalized
        (verbalized-fallback path, selected by whether that finding's underlying
        verdict_token_prob was None; see A1).
    A lone / sub-τ judge flag → the concept's finding is rewritten to
    verdict='needs_clarification' (routes to the clarification loop; NEVER docks).
    Everything else is dropped. Pure; returns a new tuple."""
```

### 5.6 `merge.py`

**`canonical_key` rule (A5, binding):** each emitted `misconceptions[]` row's
`canonical_key` MUST be the BARE `misc.<code>` value taken straight from the
finding's `signature` when that signature is bank-keyed (i.e. `finding.signature`
already looks like `misc.<code>` per `ConceptFinding.signature`'s own contract in
§2). NEVER re-prefix it and NEVER put the raw `unkeyed:<concept_id>` placeholder
into `canonical_key` — `apollo/emergent/store.py::_signature_for` re-derives the
storage signature downstream from other fields; double-prefixing a signature into
`canonical_key` breaks that re-derivation and the finding can never promote. A
finding whose `signature` is `unkeyed:<concept_id>` (no bank match — a detector
fired but couldn't attribute a bank code) IS still docked/penalized (it still
contributes to `misconception_penalty` and `ceiling_applied`), but it is NOT
emitted as a keyed `misconceptions[]` ledger row — either omit it from the
`misconceptions[]` tuple entirely, or emit it with `canonical_key: None` per the
store's contract for un-attributable rows (§6.5's writer must already tolerate a
missing key; do not invent a new placeholder string).

```python
def merge_detections(
    gated: tuple[ConceptFinding, ...],
    *,
    centrality: dict[str, float],
    clamp: float = SEVERITY_CLAMP,
    ceiling_composite: float = CEILING_COMPOSITE,
) -> MergeOutcome:
    """severity_i = centrality.get(concept_key, CENTRALITY_W_MIN) * confidence_i for
    each corroborated (docked) finding. misconception_penalty = min(clamp, Σ severity_i)
    — computed over ALL docked findings regardless of whether they are bank-keyed.
    ceiling_applied = any docked finding whose centrality >= a 'central' threshold
    (the max-centrality node(s); derived, not hand-authored). misconceptions[] rows
    are built ONLY from docked findings that carry a bank-keyed signature
    ({canonical_key: bare 'misc.<code>', evidence_span, confidence, opposes:None} — A5);
    an `unkeyed:*` docked finding still counts toward the penalty/ceiling above but is
    excluded from (or key-less in) this row list.
    ledger_findings = the docked findings (gate-cleared) for the emergent store.
    Pure; returns a frozen MergeOutcome."""
```

### 5.7 `apply.py`

**Band-system note (A4, binding):** `CEILING_COMPOSITE` (0.84, "below the NAMED
Strong band") is a SCORECARD-projection concept and applies ONLY to the artifact's
`composite` float (consumed downstream by `render_scorecard`, `apollo/projections/scorecard.py`).
The RUBRIC uses LETTER bands, not named bands — there is no "Strong-equivalent"
rubric band. `rubric_overall_after_penalty` therefore does NOT apply
`CEILING_COMPOSITE` to the rubric; it only reduces `overall.score` (an int 0-100)
by the scaled penalty and recomputes `overall.letter` via `rubric.py::score_to_letter`
on the new score. Any "ceiling" on the rubric side is expressed purely as a
score-int / letter cap (e.g. clamp `overall.score` so its letter cannot exceed
some threshold letter) — never as a 0..1 composite-style cutoff, and never
written into named-band vocabulary.

```python
def apply_penalty(
    *,
    composite: float,
    outcome: MergeOutcome,
    ceiling: float = CEILING_COMPOSITE,
) -> float:
    """Pure. renorm(composite - outcome.misconception_penalty), then if
    outcome.ceiling_applied cap at min(result, ceiling). Clamped to [0,1],
    rounded via the same `round(max(0.0, min(1.0, x)), 6)` composite-rounding
    convention (A8 — replicated inline, NOT imported from composite.py). This is
    the number the artifact's `scores.composite` is overwritten with when the
    flag is ON; unchanged input returned when outcome is empty. `ceiling` here is
    the NAMED-band (scorecard) ceiling — it belongs to this function only, not to
    rubric_overall_after_penalty (A4)."""

def rubric_overall_after_penalty(
    rubric: dict, outcome: MergeOutcome
) -> dict:
    """Return a NEW rubric dict (immutable copy) whose `overall.score` (int 0-100)
    is reduced by the penalty (scaled to 0-100) and whose `overall.letter` is
    RECOMPUTED from the new score via `rubric.py::score_to_letter` (LETTER bands,
    A4 — this function does NOT know about the named Strong/Proficient/Developing
    scorecard bands or CEILING_COMPOSITE; that ceiling lives only on the artifact
    composite in `apply_penalty`, consumed by `render_scorecard`). Used to move
    the LIVE student_response['rubric'] score/letter + XP."""
```

### 5.8 `detector.py`
```python
async def detect_misconceptions(
    db: AsyncSession,
    *,
    attempt_id: int,
    concept_id: int | None,
    student_graph: KGGraph,
    reference_graph: KGGraph,
    problem_text: str,
    student_utterances: tuple[str, ...],
    judge_fn: JudgeFn,
    embed_fn: EmbedFn,
) -> DetectionResult:
    """Orchestrator. Loads bank_entries (load_for_concept), runs sympy_veto +
    bank_pattern + judge_concepts (bank/sympy in-thread, judge conditional-and-batched
    per R4), collects all ConceptFindings into a DetectionResult. Pure aggregation —
    the gate/merge/apply stages run downstream in done.py so this stays reusable by
    the graph grader too. Any tier exception is logged and contributes zero findings
    (soft-fail); an empty bank → sympy_veto/bank_pattern abstain, judge still runs."""
```

---

## 6. Wiring edits (EXISTING files)

### 6.1 `apollo/handlers/done.py::handle_done`
Insert a parallel stage AFTER `compute_coverage` (@396) and the rubric (@403-410),
guarded by `detector_enabled()`. When OFF the block never runs → byte-identical.

```python
# NEW — after rubric @410, before diagnostic @412
detection_outcome: MergeOutcome | None = None
if detector_enabled():
    try:
        utterances = await _student_utterances(db, attempt_id=attempt.id)  # NEW helper
        detection = await detect_misconceptions(
            db, attempt_id=attempt.id, concept_id=sess.concept_id,
            student_graph=student_graph, reference_graph=reference_graph,
            problem_text=problem.problem_text,
            student_utterances=utterances,
            judge_fn=make_openai_judge(), embed_fn=_default_embed_fn,
        )
        gated = gate_findings(detection.per_concept)
        detection_outcome = merge_detections(
            gated, centrality=compute_centrality(reference_graph))
        rubric = rubric_overall_after_penalty(rubric, detection_outcome)  # NEW rubric copy
    except Exception:
        _LOG.exception("misconception_detector_failed attempt_id=%s", int(attempt.id))
        detection_outcome = None  # soft-fail: grade proceeds unpenalized
```
- `rubric` reassignment (a NEW dict) flows into `xp_earned` (@429), `diagnostic` (@412),
  `student_response["rubric"]` (@474), and `attempt.diagnostic_report` (@437) — moving
  the LIVE band + XP a real student sees (closes D1). No mutation of the original rubric.
- Thread `detection_outcome` into `write_artifacts` (§6.2) so the artifact's
  `misconception_penalty`/`misconceptions[]` are populated and the emergent ledger feeds.
- Add module-level `_student_utterances(db, *, attempt_id)` (reads `Message.content`
  where `Message.role == "student"` — CONFIRMED role string, R6 — ordered by `turn_index`)
  and `_default_embed_fn`.

### 6.2 `apollo/handlers/artifact_writer.py::write_artifacts`
Add `detection_outcome: MergeOutcome | None = None` param; thread into
`build_llm_artifact` (and `build_graph_artifact` for parity). Signature grows one kwarg
(default None → existing callers/tests unaffected).

### 6.3 `apollo/grading/artifact_build.py::build_llm_artifact`
Add `detection_outcome: MergeOutcome | None = None`. When present and non-empty:
- `misconception_penalty = detection_outcome.misconception_penalty` (was hardcoded 0.0 @419)
- `misconceptions = list(detection_outcome.misconceptions)` (was `[]` @489)
- `composite = apply_penalty(composite=composite, outcome=detection_outcome)` (overwrites @420-423)
When None/empty → byte-identical to today (default-None guard). Do NOT touch
`build_graph_artifact`'s own penalty math (it has a real graph penalty); only accept the
param for future parity.

### 6.4 `apollo/overseer/coverage.py` — the D4 fix (separable sub-PR per spec §8)
- `_BATCH_BINARY_PROMPT` @103-104: DROP "Sign flips and algebraic rearrangements are
  equivalent." Replace with "Sign flips and direction reversals are NOT equivalent — a
  student equation with a reversed sign does not cover the reference."
- `_batch_binary_match` @125-196: for `entry_type == "equation"`, PRE-GATE each ref/student
  pair through `tiers._symbolic_equiv` (sign-exact). A pair that is NOT sign-exact-equivalent
  is forced `covered=False` regardless of the LLM verdict (the LLM may still DOWNGRADE a
  sympy-equal pair, never UPGRADE a sign-flipped one). Guarded by `detector_enabled()` so the
  fix ships with the same flag (OFF → prompt+gate unchanged; test asserts byte-identical).

### 6.5 Emergent ledger feed
`done.py` already calls `write_artifacts`, which already calls
`record_observations_from_canonical` when `emergent_misconceptions_enabled()` (@221-241).
Because §6.3 now populates `misconceptions[]` on the LLM canonical payload, the EXISTING
writer picks up the detector's rows with NO change to `store.py`. Both flags must be ON for
the ledger to receive rows (spec §6). Add one assertion test that this path writes rows.

---

## 7. Ordered TDD task DAG

Each task: RED test first (fails), GREEN minimal impl, verify (`pytest <file> -q` + the
task's assertions). `parallel_group`: same integer ⇒ distinct files, safe to run in parallel.

| id | title | depends_on | files | ‖group | model |
|----|-------|-----------|-------|:--:|:--:|
| T1 | VOs (`types.py`) + `config.py` flag/constants | — | types.py, config.py, 2 tests | 1 | sonnet |
| T2 | `centrality.py` pure graph centrality | T1 | centrality.py, test | 2 | sonnet |
| T3 | `sympy_veto.py` sign-veto (reuse `_symbolic_equiv`) | T1 | sympy_veto.py, test | 2 | sonnet |
| T4 | `bank_pattern.py` (reuse bank; EmbedFn DI) | T1 | bank_pattern.py, test | 2 | sonnet |
| T5 | `judge.py` JudgeFn/JudgeRaw + `judge_concepts` (soft-fail) | T1 | judge.py, test | 2 | sonnet |
| T6 | `gate.py` corroboration + τ_fire | T1 | gate.py, test | 2 | sonnet |
| T7 | `merge.py` severity-subtract + ceiling → MergeOutcome | T1,T2 | merge.py, test | 3 | sonnet |
| T8 | `apply.py` apply_penalty + rubric_overall_after_penalty | T1 | apply.py, test | 3 | sonnet |
| T9 | `detector.py` orchestrator (DI on judge+embed) | T2,T3,T4,T5 | detector.py, test | 4 | sonnet |
| T10 | D4 fix in `coverage.py` (prompt + SymPy pre-gate, flag-gated) | T1 | coverage.py, test_coverage* | 4 | sonnet |
| T11 | wire `build_llm_artifact` (penalty/misconceptions/composite) | T7,T8 | artifact_build.py, test | 5 | sonnet |
| T12 | wire `write_artifacts` (thread outcome) | T11 | artifact_writer.py, test | 6 | sonnet |
| T13 | wire `handle_done` (parallel stage + `_student_utterances`) | T6,T7,T8,T9,T12 | done.py, test_done* | 7 | opus |
| T14 | emergent-ledger feed assertion (both flags ON) | T13 | `apollo/handlers/tests/test_misconception_ledger_feed.py` | 8 | sonnet |
| T15 | flag-OFF byte-identical regression suite (done + artifact) | T13 | `apollo/handlers/tests/test_misconception_flag_off_golden.py` | 8 | sonnet |
| T16 | campaign replay validation harness hook + acceptance doc | T13,T14 | test + writeup to `docs/_archive/experiments/` | 9 | opus |
| T17 | reconcile `docs/architecture/apollo.md` (drift contract) | T13 | apollo.md | 9 | sonnet |

Parallel waves: {T1} → {T2,T3,T4,T5,T6} → {T7,T8} → {T9,T10} → {T11} → {T12} → {T13} →
{T14,T15} → {T16,T17}.

---

## 8. RED tests (key assertions per task)

- **T1** — `DetectionResult.is_empty` on `()`; frozen VOs reject mutation
  (`dataclasses.FrozenInstanceError`); `detector_enabled()` False when unset, True on each of
  `{"1","true","yes","on"}`; env-override of each constant parses / falls back on malformed.
- **T2** — a node with N incoming DEPENDS_ON scores higher than a leaf; PRECEDES-head vs tail
  ordering reflected; single-node graph → 1.0; every score in `[CENTRALITY_W_MIN, 1.0]`;
  **(A6) a cyclic PRECEDES subgraph does NOT raise — `compute_centrality` catches the
  `ValueError` from `topological_order` and returns a uniform/DEPENDS_ON-only fallback
  centrality instead of crashing the grade.**
- **T3** — a student equation that is a sign-flipped mutant of a bank equation → one
  `misconception` finding conf=1.0 corroborated=True; a sign-EXACT match to reference → no
  finding; a genuine algebraic rearrangement (same sign) → no finding (no over-correction);
  malformed symbolic → no finding (no crash).
- **T4** — utterance embedding within `BANK_SIM_FLOOR` of a bank entry → finding
  conf=similarity corroborated=False; below floor → none; empty bank → none; SQLite in-memory
  cosine path exercised with a stub `embed_fn` (no pgvector).
- **T5** — mocked `JudgeFn` returning valid JSON → 4-way verdicts mapped; malformed JSON →
  all-`clear` (soft-fail, no raise); `verdict_token_prob=None` handled; batched call issued
  once for N concepts.
- **T6** — sympy_veto alone → docks; bank_pattern+judge≥τ → docks corroborated; lone judge≥τ →
  `needs_clarification` not dock; judge<τ → dropped/clarification; two agreeing non-judge
  sources → dock.
- **T7** — severity = centrality×confidence; Σ clamped at `SEVERITY_CLAMP`; a central docked
  finding sets `ceiling_applied=True`; peripheral-only → `ceiling_applied=False`;
  `misconceptions[]` rows carry `canonical_key` = the bare `misc.<code>` (never re-prefixed,
  never `unkeyed:*`, A5); a docked `unkeyed:<concept_id>` finding still contributes to
  `misconception_penalty`/`ceiling_applied` but is excluded from (or key-less in) the
  `misconceptions[]` row list; `ledger_findings` = docked set.
- **T8** — `apply_penalty` subtracts + renorms; `ceiling_applied` caps below the named-band
  ceiling (0.84, scorecard-only, A4); empty outcome → input returned unchanged; rounding
  matches the inline `round(max(0.0, min(1.0, x)), 6)` replication (A8 — no cross-package
  import of `_round_like_composite`); `rubric_overall_after_penalty` reduces `overall.score`
  and recomputes `overall.letter` via `score_to_letter` (A4) — asserted separately from the
  composite ceiling.
- **T9** — orchestrator aggregates all three tiers; a tier raising → 0 findings from it, others
  still returned (soft-fail); empty bank → judge-only findings; DI judge/embed never hit OpenAI.
- **T10** — **(a)** sign-reversed equation scores `missing` regardless of a mocked LLM saying
  covered (SymPy gate); **(b)** a sign-PRESERVING rearrangement still `covered` (no
  over-correction); **(c)** flag OFF → prompt string + verdict byte-identical to today;
  **(d)** prompt no longer contains "Sign flips ... equivalent".
- **T11** — flag ON + non-empty outcome → `misconception_penalty > 0`, `misconceptions != []`,
  `composite` reduced; flag OFF / None outcome → payload byte-identical to today
  (golden-dict compare).
- **T12** — outcome threaded to `build_llm_artifact`; default-None call unchanged.
- **T13** — additive-bank persona `misconception__gdp_identity.json` (covers all beats + asserts
  `misc.includes_transfers`) → `misconception_penalty > 0` and served band < Strong; a clean
  strong control (any `strong__*.json`, e.g. `strong__gdp_identity.json`) → detector emits
  nothing, `student_response` byte-identical to flag-OFF; judge crash → HTTP 200, grade
  unpenalized; XP reflects the reduced rubric.
- **T14** — both flags ON, S1 alice/bob/cara repeat → non-zero rows in
  `apollo_misconception_observations`; idempotent re-grade adds 0.
- **T15** — flag OFF: `handle_done` + `build_llm_artifact` outputs byte-identical to a captured
  pre-change golden across strong/weak fixtures (the hard regression guard).
- **T16** — replay `v2-qa-2026-07-08` batches with detector ON: false-Strong on
  misconception-class attempts drops ≥50% vs baseline; the `strong__*.json` controls stay
  high (zero FP); the `*__net_exports_sign` family (sign case) + `misconception__gdp_identity.json`
  (additive case) are caught; conceptual-omission/clarification documented as N1/N2 misses.

---

## 9. Coverage strategy (95% patch gate)

- **Pure modules (types, config, centrality, sympy_veto, merge, apply, gate)** — 100% reachable
  with in-memory `KGGraph`/`MisconceptionEntry` fixtures and hand-built `ConceptFinding`s. No IO.
- **`bank_pattern.py`** — `EmbedFn` DI + SQLite in-memory cosine path (real
  `match_by_embedding` is pgvector-only and unit-bypassed per its docstring). Postgres path
  covered by one integration test on the Testcontainers/local-Docker harness (or exempted with
  a documented note if the pgvector fixture is unavailable — "hard to test" is NOT an exemption;
  a genuine pgvector-only line gets an integration test).
- **`judge.py`** — split: `judge_concepts` (pure, prompt-build + parse + soft-fail) is fully
  unit-tested via a stub `JudgeFn`. `make_openai_judge` (the ONE live-OpenAI + logprob
  extraction site) is the sole exemption candidate — isolate it to <15 lines so the unit
  gate covers everything else; cover its logprob-extraction branch with a fake `resp` object
  (no network) so even it clears 95%.
- **`detector.py`** — DI on both `judge_fn` and `embed_fn`; every branch (tier raises, empty
  bank, all-clear) driven by stubs.
- **Wiring (`done.py`, `artifact_build.py`, `artifact_writer.py`, `coverage.py`)** — the NEW
  lines are flag-gated; test each branch flag-ON and flag-OFF. The flag-OFF path is covered by
  T15's byte-identical goldens; the flag-ON path by T11-T14. `done.py`'s soft-fail `except`
  covered by a stub detector that raises.
- **Golden personas** — lock the real `*__net_exports_sign.json` family (sign case) and
  `misconception__gdp_identity.json` (additive-bank case) from
  `campaign/cast/personas/macroeconomics/` as regression fixtures (spec §8; A2).
- Run `pytest --cov --cov-report=xml` then
  `diff-cover coverage.xml --compare-branch=origin/staging --fail-under=95` before any PR.

---

## 10. Risks / open questions (carried from spec §9 + verification findings)

- **R1 — verdict-token logprob mechanism is UNPROVEN in this codebase.** `main_chat`
  (`_llm.py` @76-95) returns only string content; there is ZERO existing `logprobs` usage
  anywhere in the repo (grep-confirmed). The gate's "verdict-token probability" therefore
  requires a NEW call path: `make_openai_judge` must call
  `client.chat.completions.create(..., logprobs=True, top_logprobs=5)` and walk
  `resp.choices[0].logprobs.content` to find the token carrying the verdict value, then
  `exp(logprob)`. This is provider-specific and untested here. The `JudgeRaw.verdict_token_prob:
  float | None` field and the `TAU_FIRE` gate are designed so that if logprobs prove
  unavailable/unreliable, `gate.py` falls back to a verbalized-confidence field the judge also
  emits — but that fallback is Reasoning's-Razor-overconfident (spec §3), gated separately via
  `TAU_FIRE_VERBALIZED` (A1), and must be recalibrated. **Do not treat the logprob path as
  proven; validate it against a real call before trusting the strict gate.**
- **R2 — calibration transfer.** `TAU_FIRE`, `TAU_FIRE_VERBALIZED`, `SEVERITY_CLAMP`,
  `CENTRALITY` weights, and the ceiling trigger are pre-calibration defaults; they MUST be
  tuned on the `v2-qa-2026-07-08` labeled batch replay set. Expected outcome per attempt is
  NOT a persona field — it comes from the batch `replay-metrics.json`'s `band_vs_expected`
  field (or is derived from the persona's own `expected.credited` / `expected.unresolved`
  arrays; A3). Personas carry `expected.misconceptions`, `expected.credited`,
  `expected.unresolved`, `persona`, and `clarification_policy` — there is no
  `expected_band` field on a persona. Ported precision numbers do not transfer.
- **R3 — severity constants start conservative** (favor FN over FP) and are tuned against the
  strong controls; zero FP on the `strong__*.json` persona family is the hard constraint.
- **R4 — judge latency.** One batched gated LLM call per attempt; keep Tier-1 abstaining first,
  judge conditional. Acceptable per spec latency analysis.
- **R5 — bank sharpness / mutant authoring.** `sympy_veto` depends on pre-authored sign mutants
  stored as `MisconceptionEntry.trigger_phrases` prefixed `eq:` (no schema change, spec §5).
  A fuzzy bank poisons every downstream method — audit the seeded macro bank before trusting
  judge-fed-bank precision. Mutant authoring is an OFFLINE data task, not code.
- **R6 — student-utterance source (RESOLVED).** `_student_utterances` reads `Message.content`
  where `Message.role == "student"` (CONFIRMED: `apollo/knowledge_graph/store.py:811` filters
  `Message.role == "student"`; live transcript roles are exactly `{"apollo", "student"}`),
  ordered by `turn_index`. The Apollo learner turns are `role == "apollo"` (the role
  `_attempt_misconception_scores` reads). No ambiguity remains — freeze `STUDENT_ROLE = "student"`.

---

## 11. Drift contract (T17)

On landing, in the SAME commit: update `docs/architecture/apollo.md` — register the
`apollo/overseer/misconception_detector/` package under `owns:`, document the parallel
detection stage in the grading data flow, the `misconception_penalty` live-wiring into
`build_llm_artifact` + `student_response['rubric']`, the D4 coverage prompt/SymPy-gate fix,
the new `APOLLO_MISCONCEPTION_DETECTOR` flag and its interaction with
`APOLLO_EMERGENT_MISCONCEPTIONS`, and bump `last_verified` to 2026-07-08.
