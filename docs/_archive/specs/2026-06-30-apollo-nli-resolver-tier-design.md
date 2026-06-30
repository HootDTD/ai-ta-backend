---
title: "Apollo Plan 1b+ — Local NLI Resolver Tier (design spec)"
date: 2026-06-30
status: draft / ready-to-plan
area: apollo / resolution / grading
related:
  - docs/_archive/specs/2026-06-29-apollo-clarification-loop-design.md
  - docs/architecture/apollo.md
owner_doc: docs/architecture/apollo.md
---

# Apollo Plan 1b+ — Local NLI Resolver Tier

## 1. Purpose & scope

Add a **local NLI (Natural Language Inference) resolver tier** to the Apollo
graph-grading resolver. The tier resolves a student evidence node onto a
reference candidate when an NLI model **certifies that the student statement
entails the reference**, instead of relying on brittle exact-string alias
matching (Phase 1b) or blind token-overlap (`fuzzy`).

The goal is **resolver recall** (the long-open "G2" problem): correct
paraphrases that the deterministic tiers miss currently fall to the residual,
which inflates `unresolved_rate` and forces the clarification loop to ask the
student about ideas the system could have credited on its own.

**One-line contract:**

> Embeddings/lexical matching may **shortlist** candidates. NLI **certifies**
> entailment/contradiction. **Embedding similarity alone must never resolve a
> node.**

### What this is NOT
- It is **not** a replacement for the clarification loop. The clarification
  loop is the *grounding* — it asks the student about genuinely ambiguous ideas
  and lets the student decide credit. NLI auto-credits only the cases it is
  **confident** about (high entailment, low contradiction, margin, polarity
  guard). Whatever NLI cannot certify stays in the residual and flows to the
  clarification detector as before.
- It is **not** a misconception detector. NLI never resolves a node *to* a
  misconception (see §9).
- It does **not** change grade math, abstention thresholds, or the
  `normalization_confidence` ceilings (see §10).

## 2. Ground truth this builds on (read before implementing)

The resolver lives in `apollo/resolution/`. Current relevant facts:

- **Live ladder** (highest-trust first), defined in
  `apollo/resolution/candidates.py`:
  `exact 1.00 → symbolic 0.98 → derived 0.95 → alias 0.92 → clarification 0.90
  → fuzzy 0.80 → unresolved 0.00`.
  `llm 0.75` is still *listed* in `METHOD_CONFIDENCE_CAP` / `RESOLUTION_METHODS`
  but is a **dead entry** — the one-LLM-adjudication path was retired and
  `apollo/resolution/adjudication.py` was deleted. **There is no LLM fallback.**
- **`resolver.py::_content_match`** is the monolithic content-tier function:
  `type_compatible` HARD filter → `exact` → `symbolic` → `derived` → a fused
  lexical block (`alias` + `fuzzy`) that runs **misconception competition** on
  raw lexical proximity plus a per-winning-alias `polarity_screen`. It returns a
  `ScoredMatch | None`. It is **pure, synchronous, deterministic** and takes no
  injected dependencies today.
- **`resolver.py::resolve_attempt`** applies `confirmed_resolutions`
  (clarification, `node_id -> candidate_key`) as an **authoritative pre-tier**
  resolution at `clarification@0.90`, then runs `_content_match` on the rest,
  then greedy global assignment, then anything unmatched stays `unresolved`.
  `llm_calls` is always `0`.
- **`resolver.py::find_residual_nodes`** returns the nodes `_content_match` left
  unmatched. **This is the clarification detector's input.** Therefore: adding
  NLI *inside* `_content_match` automatically shrinks the residual and reduces
  the number of clarification questions — with zero changes to clarification
  code.
- **Embedding primitives already exist** in
  `apollo/clarification/embedding.py`: `CandidateEmbeddingCache` (memoized per
  `candidate_set_hash`), `candidate_surface_texts(candidate)`,
  `candidate_set_hash(candidates)`, `cosine(a, b)`, `default_embedder(texts)`,
  and the type alias `Embedder = Callable[[list[str]], list[list[float]]]`. The
  cache embeds `display_name + aliases + exact_aliases` per candidate and is keyed
  on a sha256 of candidate identity fields.
- **Node types are an exhaustive set of six** (see `tiers.py::student_surface_text`):
  `equation`, `condition`, `simplification`, `definition`, `procedure_step`,
  `variable_mapping`. There is **no** `procedure` or `concept` type — earlier
  drafts used those names; do not.
