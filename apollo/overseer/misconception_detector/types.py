"""Immutable value objects for the Apollo misconception detector.

Frozen contract: ``docs/_archive/plans/2026-07-08-apollo-misconception-detector-plan.md``
section 2, amended by A1 (JudgeRaw.verdict_token_prob / confidence semantics)
and A5 (canonical_key rules, enforced downstream in ``merge.py``).

All value objects are ``@dataclass(frozen=True)`` — every stage in the
detector pipeline (tiers -> gate -> merge -> apply) returns a NEW object
rather than mutating an existing one. Collections are tuples, never lists,
so instances stay hashable-safe and accidental in-place mutation is
impossible.

No IO, no LLM, no DB imports in this module — pure data shapes only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol

from apollo.overseer.misconception_bank import MisconceptionEntry

Verdict = Literal["clear", "needs_clarification", "misconception", "wrong"]
DetectorSource = Literal["sympy_veto", "bank_pattern", "judge"]


@dataclass(frozen=True)
class ConceptFinding:
    """One per-concept misconception signal from ONE detector tier.

    Immutable — every field is set at construction time; ``severity`` starts
    at ``0.0`` and is only ever populated by ``merge.py`` when it builds a NEW
    ``ConceptFinding``-derived row (this dataclass itself is never mutated).
    """

    concept_key: str
    verdict: Verdict
    confidence: float  # 0..1; for judge findings this is verdict_token_prob or the verbalized confidence field (A1)
    severity: float  # w(centrality) * confidence, filled by merge (0.0 pre-merge)
    evidence_span: str  # student surface text that triggered it ("" if none)
    signature: str  # "misc.<code>" if bank-matched else "unkeyed:<concept_id>"
    source: DetectorSource
    corroborated: bool  # set True by merge/gate when >=2 tiers agree or a deterministic veto
    # A1 origin bit for judge findings: True when ``confidence`` came from a real
    # verdict-token probability (gate uses the looser ``tau_fire``), False when it
    # came from the verbalized-confidence fallback field (gate uses the stricter
    # ``tau_fire_verbalized``, since verbalized confidence runs overconfident).
    # Meaningless for non-judge sources; defaults True so pre-A1 constructors
    # (and deterministic/bank tiers) keep their prior gate behavior.
    verdict_token_prob_present: bool = True
    # A11 (corroboration/keying redesign spec §4.1): the validated bank code
    # this finding names, or None. Set ONLY after the code is validated
    # against the concept's bank_entries (judge tier, judge.py::_finding_from_row)
    # or taken directly from the matched entry (bank_pattern/sympy_veto).
    # Drives whether a lone judge finding is dock-eligible (A9, gate.py) and
    # how merge keys the row (A5). INVARIANT: ``bank_code is not None`` iff
    # ``signature == f"misc.{bank_code}"``. Never mutated; gate.py/merge.py
    # always build a NEW ConceptFinding (dataclasses.replace) to change it.
    bank_code: str | None = None
    # A10 (corroboration/keying redesign spec §4.1): for a bank_pattern
    # finding, True iff its best-ranked match cleared BANK_SIM_FLOOR (a
    # self-standing standalone hit) vs a below-floor corroboration-only hit.
    # Meaningless for non-bank sources; defaults True so sympy_veto/judge
    # constructors are unaffected.
    bank_match_above_floor: bool = True
    # A12 (corroboration/keying redesign spec §4.6): True only when this dock
    # is allowed to trip the anti-dilution band ceiling on a
    # maximally-central concept. sympy_veto docks and bank-corroborated docks
    # set it True; a lone-judge (penalty-only) dock sets it False. merge.py
    # reads THIS field directly (not centrality-plus-source-inference) to
    # decide ``ceiling_applied``. Defaults False so non-dock findings (every
    # pre-gate tier finding) and all existing tier constructors are unaffected.
    ceiling_eligible: bool = False


@dataclass(frozen=True)
class DetectionResult:
    """Grader-agnostic detector output. Immutable; ``per_concept`` is a tuple."""

    per_concept: tuple[ConceptFinding, ...] = field(default_factory=tuple)

    @property
    def is_empty(self) -> bool:
        return len(self.per_concept) == 0


@dataclass(frozen=True)
class MergeOutcome:
    """The merge stage's product: the live penalty + ledger-feed rows + ceiling flag."""

    misconception_penalty: (
        float  # Sigma severity over corroborated findings, clamped (SEVERITY_CLAMP)
    )
    misconceptions: tuple[
        dict, ...
    ]  # artifact misconceptions[] rows: {canonical_key, evidence_span, confidence, opposes}
    ceiling_applied: bool  # a central corroborated misconception caps the artifact composite below the named Strong band (A4)
    ledger_findings: tuple[
        ConceptFinding, ...
    ]  # gate-cleared corroborated findings for the emergent store


@dataclass(frozen=True)
class JudgeRaw:
    """Raw output from a single judge call, before parsing into findings."""

    content: str  # JSON string; each concept row carries a "confidence" field (A1)
    verdict_token_prob: float | None  # None when logprobs are unavailable/unwalkable (R1)


class JudgeFn(Protocol):
    """DI seam for the LLM judge tier — production and test implementations
    both satisfy this Protocol so no test ever makes a live OpenAI call."""

    def __call__(self, *, system: str, user: str) -> JudgeRaw: ...


class EmbedFn(Protocol):
    """DI seam for the bank_pattern tier's embedding step."""

    def __call__(self, text: str) -> list[float]: ...


@dataclass(frozen=True)
class JudgeConceptInput:
    """One concept's worth of input to a batched judge call."""

    concept_key: str
    correct_belief: str
    bank_entries: tuple[MisconceptionEntry, ...]
