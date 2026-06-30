# Apollo NLI Resolver Tier — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a local NLI resolver tier that credits correct paraphrases the exact/alias/fuzzy tiers miss (the "G2" recall problem), behind a calibrated, default-gated flag, without inflating grades.

**Architecture:** NLI runs as a **recall-only fallback** — it fires only on nodes the existing fused lexical (alias+fuzzy) tier leaves unmatched, so it can never mask the lexical misconception competition. An embedding shortlist proposes reference candidates; a small local NLI model (`cross-encoder/nli-deberta-v3-small`) *certifies* entailment; a polarity pre-screen + a **semantic misconception veto** guard soundness. The tier is wired into both grading (`done_grading.py`) and — with a thread-executor offload — the chat clarification detector (`turn.py`). A calibration harness sweeps thresholds against a mined+hand-authored dev set; the flag only ships default-ON if precision clears ≥0.95.

**Tech Stack:** Python 3.12, `transformers` + `torch` + `sentencepiece` (new deps, user-confirmed), `cross-encoder/nli-deberta-v3-small`, the existing `CandidateEmbeddingCache` (text-embedding-3-large), pytest, diff-cover.

## Global Constraints

- **Base branch:** `origin/staging` (clarification merged via PR #71, commit `106f48a` — `apollo/clarification/`, `find_residual_nodes`, and the `clarification@0.90` cap are all present). Work branch: `feat/apollo-nli-resolver-tier`, cut off `origin/staging`. PRs back into `staging`.
- **Patch coverage:** ≥95% on changed lines — `pytest --cov --cov-report=xml` then `diff-cover coverage.xml --compare-branch=origin/staging --fail-under=95`. Base is valid (staging has clarification).
- **No real model in unit tests.** Every unit test uses `FakeNLIAdjudicator` / `FakeEmbedder`. The `transformers`/`torch` model-loading lines carry `# pragma: no cover` with a documented exemption (untestable without prod infra).
- **CI safety with default-ON:** the flag helper defaults OFF when the env var is unset (standard idiom), and a conftest autouse fixture forces it OFF across the suite so CI never downloads a model. "Default ON" (decision Q7) is realized in Task 12 *only after calibration clears the bar*, paired with that CI guard.
- **Determinism:** pin `transformers`/`torch`/`sentencepiece`; the resolver's byte-identical-replay guarantee holds only within the pinned environment. The flag default is OFF, so existing replay tests are unaffected.
- **Model/dependency choice (confirmed):** `cross-encoder/nli-deberta-v3-small` via `transformers` text-classification pipeline. Do not substitute `roberta-large-mnli`.
- **Precision gate (decision Q6):** ≥0.95 precision on the dev set is the hard condition for enabling. If unmet, flag stays OFF and we report it — do not ship default-ON below the bar.
- **Drift contract:** `docs/architecture/apollo.md` owns `apollo/**`. Reconcile it in the same change that lands code (Task 12) and bump `last_verified`.
- **Deviations from the source spec** (`docs/_archive/specs/2026-06-30-apollo-nli-resolver-tier-design.md`), all deliberate, all from the 4-agent review:
  1. NLI is a **recall-only fallback after** the fused lexical tier, **not** "between alias and fuzzy" (the alias+fuzzy block is one fused misconception competition — `resolver.py:111-135` — and cannot be split without regressing it).
  2. The per-candidate `min_margin` gate is **removed** (mathematically dead: `entailment≥0.87 ⇒ margin≥0.74`). A separate `ambiguity_margin` governs only top-2 disambiguation.
  3. The misconception guard is **semantic** (NLI-classify misconception candidates and veto), not lexical-only.
  4. `variable_mapping` is **excluded** from `NLI_NODE_TYPES` (its surface is a bare `term` — degenerate for NLI).
  5. `ScoredMatch`'s 4th field is `score` (positional construction), not `confidence`.
  6. The `candidates.py` method registry change lands **first** (Task 1), before the composer/pipeline tasks.
  7. NLI threading uses an optional `nli_ctx` param (backward-compatible) rather than replacing `_content_match`'s signature with a mandatory `ctx`.

---

## File Structure

**New (`apollo/resolution/`):**
- `embedding.py` — lifted neutral embedding primitives (moved from `apollo/clarification/embedding.py`).
- `nli_config.py` — env flag reader + `NLIParams` defaults.
- `polarity.py` — deterministic polarity/negation/antonym pre-screen.
- `nli_adjudicator.py` — `NLIResult`, `NLIAdjudicator` Protocol, `normalize_nli_output`, `TransformersNLIAdjudicator`, `FakeNLIAdjudicator`.
- `semantic_shortlist.py` — embedding/lexical candidate shortlist (retriever only).
- `nli_resolution.py` — `NLIContext` + `match_nli_semantic` composer (shortlist → polarity → certify → veto).
- tests for each of the above under `apollo/resolution/tests/`.

**Modified:**
- `apollo/resolution/candidates.py` — register `nli` method/cap + `NLI_NODE_TYPES`.
- `apollo/resolution/resolver.py` — thread `nli_ctx`; recall-only NLI fallback inside `_content_match`.
- `apollo/clarification/embedding.py` — becomes a re-export shim.
- `apollo/handlers/done_grading.py` — construct + inject NLI at grading time.
- `apollo/clarification/turn.py` + `apollo/handlers/chat.py` — chat-path NLI with executor offload.
- `requirements.txt` — add `transformers`, `torch`, `sentencepiece`.
- `apollo/conftest.py` (or nearest) — autouse guard forcing the flag OFF in tests.
- `docs/architecture/apollo.md` — drift reconciliation (Task 12).
- `scripts/apollo_nli_calibrate.py` — calibration harness (Task 11).

---

## Task 1: Register the `nli` resolution method + applicability set

**Files:**
- Modify: `apollo/resolution/candidates.py:24-46`
- Test: `apollo/resolution/tests/test_candidates.py`

**Interfaces:**
- Produces: `METHOD_CONFIDENCE_CAP["nli"] == 0.88`; `"nli"` in `RESOLUTION_METHODS` between `"clarification"` and `"fuzzy"`; `NLI_NODE_TYPES: frozenset[str]` = `{"procedure_step","condition","definition","simplification"}` (excludes `equation` and `variable_mapping`).

- [ ] **Step 1: Write the failing test**

```python
# apollo/resolution/tests/test_candidates.py
from apollo.resolution.candidates import (
    METHOD_CONFIDENCE_CAP, RESOLUTION_METHODS, NLI_NODE_TYPES,
)

def test_nli_method_registered_between_clarification_and_fuzzy():
    methods = list(RESOLUTION_METHODS)
    assert methods.index("clarification") < methods.index("nli") < methods.index("fuzzy")
    assert METHOD_CONFIDENCE_CAP["nli"] == 0.88

def test_nli_node_types_exclude_equation_and_variable_mapping():
    assert NLI_NODE_TYPES == frozenset(
        {"procedure_step", "condition", "definition", "simplification"}
    )
    assert "equation" not in NLI_NODE_TYPES
    assert "variable_mapping" not in NLI_NODE_TYPES
```

- [ ] **Step 2: Run to verify failure** — `pytest apollo/resolution/tests/test_candidates.py -k nli -q` → FAIL (ImportError: `NLI_NODE_TYPES`).

- [ ] **Step 3: Implement** in `candidates.py`:

```python
RESOLUTION_METHODS: tuple[str, ...] = (
    "exact", "symbolic", "derived", "alias", "clarification",
    "nli",            # recall-only fallback (cap 0.88) — see nli_resolution.py
    "fuzzy", "llm", "unresolved",
)

METHOD_CONFIDENCE_CAP: dict[str, float] = {
    "exact": 1.00, "symbolic": 0.98, "derived": 0.95, "alias": 0.92,
    "clarification": 0.90,
    "nli": 0.88,
    "fuzzy": 0.80, "llm": 0.75, "unresolved": 0.00,
}

# Node types the NLI tier attempts. Excludes `equation` (exact/symbolic/derived
# already cover it) and `variable_mapping` (surface is a bare `term` — degenerate
# for sentence-level inference).
NLI_NODE_TYPES: frozenset[str] = frozenset(
    {"procedure_step", "condition", "definition", "simplification"}
)
```

- [ ] **Step 4: Run to verify pass** — same command → PASS.
- [ ] **Step 5: Commit** — `feat(apollo): register nli resolution method (cap 0.88) + applicability set`

---

## Task 2: Lift embedding primitives to `apollo/resolution/embedding.py` + shim

**Files:**
- Create: `apollo/resolution/embedding.py`
- Modify: `apollo/clarification/embedding.py` (replace body with re-export shim)
- Test: `apollo/resolution/tests/test_embedding.py` (new), existing `apollo/clarification/tests/test_embedding.py` must still pass

**Interfaces:**
- Produces (from `apollo.resolution.embedding`): `Embedder`, `default_embedder`, `candidate_surface_texts`, `candidate_set_hash`, `cosine`, `CandidateEmbeddingCache` — identical signatures to today's `apollo/clarification/embedding.py`.

- [ ] **Step 1: Write the failing test**

```python
# apollo/resolution/tests/test_embedding.py
def test_primitives_importable_from_resolution():
    from apollo.resolution.embedding import (
        CandidateEmbeddingCache, Embedder, candidate_set_hash,
        candidate_surface_texts, cosine, default_embedder,
    )
    assert cosine([1.0, 0.0], [1.0, 0.0]) == 1.0

def test_clarification_shim_reexports_same_objects():
    from apollo.resolution.embedding import cosine as res_cosine
    from apollo.clarification.embedding import cosine as clar_cosine
    assert res_cosine is clar_cosine
```

- [ ] **Step 2: Run to verify failure** — `pytest apollo/resolution/tests/test_embedding.py -q` → FAIL (module not found).

- [ ] **Step 3: Implement** — `git mv apollo/clarification/embedding.py apollo/resolution/embedding.py`, fix the module docstring to drop the clarification-specific framing, then recreate `apollo/clarification/embedding.py` as:

```python
"""Re-export shim — embedding primitives now live in apollo.resolution.embedding
(neutral module so the NLI resolver can import them without a clarification cycle)."""
from apollo.resolution.embedding import (  # noqa: F401
    CandidateEmbeddingCache, Embedder, candidate_set_hash,
    candidate_surface_texts, cosine, default_embedder,
)
```

Confirm `apollo/clarification/__init__.py`'s `__all__`/imports still resolve (they import from `apollo.clarification.embedding`, which the shim satisfies).

- [ ] **Step 4: Run to verify pass** — `pytest apollo/resolution/tests/test_embedding.py apollo/clarification/tests/test_embedding.py -q` → PASS.
- [ ] **Step 5: Commit** — `refactor(apollo): lift embedding primitives to apollo/resolution/embedding.py (+ shim)`

---

## Task 3: NLI config — env flag + `NLIParams`

**Files:**
- Create: `apollo/resolution/nli_config.py`
- Test: `apollo/resolution/tests/test_nli_config.py`

**Interfaces:**
- Produces: `NLI_ENABLED_FLAG: str = "APOLLO_NLI_ENABLED"`; `nli_enabled() -> bool` (default OFF when unset); `NLIParams` (frozen dataclass) with fields `top_k:int=5, min_entailment:float=0.87, max_contradiction:float=0.10, ambiguity_margin:float=0.10, misconception_veto_entailment:float=0.80`; `load_nli_params() -> NLIParams` (env overrides, else defaults); `NLI_MODEL_NAME:str="cross-encoder/nli-deberta-v3-small"`; `NLI_DEVICE:str="cpu"`.

- [ ] **Step 1: Write the failing test**

```python
# apollo/resolution/tests/test_nli_config.py
from apollo.resolution.nli_config import nli_enabled, load_nli_params, NLIParams, NLI_ENABLED_FLAG

def test_flag_defaults_off_when_unset(monkeypatch):
    monkeypatch.delenv(NLI_ENABLED_FLAG, raising=False)
    assert nli_enabled() is False

def test_flag_truthy_values(monkeypatch):
    for v in ("1", "true", "YES"):
        monkeypatch.setenv(NLI_ENABLED_FLAG, v)
        assert nli_enabled() is True

def test_params_defaults_and_env_override(monkeypatch):
    monkeypatch.delenv("APOLLO_NLI_MIN_ENTAILMENT", raising=False)
    assert load_nli_params().min_entailment == 0.87
    monkeypatch.setenv("APOLLO_NLI_MIN_ENTAILMENT", "0.93")
    assert load_nli_params().min_entailment == 0.93
    assert isinstance(load_nli_params(), NLIParams)
```

- [ ] **Step 2: Run to verify failure** → FAIL (module not found).

- [ ] **Step 3: Implement** — mirror the `learner_update.py` flag idiom:

```python
# apollo/resolution/nli_config.py
from __future__ import annotations
import os
from dataclasses import dataclass

NLI_ENABLED_FLAG: str = "APOLLO_NLI_ENABLED"
NLI_MODEL_NAME: str = "cross-encoder/nli-deberta-v3-small"
NLI_DEVICE: str = "cpu"

def nli_enabled() -> bool:
    return os.environ.get(NLI_ENABLED_FLAG, "").lower() in ("1", "true", "yes")

@dataclass(frozen=True)
class NLIParams:
    top_k: int = 5
    min_entailment: float = 0.87
    max_contradiction: float = 0.10
    ambiguity_margin: float = 0.10            # top-2 entailment separation (NOT a per-candidate gate)
    misconception_veto_entailment: float = 0.80

def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return float(raw) if raw is not None else default

def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw is not None else default

def load_nli_params() -> NLIParams:
    return NLIParams(
        top_k=_env_int("APOLLO_NLI_TOP_K", 5),
        min_entailment=_env_float("APOLLO_NLI_MIN_ENTAILMENT", 0.87),
        max_contradiction=_env_float("APOLLO_NLI_MAX_CONTRADICTION", 0.10),
        ambiguity_margin=_env_float("APOLLO_NLI_AMBIGUITY_MARGIN", 0.10),
        misconception_veto_entailment=_env_float("APOLLO_NLI_MISC_VETO_ENT", 0.80),
    )
```

- [ ] **Step 4: Run to verify pass** → PASS.
- [ ] **Step 5: Commit** — `feat(apollo): add NLI config flag + NLIParams`

---

## Task 4: Polarity guard

**Files:**
- Create: `apollo/resolution/polarity.py`
- Test: `apollo/resolution/tests/test_polarity.py`

**Interfaces:**
- Produces: `PolarityDecision` (frozen: `allowed: bool`, `reason: str`); `polarity_allows_match(student_text: str, reference_text: str) -> PolarityDecision`. `reason ∈ {"same_or_unknown","negation_mismatch","direction_mismatch"}`. Conservative: rejects only on high-confidence conflict.

**Notes (review B6):** extends the antonym set with inverse-proportionality and domain pairs (laminar/turbulent, isothermal/adiabatic, subsonic/supersonic, elastic/inelastic, compressible/incompressible, conserved/"not conserved", constant/varying); skips morphological `in-/im-` prefixes; treats double-negation as ambiguous (passes to NLI). This module is the single source of truth for the NLI lexicon — do **not** duplicate `competition.py`'s `polarity_screen`.

- [ ] **Step 1: Write the failing test**

```python
# apollo/resolution/tests/test_polarity.py
from apollo.resolution.polarity import polarity_allows_match

def test_negation_mismatch_rejected():
    d = polarity_allows_match("pressure does not increase", "pressure increases")
    assert d.allowed is False and d.reason == "negation_mismatch"

def test_direction_mismatch_rejected():
    d = polarity_allows_match("velocity decreases downstream", "velocity increases downstream")
    assert d.allowed is False and d.reason == "direction_mismatch"

def test_inverse_proportional_rejected():
    d = polarity_allows_match("pressure is proportional to volume",
                              "pressure is inversely proportional to volume")
    assert d.allowed is False and d.reason == "direction_mismatch"

def test_litotes_allowed_unknown_polarity():
    d = polarity_allows_match("there is no change in density", "density is constant")
    assert d.allowed is True and d.reason == "same_or_unknown"

def test_neutral_text_allowed():
    assert polarity_allows_match("the fluid is incompressible",
                                 "incompressible flow").allowed is True
```

- [ ] **Step 2: Run to verify failure** → FAIL (module not found).

- [ ] **Step 3: Implement** (`polarity.py`):

```python
from __future__ import annotations
from dataclasses import dataclass

_NEGATION = {"not", "no", "n't", "never", "cannot", "can't", "doesn't",
             "isn't", "won't", "without", "neither", "nor"}

_ANTONYM_PAIRS: tuple[tuple[str, str], ...] = (
    ("increase", "decrease"), ("increases", "decreases"),
    ("rises", "falls"), ("rise", "fall"), ("higher", "lower"),
    ("more", "less"), ("faster", "slower"), ("up", "down"),
    ("compressible", "incompressible"), ("constant", "varying"),
    ("laminar", "turbulent"), ("isothermal", "adiabatic"),
    ("subsonic", "supersonic"), ("elastic", "inelastic"),
    ("conserved", "unconserved"), ("appreciate", "depreciate"),
    ("surplus", "deficit"), ("expansionary", "contractionary"),
    ("inflation", "deflation"),
)

@dataclass(frozen=True)
class PolarityDecision:
    allowed: bool
    reason: str

def _tokens(text: str) -> set[str]:
    return {w.strip(".,;:!?").lower() for w in text.split()}

def _negation_count(toks: set[str], raw: str) -> int:
    n = sum(1 for t in toks if t in _NEGATION)
    # contractions split off "n't"
    n += sum(1 for w in raw.lower().split() if w.endswith("n't"))
    return n

def polarity_allows_match(student_text: str, reference_text: str) -> PolarityDecision:
    s, r = _tokens(student_text), _tokens(reference_text)
    # 1. Negation XOR (single-negation only; double negation is ambiguous -> allow).
    sn, rn = _negation_count(s, student_text), _negation_count(r, reference_text)
    if (sn % 2) != (rn % 2):
        return PolarityDecision(False, "negation_mismatch")
    # 2. Inverse-proportionality: one side qualifies "proportional" with "inverse(ly)".
    s_inv = ("proportional" in s) and bool(s & {"inversely", "inverse"})
    r_inv = ("proportional" in r) and bool(r & {"inversely", "inverse"})
    if ("proportional" in s and "proportional" in r) and (s_inv != r_inv):
        return PolarityDecision(False, "direction_mismatch")
    # 3. Antonym poles for a shared quantity.
    for left, right in _ANTONYM_PAIRS:
        if left in s and left in r:   # same pole -> not discriminating
            continue
        if right in s and right in r:
            continue
        if (left in s and right in r) or (right in s and left in r):
            return PolarityDecision(False, "direction_mismatch")
    return PolarityDecision(True, "same_or_unknown")
```

- [ ] **Step 4: Run to verify pass** → PASS.
- [ ] **Step 5: Commit** — `feat(apollo): add NLI polarity guard`

---

## Task 5: NLI adjudicator (Protocol + Transformers impl + Fake)

**Files:**
- Create: `apollo/resolution/nli_adjudicator.py`
- Test: `apollo/resolution/tests/test_nli_adjudicator.py`

**Interfaces:**
- Produces: `NLIResult` (frozen: `label:str`, `entailment:float`, `contradiction:float`, `neutral:float`, `model_name:str`); `NLIAdjudicator` Protocol with `classify(premise:str, hypothesis:str) -> NLIResult`; `normalize_nli_output(raw, model_name) -> NLIResult`; `TransformersNLIAdjudicator`; `FakeNLIAdjudicator(scripted: dict[tuple[str,str], NLIResult])`.
- **Framing:** Premise = student text, Hypothesis = reference text. Accept on student entailing reference.

- [ ] **Step 1: Write the failing test**

```python
# apollo/resolution/tests/test_nli_adjudicator.py
from apollo.resolution.nli_adjudicator import normalize_nli_output, NLIResult, FakeNLIAdjudicator

def test_normalize_maps_by_label_case_insensitive_not_index():
    # deberta-v3 order is contradiction,entailment,neutral — must map by name.
    raw = [
        {"label": "ENTAILMENT", "score": 0.91},
        {"label": "neutral", "score": 0.06},
        {"label": "Contradiction", "score": 0.03},
    ]
    r = normalize_nli_output(raw, "m")
    assert r.label == "entailment"
    assert (r.entailment, r.neutral, r.contradiction) == (0.91, 0.06, 0.03)
    assert r.model_name == "m"

def test_normalize_handles_nested_single_input_list():
    raw = [[{"label": "neutral", "score": 0.8},
            {"label": "entailment", "score": 0.1},
            {"label": "contradiction", "score": 0.1}]]
    assert normalize_nli_output(raw, "m").label == "neutral"

def test_fake_adjudicator_returns_scripted():
    want = NLIResult("entailment", 0.9, 0.05, 0.05, "fake")
    fake = FakeNLIAdjudicator({("p", "h"): want})
    assert fake.classify(premise="p", hypothesis="h") is want
```

- [ ] **Step 2: Run to verify failure** → FAIL (module not found).

- [ ] **Step 3: Implement** (`nli_adjudicator.py`):

```python
from __future__ import annotations
from dataclasses import dataclass
from typing import Protocol

@dataclass(frozen=True)
class NLIResult:
    label: str            # "entailment" | "neutral" | "contradiction"
    entailment: float
    contradiction: float
    neutral: float
    model_name: str

class NLIAdjudicator(Protocol):
    def classify(self, premise: str, hypothesis: str) -> NLIResult: ...

def normalize_nli_output(raw, model_name: str) -> NLIResult:
    """Map a transformers text-classification (top_k=None) output to NLIResult.
    Maps by LABEL NAME (case-insensitive), never by index — deberta-v3 and
    roberta-mnli use different index orders."""
    rows = raw[0] if raw and isinstance(raw[0], list) else raw
    scores = {str(d["label"]).lower(): float(d["score"]) for d in rows}
    ent = scores.get("entailment", 0.0)
    con = scores.get("contradiction", 0.0)
    neu = scores.get("neutral", 0.0)
    label = max(("entailment", ent), ("neutral", neu), ("contradiction", con),
                key=lambda kv: kv[1])[0]
    return NLIResult(label, ent, con, neu, model_name)

class FakeNLIAdjudicator:
    def __init__(self, scripted: dict[tuple[str, str], NLIResult]):
        self._scripted = scripted
    def classify(self, premise: str, hypothesis: str) -> NLIResult:
        return self._scripted[(premise, hypothesis)]

class TransformersNLIAdjudicator:
    def __init__(self, model_name: str, device: str | int | None = None):
        self.model_name = model_name
        self.device = device
        self._pipe = None

    def _load(self):  # pragma: no cover - requires a model download; covered by the live probe (Task 12)
        if self._pipe is None:
            from transformers import pipeline
            self._pipe = pipeline(
                "text-classification", model=self.model_name,
                device=self.device, top_k=None,
            )
        return self._pipe

    def classify(self, premise: str, hypothesis: str) -> NLIResult:  # pragma: no cover - real-model path
        pipe = self._load()
        raw = pipe({"text": premise, "text_pair": hypothesis}, truncation=True)
        return normalize_nli_output(raw, self.model_name)
```

- [ ] **Step 4: Run to verify pass** → PASS.
- [ ] **Step 5: Commit** — `feat(apollo): add local NLI adjudicator (deberta-v3 + Fake)`

---

## Task 6: Semantic shortlist (retriever, never a resolver)

**Files:**
- Create: `apollo/resolution/semantic_shortlist.py`
- Test: `apollo/resolution/tests/test_semantic_shortlist.py`

**Interfaces:**
- Consumes: `apollo.resolution.embedding.{CandidateEmbeddingCache, Embedder, candidate_surface_texts}`; `apollo.resolution.tiers.student_surface_text`.
- Produces: `SemanticCandidate` (frozen: `candidate: Candidate`, `text: str`, `score: float`, `source: str`); `shortlist_semantic_candidates(student_node, candidates, *, top_k=5, embedder=None, cache=None) -> list[SemanticCandidate]`. Embedding when `embedder` given (decision Q3), lexical token-overlap fallback when `None`. Caller pre-filters (type-compat; misconceptions excluded for the credit shortlist). Rank `(-score, candidate.canonical_key)`; first `top_k`.

- [ ] **Step 1: Write the failing test**

```python
# apollo/resolution/tests/test_semantic_shortlist.py
from apollo.resolution.semantic_shortlist import shortlist_semantic_candidates
from apollo.resolution.candidates import Candidate
# (build Node + Candidate fixtures via existing test helpers)

def _cand(key, name):
    return Candidate(key, -1, "definition", False, None, (), name, None, ())

def test_lexical_fallback_ranks_by_overlap(make_def_node):
    node = make_def_node("density stays constant throughout the pipe")
    cands = (_cand("def.const_density", "density is constant"),
             _cand("def.unrelated", "energy is conserved"))
    out = shortlist_semantic_candidates(node, cands, top_k=2, embedder=None)
    assert out[0].candidate.canonical_key == "def.const_density"
    assert out[0].source == "lexical"

def test_embedding_mode_uses_fake_embedder(make_def_node):
    node = make_def_node("incompressible flow")
    cands = (_cand("def.incompress", "incompressibility"),)
    fake = lambda texts: [[1.0, 0.0] for _ in texts]  # all identical -> cosine 1.0
    out = shortlist_semantic_candidates(node, cands, top_k=1, embedder=fake)
    assert out[0].source == "embedding" and out[0].score == 1.0

def test_deterministic_tiebreak(make_def_node):
    node = make_def_node("x")
    cands = (_cand("def.b", "x"), _cand("def.a", "x"))
    out = shortlist_semantic_candidates(node, cands, top_k=2, embedder=None)
    assert [c.candidate.canonical_key for c in out] == ["def.a", "def.b"]  # equal score -> key asc
```

- [ ] **Step 2: Run to verify failure** → FAIL.

- [ ] **Step 3: Implement** (`semantic_shortlist.py`):

```python
from __future__ import annotations
from dataclasses import dataclass

from apollo.ontology.nodes import Node
from apollo.resolution.candidates import Candidate
from apollo.resolution.embedding import (
    CandidateEmbeddingCache, Embedder, candidate_surface_texts, cosine,
)
from apollo.resolution.tiers import student_surface_text

@dataclass(frozen=True)
class SemanticCandidate:
    candidate: Candidate
    text: str
    score: float
    source: str            # "lexical" | "embedding"

def _overlap(a: str, b: str) -> float:
    sa = {w.strip(".,;:!?").lower() for w in a.split()}
    sb = {w.strip(".,;:!?").lower() for w in b.split()}
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)

def shortlist_semantic_candidates(
    student_node: Node,
    candidates: tuple[Candidate, ...],
    *,
    top_k: int = 5,
    embedder: Embedder | None = None,
    cache: CandidateEmbeddingCache | None = None,
) -> list[SemanticCandidate]:
    text = student_surface_text(student_node)
    if not text or not candidates:
        return []
    scored: list[SemanticCandidate] = []
    if embedder is None:
        for c in candidates:
            best_text, best = "", 0.0
            for surf in candidate_surface_texts(c):
                s = _overlap(text, surf)
                if s >= best:
                    best, best_text = s, surf
            scored.append(SemanticCandidate(c, best_text, best, "lexical"))
    else:
        sv = embedder([text])[0]
        vecs = (cache or CandidateEmbeddingCache()).vectors_for(candidates, embedder=embedder)
        for c in candidates:
            best_text, best = "", 0.0
            surfaces = candidate_surface_texts(c)
            for surf, v in zip(surfaces, vecs.get(c.canonical_key, []), strict=False):
                s = cosine(sv, v)
                if s >= best:
                    best, best_text = s, surf
            scored.append(SemanticCandidate(c, best_text, best, "embedding"))
    scored.sort(key=lambda sc: (-sc.score, sc.candidate.canonical_key))
    return scored[:top_k]
```

- [ ] **Step 4: Run to verify pass** → PASS (add a `make_def_node` fixture to `conftest.py` if not present, reusing existing node builders).
- [ ] **Step 5: Commit** — `feat(apollo): add semantic shortlist for NLI tier`

---

## Task 7: NLI resolver composer + semantic misconception veto

**Files:**
- Create: `apollo/resolution/nli_resolution.py`
- Test: `apollo/resolution/tests/test_nli_resolution.py`

**Interfaces:**
- Consumes: Tasks 1,3,4,5,6 + `apollo.resolution.structural.ScoredMatch` + `candidates.METHOD_CONFIDENCE_CAP`/`NLI_NODE_TYPES`.
- Produces: `NLIContext` (frozen: `nli: NLIAdjudicator | None=None`, `embedder: Embedder | None=None`, `cache: CandidateEmbeddingCache | None=None`, `params: NLIParams = NLIParams()`); `match_nli_semantic(student_node, type_ok, *, ctx) -> ScoredMatch | None`.
- **Logic:** gate on `node_type ∈ NLI_NODE_TYPES` and `ctx.nli is not None`. Split `type_ok` into refs (non-misc) and miscs. **Veto first:** if any shortlisted misconception is entailed ≥ `misconception_veto_entailment` (polarity-allowed), return `None`. Otherwise certify references: polarity screen → `classify` → accept if `label=="entailment" and entailment≥min_entailment and contradiction≤max_contradiction` AND a positive content-overlap floor (≥1 shared content token). Ambiguity guard: if top-2 accepted entailments are within `ambiguity_margin`, return `None`. Emit `ScoredMatch(node_id, candidate, "nli", METHOD_CONFIDENCE_CAP["nli"])` (4th arg positional = `score`).

- [ ] **Step 1: Write the failing test**

```python
# apollo/resolution/tests/test_nli_resolution.py
from apollo.resolution.nli_resolution import match_nli_semantic, NLIContext
from apollo.resolution.nli_adjudicator import FakeNLIAdjudicator, NLIResult
from apollo.resolution.nli_config import NLIParams
from apollo.resolution.candidates import Candidate

ENT = NLIResult("entailment", 0.95, 0.02, 0.03, "fake")
CON = NLIResult("contradiction", 0.02, 0.95, 0.03, "fake")
NEU = NLIResult("neutral", 0.10, 0.05, 0.85, "fake")

def _ref(key, name): return Candidate(key, -1, "definition", False, None, (), name, None, ())
def _misc(key, name): return Candidate(key, -1, "definition", True, None, (name,), name, None, ())

def test_paraphrase_resolves_via_nli(make_def_node):
    node = make_def_node("density stays constant throughout")
    ref = _ref("def.const_density", "density is constant")
    fake = FakeNLIAdjudicator({("density stays constant throughout", "density is constant"): ENT})
    ctx = NLIContext(nli=fake, embedder=None, cache=None, params=NLIParams())
    m = match_nli_semantic(node, (ref,), ctx=ctx)
    assert m is not None and m.method == "nli" and m.candidate.canonical_key == "def.const_density"

def test_misconception_entailment_vetoes(make_def_node):
    node = make_def_node("pressure rises as speed rises")
    ref = _ref("def.tradeoff", "pressure falls as speed rises")
    misc = _misc("misc.same_dir", "pressure rises as speed rises")
    fake = FakeNLIAdjudicator({
        ("pressure rises as speed rises", "pressure falls as speed rises"): ENT,   # would wrongly credit
        ("pressure rises as speed rises", "pressure rises as speed rises"): ENT,   # misconception entailed
    })
    ctx = NLIContext(nli=fake, params=NLIParams())
    assert match_nli_semantic(node, (ref, misc), ctx=ctx) is None   # vetoed

def test_contradiction_unresolved(make_def_node):
    node = make_def_node("energy is not conserved")
    ref = _ref("def.energy", "energy is conserved")
    fake = FakeNLIAdjudicator({("energy is not conserved", "energy is conserved"): CON})
    # polarity rejects before NLI even fires -> None
    assert match_nli_semantic(node, (ref,), ctx=NLIContext(nli=fake, params=NLIParams())) is None

def test_neutral_unresolved(make_def_node):
    node = make_def_node("the pipe is made of steel")
    ref = _ref("def.const_density", "density is constant")
    fake = FakeNLIAdjudicator({("the pipe is made of steel", "density is constant"): NEU})
    assert match_nli_semantic(node, (ref,), ctx=NLIContext(nli=fake, params=NLIParams())) is None

def test_equation_and_variable_mapping_never_entered(make_equation_node):
    node = make_equation_node("P + rho*g*h = const")
    ref = _ref("eq.bernoulli", "bernoulli")
    fake = FakeNLIAdjudicator({})   # must never be consulted
    assert match_nli_semantic(node, (ref,), ctx=NLIContext(nli=fake, params=NLIParams())) is None

def test_inert_without_adjudicator(make_def_node):
    node = make_def_node("anything")
    assert match_nli_semantic(node, (), ctx=NLIContext(nli=None)) is None
```

- [ ] **Step 2: Run to verify failure** → FAIL.

- [ ] **Step 3: Implement** (`nli_resolution.py`):

```python
from __future__ import annotations
import logging
from dataclasses import dataclass, field

from apollo.ontology.nodes import Node
from apollo.resolution.candidates import Candidate, METHOD_CONFIDENCE_CAP, NLI_NODE_TYPES
from apollo.resolution.embedding import CandidateEmbeddingCache, Embedder
from apollo.resolution.nli_adjudicator import NLIAdjudicator, NLIResult
from apollo.resolution.nli_config import NLIParams
from apollo.resolution.polarity import polarity_allows_match
from apollo.resolution.semantic_shortlist import shortlist_semantic_candidates, SemanticCandidate
from apollo.resolution.structural import ScoredMatch
from apollo.resolution.tiers import student_surface_text

_LOG = logging.getLogger(__name__)

@dataclass(frozen=True)
class NLIContext:
    nli: NLIAdjudicator | None = None
    embedder: Embedder | None = None
    cache: CandidateEmbeddingCache | None = None
    params: NLIParams = field(default_factory=NLIParams)

def _content_tokens(t: str) -> set[str]:
    return {w.strip(".,;:!?").lower() for w in t.split() if len(w) > 2}

def match_nli_semantic(student_node: Node, type_ok: tuple[Candidate, ...], *, ctx: NLIContext) -> ScoredMatch | None:
    if ctx.nli is None:
        return None
    text = student_surface_text(student_node)
    if not text or student_node.node_type not in NLI_NODE_TYPES:
        return None
    p = ctx.params
    refs = tuple(c for c in type_ok if not c.is_misconception)
    miscs = tuple(c for c in type_ok if c.is_misconception)

    # --- Semantic veto: student voicing a (paraphrased) misconception? ---
    for sc in shortlist_semantic_candidates(student_node, miscs, top_k=p.top_k,
                                             embedder=ctx.embedder, cache=ctx.cache):
        if not polarity_allows_match(text, sc.text).allowed:
            continue
        r = ctx.nli.classify(premise=text, hypothesis=sc.text)
        if r.label == "entailment" and r.entailment >= p.misconception_veto_entailment:
            _LOG.info("nli_misconception_veto key=%s ent=%.3f", sc.candidate.canonical_key, r.entailment)
            return None

    # --- Certify references ---
    passed: list[tuple[SemanticCandidate, NLIResult]] = []
    for sc in shortlist_semantic_candidates(student_node, refs, top_k=p.top_k,
                                            embedder=ctx.embedder, cache=ctx.cache):
        if not polarity_allows_match(text, sc.text).allowed:
            continue
        if not (_content_tokens(text) & _content_tokens(sc.text)):   # positive-overlap floor (review B6)
            continue
        r = ctx.nli.classify(premise=text, hypothesis=sc.text)
        if (r.label == "entailment" and r.entailment >= p.min_entailment
                and r.contradiction <= p.max_contradiction):
            passed.append((sc, r))
        elif r.contradiction > p.max_contradiction:
            _LOG.info("nli_contradiction_signal key=%s c=%.3f", sc.candidate.canonical_key, r.contradiction)

    if not passed:
        return None
    passed.sort(key=lambda pr: (-pr[1].entailment, pr[0].candidate.canonical_key))
    if len(passed) >= 2 and (passed[0][1].entailment - passed[1][1].entailment) < p.ambiguity_margin:
        _LOG.info("nli_ambiguous top2=%.3f,%.3f", passed[0][1].entailment, passed[1][1].entailment)
        return None
    sc, _ = passed[0]
    return ScoredMatch(student_node.node_id, sc.candidate, "nli", METHOD_CONFIDENCE_CAP["nli"])
```

- [ ] **Step 4: Run to verify pass** → `pytest apollo/resolution/tests/test_nli_resolution.py -q` → PASS.
- [ ] **Step 5: Commit** — `feat(apollo): add NLI resolver composer with semantic misconception veto`

---

## Task 8: Wire the recall-only NLI fallback into the resolver

**Files:**
- Modify: `apollo/resolution/resolver.py` (`_content_match`, `find_residual_nodes`, `resolve_attempt`)
- Modify: `apollo/resolution/tests/test_resolver.py:576` (the direct `_content_match` caller)
- Test: `apollo/resolution/tests/test_resolver.py` (new cases)

**Interfaces:**
- Consumes: `NLIContext`, `match_nli_semantic`.
- Produces: optional `nli_ctx: NLIContext | None = None` on `_content_match`, `find_residual_nodes`, `resolve_attempt`. When `nli_ctx is None` (or `nli_ctx.nli is None`), behavior is **byte-identical** to today. NLI fires only after the fused lexical block yields no candidate.

- [ ] **Step 1: Write the failing test**

```python
# additions to apollo/resolution/tests/test_resolver.py
from apollo.resolution.nli_resolution import NLIContext
from apollo.resolution.nli_adjudicator import FakeNLIAdjudicator, NLIResult

def test_nli_fallback_resolves_when_lexical_misses(make_student_graph_one_def):
    graph, node = make_student_graph_one_def("density stays constant throughout")
    ref = ...  # Candidate def.const_density with NO matching alias (so lexical misses)
    ENT = NLIResult("entailment", 0.95, 0.02, 0.03, "fake")
    fake = FakeNLIAdjudicator({("density stays constant throughout", "density is constant"): ENT})
    ctx = NLIContext(nli=fake)
    res = resolve_attempt(graph, (ref,), nli_ctx=ctx)
    rn = res.resolved[0]
    assert rn.resolution == "resolved" and rn.method == "nli" and rn.confidence == 0.88

def test_nli_none_is_byte_identical(make_student_graph_one_def):
    graph, _ = make_student_graph_one_def("density stays constant throughout")
    ref = ...  # same Candidate
    assert resolve_attempt(graph, (ref,)) == resolve_attempt(graph, (ref,), nli_ctx=NLIContext(nli=None))
```

Also update line 576: `_content_match(node, (cand,), fuzzy_threshold=0.9, symbolic_mappings={})` stays valid (we keep those kwargs); add an explicit `nli_ctx=None` only if a new assertion needs it.

- [ ] **Step 2: Run to verify failure** → FAIL.

- [ ] **Step 3: Implement** — edit `_content_match` to accept `nli_ctx` and add the fallback where it currently returns `None` on empty lexical:

```python
def _content_match(node, candidates, *, fuzzy_threshold, symbolic_mappings, nli_ctx=None):
    type_ok = tuple(c for c in candidates if type_compatible(node.node_type, c))
    if not type_ok:
        return None
    # exact / symbolic / derived  ... (unchanged) ...
    # fused lexical block ... (unchanged) ...
    lexical = list(by_candidate.values())
    if lexical:
        return apply_misconception_competition(surface, lexical)
    # Recall-only NLI fallback: ONLY when the fused lexical tier found nothing,
    # so it can never mask a lexical-level misconception (it never ran one here).
    if nli_ctx is not None and nli_ctx.nli is not None:
        from apollo.resolution.nli_resolution import match_nli_semantic
        return match_nli_semantic(node, type_ok, ctx=nli_ctx)
    return None
```

Thread `nli_ctx=nli_ctx` through `find_residual_nodes` and `resolve_attempt` (both gain `nli_ctx: NLIContext | None = None`, default None → unchanged behavior; pass it into the two `_content_match` call sites at resolver.py:151 and :213).

- [ ] **Step 4: Run to verify pass** → `pytest apollo/resolution/tests/test_resolver.py -q` → PASS (all existing cases green; the two new ones green).
- [ ] **Step 5: Commit** — `feat(apollo): wire recall-only NLI fallback into resolve_attempt/find_residual_nodes`

---

## Task 9: Grading-time injection (`done_grading.py`)

**Files:**
- Modify: `apollo/handlers/done_grading.py` (import + a module-level lazy adjudicator + the `resolve_attempt` call at :192-198)
- Test: `apollo/handlers/tests/test_done_grading_nli.py` (new)

**Interfaces:**
- Produces: a module-private `_nli_context() -> NLIContext | None` reading `nli_enabled()`; threads `nli_ctx=_nli_context()` into the `resolve_attempt` call. The `TransformersNLIAdjudicator` is built once (module-level lazy singleton) and reused.

- [ ] **Step 1: Write the failing test** (inject a fake via patching the singleton builder; never load a model):

```python
# apollo/handlers/tests/test_done_grading_nli.py
import apollo.handlers.done_grading as dg
from apollo.resolution.nli_resolution import NLIContext

def test_nli_context_none_when_flag_off(monkeypatch):
    monkeypatch.delenv("APOLLO_NLI_ENABLED", raising=False)
    assert dg._nli_context() is None

def test_nli_context_built_when_flag_on(monkeypatch):
    monkeypatch.setenv("APOLLO_NLI_ENABLED", "1")
    monkeypatch.setattr(dg, "_build_adjudicator", lambda: object())   # avoid model load
    ctx = dg._nli_context()
    assert isinstance(ctx, NLIContext) and ctx.nli is not None
```

- [ ] **Step 2: Run to verify failure** → FAIL.

- [ ] **Step 3: Implement** — add to `done_grading.py`:

```python
from apollo.resolution.nli_config import nli_enabled, load_nli_params, NLI_MODEL_NAME, NLI_DEVICE
from apollo.resolution.nli_resolution import NLIContext
from apollo.resolution.embedding import CandidateEmbeddingCache, default_embedder

_NLI_ADJUDICATOR = None        # process-lived singleton
_NLI_CACHE = CandidateEmbeddingCache()

def _build_adjudicator():  # pragma: no cover - constructs the real model (Task 12 probe)
    from apollo.resolution.nli_adjudicator import TransformersNLIAdjudicator
    return TransformersNLIAdjudicator(NLI_MODEL_NAME, device=NLI_DEVICE)

def _nli_context() -> NLIContext | None:
    if not nli_enabled():
        return None
    global _NLI_ADJUDICATOR
    if _NLI_ADJUDICATOR is None:
        _NLI_ADJUDICATOR = _build_adjudicator()
    return NLIContext(nli=_NLI_ADJUDICATOR, embedder=default_embedder,
                      cache=_NLI_CACHE, params=load_nli_params())
```

Then change the call at :192:

```python
resolution = resolve_attempt(
    student_graph, inputs.candidates,
    confirmed_resolutions=confirmed_resolutions,
    fuzzy_threshold=0.9, symbolic_mappings=inputs.symbolic_mappings,
    nli_ctx=_nli_context(),
)
```

- [ ] **Step 4: Run to verify pass** → PASS.
- [ ] **Step 5: Commit** — `feat(apollo): inject NLI at grading time behind APOLLO_NLI_ENABLED`

---

## Task 10: Chat hot-path with thread offload (`turn.py` + `chat.py`)

**Files:**
- Modify: `apollo/clarification/turn.py` (`run_clarification_detection` gains optional `nli_ctx`; offload residual to an executor)
- Modify: `apollo/handlers/chat.py` (build `nli_ctx` reusing the existing embedder+cache + the shared adjudicator; pass it in)
- Test: `apollo/clarification/tests/test_turn_nli.py` (new)

**Interfaces:**
- Produces: `run_clarification_detection(..., nli_ctx: NLIContext | None = None)`. When provided, the residual computation runs via `loop.run_in_executor` so the CPU model never blocks the event loop. Fail-safe unchanged (any failure → `[]`). Reuse `done_grading`'s adjudicator singleton (shared) so the model loads once per process.

- [ ] **Step 1: Write the failing test**

```python
# apollo/clarification/tests/test_turn_nli.py
import asyncio
from apollo.clarification.turn import run_clarification_detection
from apollo.resolution.nli_resolution import NLIContext
from apollo.resolution.nli_adjudicator import FakeNLIAdjudicator

def test_residual_runs_with_nli_ctx_via_executor(fake_db, def_nodes, candidates_no_alias):
    # node paraphrases a reference; with NLI it resolves -> NOT residual -> no probe asked
    fake = FakeNLIAdjudicator({...})  # entailment for the paraphrase
    ctx = NLIContext(nli=fake)
    hints = asyncio.run(run_clarification_detection(
        db=fake_db, parsed_nodes=def_nodes, candidates=candidates_no_alias,
        symbolic_mappings={}, embedder=lambda t: [[1.0] for _ in t],
        cache=None, attempt_id=1, session_id=1, user_id="u", search_space_id=1,
        concept_id=None, asked_turn=0, nli_ctx=ctx,
    ))
    assert hints == []   # NLI resolved it -> nothing to clarify

def test_nli_ctx_none_keeps_current_behavior(fake_db, def_nodes, candidates_no_alias):
    # without NLI the paraphrase is residual -> a probe is produced (existing behavior)
    ...
```

- [ ] **Step 2: Run to verify failure** → FAIL.

- [ ] **Step 3: Implement** — in `turn.py`, add the param and offload:

```python
import asyncio
from apollo.resolution.nli_resolution import NLIContext

async def run_clarification_detection(*, ..., asked_turn: int,
                                      nli_ctx: NLIContext | None = None) -> list[str]:
    if not parsed_nodes or not candidates:
        return []
    try:
        loop = asyncio.get_running_loop()
        residual = await loop.run_in_executor(
            None,
            lambda: find_residual_nodes(parsed_nodes, candidates,
                                        symbolic_mappings=symbolic_mappings, nli_ctx=nli_ctx),
        )
        # ... rest unchanged ...
```

In `chat.py`, where `run_clarification_detection` is called, build the context (reuse the request's `embedder`/`_CLARIFICATION_CACHE`, share `done_grading._nli_context()`'s adjudicator) and pass `nli_ctx=...` only when `nli_enabled()`. Add a latency budget: cap residual nodes considered (e.g. top-N) and document the per-turn cost.

- [ ] **Step 4: Run to verify pass** → PASS.
- [ ] **Step 5: Commit** — `feat(apollo): run NLI in chat clarification detector with executor offload`

---

## Task 11: Calibration harness + dev set

**Files:**
- Create: `scripts/apollo_nli_calibrate.py`
- Create: `apollo/resolution/tests/data/nli_dev_set.jsonl` (mined + hand-authored; committed)
- Create: `apollo/resolution/calibration.py` (pure sweep logic, unit-tested)
- Test: `apollo/resolution/tests/test_nli_calibration.py`

**Interfaces:**
- Produces: `sweep_thresholds(labeled: list[LabeledPair], classify_fn) -> SweepReport` where `LabeledPair=(premise, hypothesis, gold ∈ {"entailment","neutral","contradiction"})`; `SweepReport` lists, per `(min_entailment, max_contradiction)` grid point, precision/recall/F1 for the "credit" decision (predict entailment ⇒ credit). `best_operating_point(report, min_precision=0.95) -> NLIParams | None` (None if the bar is unreachable).
- **Dev-set sourcing (decision Q5):** mine paraphrase/residual nodes from the macro (OpenStax) + fluids probe corpora under `docs/experiments/.../` and `scripts/run_macro_probe.py` outputs, hand-label them, AND add hand-authored hard cases (partials, inverse-proportionality, paraphrased misconceptions). Target ≥150 labeled pairs with explicit hard-negative coverage.

- [ ] **Step 1: Write the failing test** (sweep logic is pure — unit-test with a fake classifier + tiny labeled set):

```python
# apollo/resolution/tests/test_nli_calibration.py
from apollo.resolution.calibration import sweep_thresholds, best_operating_point

def test_precision_gate_selects_high_threshold():
    labeled = [("a","a","entailment"), ("b","c","neutral"), ("d","d","entailment")]
    def fake(premise, hypothesis):
        # high entailment only when premise==hypothesis
        return 0.99 if premise == hypothesis else 0.50
    report = sweep_thresholds(labeled, fake)
    params = best_operating_point(report, min_precision=0.95)
    assert params is not None and params.min_entailment >= 0.6

def test_returns_none_when_bar_unreachable():
    labeled = [("a","b","neutral")]   # only a false-positive available
    def fake(p, h): return 0.99
    assert best_operating_point(sweep_thresholds(labeled, fake), min_precision=0.95) is None
```

- [ ] **Step 2: Run to verify failure** → FAIL.

- [ ] **Step 3: Implement** `calibration.py` (pure precision/recall sweep over a threshold grid, returns the highest-recall point meeting the precision floor) and `scripts/apollo_nli_calibrate.py` (loads the JSONL dev set, builds a `TransformersNLIAdjudicator`, runs `sweep_thresholds`, prints the PR table + the selected `NLIParams`, and exits non-zero if `best_operating_point(... , 0.95) is None`). Mark the model-construction line in the script `# pragma: no cover`. Author `nli_dev_set.jsonl` (mined + hard cases) as described.

- [ ] **Step 4: Run to verify pass** → `pytest apollo/resolution/tests/test_nli_calibration.py -q` → PASS. Then run the real sweep manually: `./.venv/Scripts/python.exe scripts/apollo_nli_calibrate.py apollo/resolution/tests/data/nli_dev_set.jsonl` and record the PR table + selected thresholds in the PR description.
- [ ] **Step 5: Commit** — `feat(apollo): NLI threshold calibration harness + dev set`

---

## Task 12: Live probe, enable decision, CI guard, docs drift

**Files:**
- Modify: `apollo/resolution/nli_config.py` (flip the in-code default to ON **iff** calibration cleared ≥0.95; bake the calibrated thresholds as the new defaults)
- Create/Modify: `apollo/conftest.py` — autouse fixture forcing `APOLLO_NLI_ENABLED=0` for the suite (opt-in tests re-enable via monkeypatch)
- Modify: `requirements.txt` — add `transformers>=4.44,<5`, `torch>=2.2`, `sentencepiece`
- Modify: `docs/architecture/apollo.md` — drift reconciliation
- Test: `apollo/conftest.py` guard verified by the suite staying green with no model download

**Interfaces:** none new — this task gates and documents.

- [ ] **Step 1: Add deps** to `requirements.txt`; install into `.venv`. Add the conftest autouse guard:

```python
# apollo/conftest.py
import pytest
@pytest.fixture(autouse=True)
def _force_nli_off(monkeypatch):
    monkeypatch.setenv("APOLLO_NLI_ENABLED", "0")
```

- [ ] **Step 2: Run the live probe** (real model) — `./.venv/Scripts/python.exe scripts/run_macro_probe.py --skip-embed --skip-mining --tag .nli` with `APOLLO_NLI_ENABLED=1`, and the fluids probe. Record per the spec §14 pass criteria: `unresolved_rate` drops on ≥1 under-resolved strong/partial attempt; no weak attempt falsely certified; grade-math byte-identity holds where NLI is off; misconception-detection rate does not drop; every NLI resolution carries audit data.

- [ ] **Step 3: Enable decision (the Q6 gate).** If Task 11's `best_operating_point(..., 0.95)` returned `NLIParams` AND the live probe passes: set those thresholds as `NLIParams` defaults and flip the in-code default ON:

```python
def nli_enabled() -> bool:
    return os.environ.get(NLI_ENABLED_FLAG, "true").lower() in ("1", "true", "yes")
```

  If the bar was NOT met: leave the default OFF, write the failing numbers into the PR description and `docs/_archive/experiments/`, and STOP — do not ship default-ON below ≥0.95 precision (Global Constraints).

- [ ] **Step 4: Reconcile `docs/architecture/apollo.md`** — document the `nli` tier (cap 0.88, recall-only fallback after the fused lexical tier), the shortlist→polarity→certify→veto rule, references-only + semantic-veto invariants, `NLI_NODE_TYPES`, the new flags/params, the embedding-primitive move, and the chat-path executor offload. Bump `last_verified` to today.

- [ ] **Step 5: Full verification + commit.**

```bash
pytest apollo/ -q
ruff check apollo scripts
mypy apollo
pytest --cov --cov-report=xml -q
diff-cover coverage.xml --compare-branch=origin/staging --fail-under=95   # or clarification merge-base until staging has it
```

Commit — `feat(apollo): enable NLI tier (calibrated) + deps + docs drift` (or `…keep NLI disabled — calibration below 0.95` if the bar was unmet).

---

## Self-Review

**Spec coverage:** every §-section maps to a task — §6 flags→T3; §7 registry→T1; §8.1 embedding move→T2; §8.2 shortlist→T6; §8.3 polarity→T4; §8.4 adjudicator→T5; §8.5 composer→T7; §8.6 wiring→T8/T9; §9 misconception safety→T7 (semantic, upgraded); §10 abstention (verified safe — no change needed); §11 audit logging→T7 (`_LOG.info` lines) + T12 probe check; §12 chat composition→T10; §13 tests→every task; §14 live probe→T12; §15 drift→T12; §16 commit plan→reordered (registry first); §17 open decisions→resolved in the interview (recorded in `state.json`).

**Placeholder scan:** clean — no TODO/TBD/illustrative stubs. Every code step shows the real implementation.

**Type consistency:** `NLIContext`, `NLIParams`, `NLIResult`, `SemanticCandidate`, `match_nli_semantic`, `shortlist_semantic_candidates`, `polarity_allows_match`, `normalize_nli_output`, `nli_enabled`, `load_nli_params` names are identical across all tasks that consume them. `ScoredMatch(node_id, candidate, "nli", cap)` uses the real 4-positional form (4th field `score`).