- **`Candidate`** (`candidates.py`) fields: `canonical_key`, `canon_key`,
  `node_type`, `is_misconception`, `symbolic`, `aliases`, `display_name`,
  `opposes_key`, `exact_aliases`.
- **`normalization_confidence.py`** holds `RESOLUTION_CEILING_BY_TYPE = {"equation": 1.00}`
  and `RESOLUTION_CEILING_DEFAULT = 0.75`. **`abstention.py`** holds the
  `min_normalization_confidence = 0.85` and `unresolved_rate = 0.35` gates. Both
  files are **out of scope to change** (§10).

## 3. Design decisions (the take to implement)

1. **Pure placement.** NLI is a first-class tier in the ordered ladder,
   executing **before `fuzzy`** so execution order matches cap order (always
   resolve via the most-trusted available method). This requires refactoring
   `_content_match` into an ordered tier pipeline (§8.6).
2. **NLI is references-only.** The NLI shortlist excludes
   `is_misconception` candidates. NLI never resolves a node to a misconception.
3. **Misconception competition is preserved.** Keep the fused `alias`+`fuzzy`
   competition exactly as-is. NLI slots between `alias` and `fuzzy`. Add the
   **misconception-masking guard** (§9) so NLI cannot silently credit a
   reference for a node that is actually voicing a known misconception.
4. **Embeddings shortlist, NLI certifies.** Embedding cosine (or a lexical
   fallback) only produces a top-k candidate shortlist. NLI decides. The
   embedding score never becomes a resolver confidence.
5. **Polarity guard before NLI.** A cheap, conservative pre-screen rejects
   obvious negation/direction flips before paying for an NLI classification.
   "If unsure, allow NLI" — NLI's `contradiction` label is the real arbiter.
6. **Keep the 0.75 ceiling.** Do **not** raise `RESOLUTION_CEILING_BY_TYPE` for
   conceptual node types. NLI's value is on the `unresolved_rate` axis, not the
   `normalization_confidence` axis. With the ceiling at 0.75, an `nli@0.88`
   resolution type-normalizes to `min(1.0, 0.88/0.75) = 1.0` — it can never trip
   the 0.85 abstention gate. Keeping 0.75 makes NLI **purely additive** on the
   abstention math.
7. **Reuse the embedding cache; lift it to a neutral module.** Do not rebuild
   `CandidateEmbeddingCache`/`cosine`/`candidate_surface_texts`. Because
   `apollo/clarification` already imports from `apollo/resolution`, a back-import
   would be circular — so **move the embedding primitives to a neutral module**
   (`apollo/resolution/embedding.py`) and have both `clarification` and the new
   NLI code import from there. Keep a thin re-export shim in
   `apollo/clarification/embedding.py` if needed to avoid churn.
8. **Flag-gated, default off, deterministic, CI-safe.** `APOLLO_NLI_ENABLED`
   defaults `False`. The NLI tier is inert when no adjudicator/embedder is
   injected (`None`), exactly like an unconfigured optional dependency. Unit
   tests use fakes; **no real model download in any unit test**.
9. **Authority order with clarification.** `clarification@0.90` (student
   committed an answer) outranks `nli@0.88` (machine-certified). This is already
   guaranteed by cap ordering and by clarification being an authoritative
   pre-tier resolution.

## 4. Target resolver ladder

```
type-compat (HARD)
  → exact          1.00
  → symbolic       0.98
  → derived        0.95
  → alias          0.92
  → [clarification 0.90 — authoritative pre-tier in resolve_attempt]
  → nli            0.88   ← NEW
  → fuzzy          0.80
  → unresolved     0.00
```

`nli` executes between `alias` and `fuzzy` inside the content-tier pipeline.
`clarification` remains a pre-tier override applied in `resolve_attempt`.

## 5. Files

### New
```
apollo/resolution/embedding.py              # lifted neutral embedding primitives (move from clarification)
apollo/resolution/semantic_shortlist.py     # candidate generation only
apollo/resolution/polarity.py               # polarity guard
apollo/resolution/nli_adjudicator.py        # NLIResult + Protocol + Transformers impl + normalize
apollo/resolution/nli_resolution.py         # match_nli_semantic composer (shortlist → polarity → certify)

apollo/resolution/tests/test_semantic_shortlist.py
apollo/resolution/tests/test_polarity.py
apollo/resolution/tests/test_nli_adjudicator.py
apollo/resolution/tests/test_nli_resolution.py
```

### Modified
```
apollo/resolution/candidates.py     # add "nli" to RESOLUTION_METHODS + METHOD_CONFIDENCE_CAP
apollo/resolution/resolver.py       # _content_match → tier pipeline + ResolveContext; thread nli/embedder
apollo/clarification/embedding.py   # becomes a re-export shim of apollo/resolution/embedding.py
apollo/handlers/done_grading.py     # construct + inject NLI adjudicator/embedder when APOLLO_NLI_ENABLED
config/settings.py                  # APOLLO_NLI_* flags
docs/architecture/apollo.md         # drift reconciliation (owns apollo/**)
```

No database migration is required — NLI is pure compute + config.

## 6. Config flags (`config/settings.py`)

```python
APOLLO_NLI_ENABLED: bool = False
APOLLO_NLI_MODEL_NAME: str = "FacebookAI/roberta-large-mnli"
APOLLO_NLI_DEVICE: str = "cpu"          # "cpu" | "cuda" | int device index
APOLLO_NLI_TOP_K: int = 5               # shortlist size handed to NLI
APOLLO_NLI_MIN_ENTAILMENT: float = 0.87
APOLLO_NLI_MAX_CONTRADICTION: float = 0.10
APOLLO_NLI_MIN_MARGIN: float = 0.15     # entailment − max(neutral, contradiction)
```

Keep disabled until the live probe (§14) verifies it.

## 7. Method registry & confidence (`candidates.py`)

```python
RESOLUTION_METHODS = (
    "exact", "symbolic", "derived", "alias", "clarification",
    "nli",            # NEW — between clarification and fuzzy
    "fuzzy", "llm", "unresolved",
)

METHOD_CONFIDENCE_CAP = {
    "exact": 1.00, "symbolic": 0.98, "derived": 0.95, "alias": 0.92,
    "clarification": 0.90,
    "nli": 0.88,      # NEW
    "fuzzy": 0.80, "llm": 0.75, "unresolved": 0.00,
}
```

**Do not change `RESOLUTION_CEILING_BY_TYPE`** in
`normalization_confidence.py` (decision §3.6).

NLI **applicability set** (node types the tier attempts) —
use the real `NodeType` literals:
```python
NLI_NODE_TYPES = frozenset({
    "procedure_step", "condition", "definition",
    "variable_mapping", "simplification",
})
# Excluded: "equation" (has exact/symbolic/derived). No numeric type exists.
```

## 8. Component specs

### 8.1 Embedding primitives — `apollo/resolution/embedding.py`

Move (not copy) `Embedder`, `default_embedder`, `candidate_surface_texts`,
`candidate_set_hash`, `cosine`, `CandidateEmbeddingCache` here from
`apollo/clarification/embedding.py`. Replace the old file with a re-export shim:

```python
# apollo/clarification/embedding.py
from apollo.resolution.embedding import (  # noqa: F401
    CandidateEmbeddingCache, Embedder, candidate_set_hash,
    candidate_surface_texts, cosine, default_embedder,
)
```

This keeps clarification working unchanged and gives the NLI shortlist a
dependency-clean import.

### 8.2 Semantic shortlist — `apollo/resolution/semantic_shortlist.py`

A **retriever**, never a resolver. Returns the top-k reference candidates for
NLI to certify. Its score is a ranking signal only — never a confidence.

```python
@dataclass(frozen=True)
class SemanticCandidate:
    candidate: Candidate          # whole Candidate; downstream needs node_type/text
    text: str                     # the reference surface form scored
    score: float                  # ranking signal ONLY (never a resolver confidence)
    source: Literal["lexical", "embedding"]

def shortlist_semantic_candidates(
    student_node: Node,
    reference_candidates: tuple[Candidate, ...],   # already type-compat, MISCONCEPTIONS EXCLUDED
    *,
    top_k: int = 5,
    embedder: Embedder | None = None,              # None → lexical fallback
    cache: CandidateEmbeddingCache | None = None,
) -> list[SemanticCandidate]:
    ...
```

- Student side: reuse `student_surface_text(node)` (the same string the
  alias/fuzzy tiers compare — keeps the ladder consistent).
- Candidate side: each candidate's surface forms = `candidate_surface_texts(c)`.
- Scoring:
  - `embedder is None` → **lexical MVP**: token-overlap (Jaccard or
    `token_set_ratio`-style), `source="lexical"`. No model → CI-safe.
  - `embedder` given → **embedding**: cosine of the batched student vector
    against the cached candidate vectors, `source="embedding"`. Use the
    `CandidateEmbeddingCache` so candidate vectors are embedded once.
- Rank `(-score, candidate.canonical_key)` — identical tiebreak to
  `match_fuzzy_all` so re-runs are byte-identical. Return the first `top_k`.

### 8.3 Polarity guard — `apollo/resolution/polarity.py`

Cheap deterministic pre-screen. Conservative: only rejects on a high-confidence
polarity conflict; everything ambiguous passes to NLI.

```python
@dataclass(frozen=True)
class PolarityDecision:
    allowed: bool
    reason: Literal["same_or_unknown", "negation_mismatch", "direction_mismatch"]

def polarity_allows_match(student_text: str, reference_text: str) -> PolarityDecision:
    ...
```

Two detectors, no model:
1. **Negation XOR** over a closed marker set
   `{not, no, n't, never, cannot, can't, doesn't, isn't, won't, without,
   neither, nor}`. If exactly one side is negated around a shared content word →
   `negation_mismatch`, `allowed=False`. Skip morphological `in-/im-` prefixes
   (too noisy — let NLI handle them).
2. **Antonym/direction pairs** — a small curated `_ANTONYM_PAIRS` constant
   (`increases/decreases`, `rises/falls`, `compressible/incompressible`,
   `constant/varying`, `conserved/not conserved`, `higher/lower`, `more/less`,
   …). Opposite poles for the same quantity → `direction_mismatch`,
   `allowed=False`.
3. Else → `same_or_unknown`, `allowed=True`.

**This module is the single source of truth for the negation/antonym lexicon.**
The existing lexical `competition.polarity_screen` (alias-bound) stays as-is for
now; a later refactor may have it delegate here. Do **not** duplicate the
lexicon in two files.

### 8.4 NLI adjudicator — `apollo/resolution/nli_adjudicator.py`

NLI framing: **Premise = student node text, Hypothesis = reference node text.**
Accept on the student **entailing** the reference (the student expressed at
least the reference idea — the correct, conservative direction for credit).

```python
@dataclass(frozen=True)
class NLIResult:
    label: Literal["entailment", "contradiction", "neutral"]
    entailment: float
    contradiction: float
    neutral: float
    model_name: str

class NLIAdjudicator(Protocol):
    def classify(self, premise: str, hypothesis: str) -> NLIResult: ...

class TransformersNLIAdjudicator:
    def __init__(self, model_name: str, device: str | int | None = None):
        self.model_name = model_name
        self.device = device
        self._pipe = None

    def _load(self):
        if self._pipe is None:
            from transformers import pipeline
            # top_k=None returns all class scores (return_all_scores is deprecated).
            self._pipe = pipeline(
                "text-classification", model=self.model_name,
                device=self.device, top_k=None,
            )
        return self._pipe

    def classify(self, premise: str, hypothesis: str) -> NLIResult:
        pipe = self._load()
        # Sentence-pair form; the pipeline applies the model's pair template.
        raw = pipe({"text": premise, "text_pair": hypothesis}, truncation=True)
        return normalize_nli_output(raw, self.model_name)
```

`normalize_nli_output` maps the model's label set to `NLIResult`. For
`FacebookAI/roberta-large-mnli` the model `id2label` is
`{0: CONTRADICTION, 1: NEUTRAL, 2: ENTAILMENT}` — normalize case-insensitively
and do not hard-code index order (read labels from the returned dicts).

`FakeNLIAdjudicator(scripted: dict[tuple[str, str], NLIResult])` for tests —
**no real model in unit tests.**

### 8.5 NLI resolver composer — `apollo/resolution/nli_resolution.py`

```python
@dataclass(frozen=True)
class NLIParams:
    top_k: int
    min_entailment: float
    max_contradiction: float
    min_margin: float

def match_nli_semantic(
    student_node: Node,
    reference_candidates: tuple[Candidate, ...],   # type-compat; misconceptions excluded
    *,
    nli: NLIAdjudicator,
    embedder: Embedder | None,
    cache: CandidateEmbeddingCache | None,
    params: NLIParams,
) -> ScoredMatch | None:
    text = student_surface_text(student_node)
    if not text or student_node.node_type not in NLI_NODE_TYPES:
        return None

    passed: list[tuple[SemanticCandidate, NLIResult]] = []
    for sc in shortlist_semantic_candidates(
        student_node, reference_candidates,
        top_k=params.top_k, embedder=embedder, cache=cache,
    ):
        pol = polarity_allows_match(text, sc.text)
        if not pol.allowed:
            _LOG.info("nli_polarity_reject reason=%s key=%s", pol.reason, sc.candidate.canonical_key)
            continue
        r = nli.classify(premise=text, hypothesis=sc.text)
        margin = r.entailment - max(r.neutral, r.contradiction)
        if (r.label == "entailment"
                and r.entailment >= params.min_entailment
                and r.contradiction <= params.max_contradiction
                and margin >= params.min_margin):
            passed.append((sc, r))
        elif r.contradiction > params.max_contradiction:
            # NLI flags a likely misconception/contradiction — do NOT resolve here.
            _LOG.info("nli_contradiction_signal key=%s c=%.3f", sc.candidate.canonical_key, r.contradiction)

    if not passed:
        return None

    # Ambiguity guard: if two candidates pass, require a margin between the top
    # two entailment scores — otherwise unresolved (do not guess between them).
    passed.sort(key=lambda p: -p[1].entailment)
    if len(passed) >= 2 and (passed[0][1].entailment - passed[1][1].entailment) < params.min_margin:
        _LOG.info("nli_ambiguous top2=%.3f,%.3f", passed[0][1].entailment, passed[1][1].entailment)
        return None

    sc, _ = passed[0]
    return ScoredMatch(student_node.node_id, sc.candidate, "nli", METHOD_CONFIDENCE_CAP["nli"])
```

### 8.6 Tier pipeline refactor + wiring (`resolver.py`)

Decompose `_content_match` into an ordered list of pure tier callables, each
`(node, type_ok, ctx) -> ScoredMatch | None`:

```python
@dataclass(frozen=True)
class ResolveContext:
    fuzzy_threshold: float
    symbolic_mappings: dict[str, str]
    nli: NLIAdjudicator | None = None
    embedder: Embedder | None = None
    cache: CandidateEmbeddingCache | None = None
    nli_params: NLIParams | None = None

def _content_match(node, candidates, *, ctx) -> ScoredMatch | None:
    type_ok = tuple(c for c in candidates if type_compatible(node.node_type, c))
    if not type_ok:
        return None
    for tier in (_tier_exact, _tier_symbolic, _tier_derived,
                 _tier_alias, _tier_nli, _tier_fuzzy):
        hit = tier(node, type_ok, ctx)
        if hit is not None:
            return hit
    return None
```

- `_tier_alias` and `_tier_fuzzy` must preserve the **exact current behavior**
  of the fused lexical block, including misconception competition and the
  per-winning-alias `polarity_screen`. Splitting them is allowed only if the
  misconception guardrail is preserved (§9). Keeping the alias+fuzzy competition
  fused inside a single combined tier function is acceptable if cleaner — the
  only hard requirement is that `nli` executes between the alias-level resolve
  and the fuzzy-level resolve.
- `_tier_nli` returns `None` immediately when `ctx.nli is None` (inert/CI-safe).
  When active it calls `match_nli_semantic` with
  `reference_candidates = tuple(c for c in type_ok if not c.is_misconception)`.
- Thread `ResolveContext` through `resolve_attempt` and `find_residual_nodes`.
  `resolve_attempt` gains optional `nli`, `embedder`, `cache`, `nli_params`
  params (default `None`) — mirroring how `confirmed_resolutions` is already an
  optional injected input. **Backward compatibility:** every existing caller
  that omits the NLI inputs gets identical behavior (the tier is inert).
- `find_residual_nodes` also accepts the context. Because it reuses
  `_content_match`, passing a live NLI context there makes the **clarification
  detector see NLI-resolved nodes as non-residual** → fewer student questions.
  If chat-turn latency is a concern, the chat caller may pass `nli=None` to skip
  NLI in the hot path while grading-time `resolve_attempt` still runs it. Make
  this an explicit caller decision; default grading path runs NLI when enabled.
- Live construction happens in `done_grading.py`: when `APOLLO_NLI_ENABLED`,
  build `TransformersNLIAdjudicator(...)` + the embedder + a process-lived
  `CandidateEmbeddingCache`, and pass them into `resolve_attempt`.

## 9. Misconception safety (the masking guard)

Inserting NLI before `fuzzy` introduces exactly one risk: a node that matches a
**misconception trigger at fuzzy level** *and* semantically entails the correct
reference could be credited by NLI before the fuzzy-level misconception
competition runs — masking a misconception the student actually voiced.

**Guard:** before accepting an NLI entailment for a node, run the existing cheap
misconception-trigger lexical check (the same alias/fuzzy match the competition
uses, restricted to `is_misconception` candidates). If the node **strongly
matches a misconception trigger**, abstain from the NLI resolution (return
`None`) and let `_tier_fuzzy` + `apply_misconception_competition` arbitrate.

This is ~5 lines and removes the only soundness regression. The live probe
(§14) must confirm misconception-detection rate does not drop versus the
pre-NLI baseline.

## 10. Abstention / normalization_confidence — DO NOT CHANGE

- Keep `RESOLUTION_CEILING_BY_TYPE = {"equation": 1.00}` and
  `RESOLUTION_CEILING_DEFAULT = 0.75`.
- Keep `min_normalization_confidence = 0.85` and `unresolved_rate = 0.35`.
- Rationale: with the 0.75 ceiling, `nli@0.88` type-normalizes to
  `min(1.0, 0.88/0.75) = 1.0` for conceptual nodes — it can never trip the nc
  gate. NLI's benefit is on `unresolved_rate` (it moves residual nodes into
  `resolved`), so it can only *reduce* abstention. Raising the ceiling would
  re-open a regression for zero benefit to NLI's purpose.

## 11. Audit logging

Every NLI decision must be auditable. Emit (structured log and/or persisted run
diagnostics) per resolved-via-NLI node:

```json
{
  "method": "nli",
  "nli_label": "entailment",
  "nli_entailment": 0.93,
  "nli_neutral": 0.05,
  "nli_contradiction": 0.02,
  "nli_model_name": "FacebookAI/roberta-large-mnli",
  "polarity_allowed": true,
  "polarity_reason": "same_or_unknown",
  "semantic_candidates_considered": 5,
  "semantic_candidate_margin": 0.21
}
```

A **high-contradiction** outcome must be logged as a misconception signal — it
must never silently become `unresolved` with no record.

## 12. Composition with the clarification loop

- NLI auto-credits confident paraphrases; the clarification loop handles the
  ambiguous residue. They compose through `find_residual_nodes`: enabling NLI
  shrinks the residual → the clarification detector asks fewer, better-targeted
  questions.
- Authority order: `clarification@0.90` (student-confirmed) outranks
  `nli@0.88` (machine-certified). Already guaranteed by cap ordering and by
  clarification being an authoritative pre-tier override in `resolve_attempt`.
- This spec assumes the clarification loop (`feat/apollo-clarification-loop`) is
  the base. **Branch this work off a clean clarification HEAD**, not off a dirty
  working tree (the tree may contain unrelated uncommitted work). Confirm
  clarification's merge target (staging) to pin the base commit.

## 13. Tests & coverage gate

Patch coverage of changed lines must be **≥95%** (`diff-cover` vs
`origin/staging`). No real model download in unit tests.

- **Polarity** (`test_polarity.py`): `pressure does not increase ≠ pressure
  increases` (negation), `velocity decreases ≠ velocity increases` (direction),
  unknown polarity → allowed.
- **Semantic shortlist** (`test_semantic_shortlist.py`): lexical mode pure
  tests; embedding mode with a `FakeEmbedder`; deterministic ordering;
  misconceptions excluded from the input.
- **NLI adjudicator** (`test_nli_adjudicator.py`): `normalize_nli_output`
  label mapping (case-insensitive, label-not-index); `FakeNLIAdjudicator`.
- **NLI resolution** (`test_nli_resolution.py`): paraphrased procedure →
  resolves via `nli`; negated procedure → rejected by polarity before NLI;
  contradiction → unresolved + logged; neutral → unresolved; two close
  entailment candidates → ambiguous unresolved; misconception-trigger node →
  masking guard abstains; equation/`is_misconception` candidates never entered.
- **Resolver integration** (`test_resolver.py`): existing
  exact/symbolic/derived/alias/fuzzy/clarification behavior **unchanged** when
  `ctx.nli is None`; `nli` tier resolves between alias and fuzzy when active;
  `resolve_attempt`/`find_residual_nodes` backward-compatible without NLI inputs.

Verification commands:
```bash
pytest apollo/resolution/tests -q
pytest apollo/grading/tests -q
pytest apollo/graph_compare/tests -q
pytest apollo/ -q
ruff check apollo scripts
mypy apollo
pytest --cov --cov-report=xml -q
diff-cover coverage.xml --compare-branch=origin/staging --fail-under=95
```

## 14. Live acceptance (probe, NLI enabled)

Macro probe:
```bash
./.venv/Scripts/python.exe scripts/run_macro_probe.py --skip-embed --skip-mining --tag .nli
```
Pass criteria:
- `unresolved_rate` decreases on ≥1 previously under-resolved strong/partial
  attempt.
- No weak attempt becomes falsely certified.
- No grade-math byte-identity regression.
- No new `normalization_confidence` abstention bug.
- Every NLI resolution carries the §11 audit data.
- **Misconception-detection rate does not drop** versus the pre-NLI baseline
  (validates the §9 guard).

Fluids probe:
- Still routes correctly.
- Strong `unresolved_rate` improves if the missing nodes are paraphrastic.
- Weak attempts remain rejected/abstained where appropriate.

## 15. Drift contract

`docs/architecture/apollo.md` owns `apollo/**`. In the same change that lands the
code, update it to document: the `nli` resolver tier and its 0.88 cap, the
shortlist→polarity→certify rule, the references-only + masking-guard invariants,
the `NLI_NODE_TYPES` set, the new config flags, and the embedding-primitive move
to `apollo/resolution/embedding.py`. Bump `last_verified`.

## 16. Commit plan

```
1. refactor(apollo): lift embedding primitives to apollo/resolution/embedding.py (+ shim)
2. feat(apollo): add polarity guard
3. feat(apollo): add local NLI adjudicator (Transformers + Fake)
4. feat(apollo): add semantic shortlist
5. feat(apollo): add NLI resolver tier (match_nli_semantic)
6. refactor(apollo): _content_match → ordered tier pipeline + ResolveContext
7. feat(apollo): register nli method (cap 0.88) + wire tier into resolve_attempt/find_residual_nodes
8. feat(apollo): construct + inject NLI in done_grading behind APOLLO_NLI_ENABLED
9. docs(apollo): reconcile apollo.md for the NLI resolver tier
```

## 17. Open decisions to confirm before/at planning

- **Base commit:** confirm clarification's merge to `staging`, then branch NLI
  off that (or off `feat/apollo-clarification-loop` HEAD) from a clean checkout.
- **NLI in the chat hot path:** run NLI inside `find_residual_nodes` (fewer
  clarification questions, +latency) vs grading-only (cheaper chat). Default:
  grading-time on; chat detector may pass `nli=None`.
- **Shortlist scorer for v1:** ship lexical-only first (zero new infra) and add
  the embedding scorer once the cache move is in, or wire embeddings from the
  start. Either satisfies the contract; lexical-first de-risks the probe.

## 18. Sources & references

NLI task framing (entailment / contradiction / neutral):
- SNLI — Bowman, Angeli, Potts, Manning (2015), *A large annotated corpus for
  learning natural language inference*. Project:
  https://nlp.stanford.edu/projects/snli/ — Paper: https://arxiv.org/abs/1508.05326
- MultiNLI (the corpus `roberta-large-mnli` is fine-tuned on) — Williams,
  Nangia, Bowman (2018): https://cims.nyu.edu/~sbowman/multinli/

Local model inference:
- Hugging Face Transformers — Pipelines (local inference + batching):
  https://huggingface.co/docs/transformers/main_classes/pipelines
  Text-classification task: https://huggingface.co/docs/transformers/tasks/sequence_classification
- Candidate local NLI model card — `FacebookAI/roberta-large-mnli`:
  https://huggingface.co/FacebookAI/roberta-large-mnli
  (labels: `CONTRADICTION`, `NEUTRAL`, `ENTAILMENT`)

Strict-JSON option (only relevant if a cheap LLM fallback is added later):
- OpenAI Structured Outputs:
  https://platform.openai.com/docs/guides/structured-outputs
