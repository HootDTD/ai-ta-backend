"""WU-3B2e stage 3 — the pairing / correctness gate (two-phase span-grounded judge).

``validate_pair`` decides whether a ``ReferenceSolutionDraft`` may become teachable
for THIS ``CandidateQuestion``. It runs a two-phase span-grounded LLM judge over
the injected ``judge_fn`` (cheap_chat-shaped, MOCKED Tier-1):

  * **Phase A — pairing / answer-relevance:** "does THIS solution answer THIS
    question?" If not paired, Phase B is short-circuited (cheaper).
  * **Phase B — claim-decomposed faithfulness:** the judge decomposes the solution
    into claims and marks each entailed/not-entailed by the grounding; ANY
    unentailed claim ⇒ ``faithful=False`` with the offending claims in
    ``failed_claims``.

The judge sees the SAME grounding the generator used — ``draft.grounding`` when
present, else it re-grounds via ``retrieve_fn(question)`` (span-grounded, both
phases use the identical context).

**FAIL-CLOSED — the load-bearing safety property.** This is the EXPLICIT INVERSION
of the §6 leakage judge (``apollo/agent/leakage_judge.py:126-129``), which fails
OPEN (a parse glitch lets content THROUGH with ``leaks=False``). Here a
malformed/non-JSON/exception judge response at EITHER phase ⇒
``PairingVerdict(paired=False, faithful=False, failed_claims=("<unparseable
judge response>",), confidence=0.0)`` — auto-provisioning prefers a false-REJECT
to a false-APPROVE. ALL ``judge_fn`` invocations route through one private helper
(``_judge_or_fail_closed``) so there is a SINGLE place the fail-closed default
lives (a future third judge call cannot accidentally skip it).

§1.8 / OPS-6 CAVEAT: a coherent-but-WRONG solution that passes Phase A + Phase B +
all 8 promotion gates IS still shown in shadow (no Layer-3 belief movement,
``APOLLO_GRAPH_SIM_LAYER3_ENABLED`` OFF). The pairing gate REDUCES but does NOT
eliminate this; the real pre-exposure safety is the ``APOLLO_AUTOPROVISION_ENABLED``
flag-OFF default + the §6.7 calibration gate, with 3B2h quarantine the retroactive
catch.

A FAIL verdict is mapped to a typed ``Rejection`` by ``rejection_from_verdict``
(the single fail-mapping point; 3B2g writes the ``apollo_rejected_problems`` row).

NO network: ``judge_fn``/``retrieve_fn`` are injected (mocked in Tier-1). NO DB
write (pure compute over the injected fns). INPUTS ARE COURSE MATERIAL ONLY — no
student PII; the structured log line carries only counts + booleans +
``solution_source`` (NO solution/question/passage text).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, Literal

from pydantic import BaseModel, field_validator

from apollo.provisioning.solution import GroundingSpan, ReferenceSolutionDraft

__all__ = [
    "PairingVerdict",
    "Rejection",
    "validate_pair",
    "rejection_from_verdict",
]

_LOG = logging.getLogger(__name__)

_UNPARSEABLE_MARKER = "<unparseable judge response>"
_NO_CLAIMS_MARKER = "<no claims decomposed>"


class PairingVerdict(BaseModel):
    """The stage-3 output type. ``paired`` (Phase A: solution answers THIS
    question), ``faithful`` (Phase B: every claim entailed by the grounding),
    ``failed_claims`` (the unentailed claims — empty on PASS), ``confidence``
    (clamped to [0, 1]). ``approved`` == (``paired`` and ``faithful``)."""

    paired: bool
    faithful: bool
    failed_claims: tuple[str, ...] = ()
    confidence: float = 0.0

    @field_validator("failed_claims", mode="before")
    @classmethod
    def _coerce_failed_claims(cls, v: Any) -> Any:
        if isinstance(v, list):
            return tuple(v)
        return v

    @field_validator("confidence", mode="after")
    @classmethod
    def _clamp_confidence(cls, v: float) -> float:
        return max(0.0, min(1.0, v))

    @property
    def approved(self) -> bool:
        return self.paired and self.faithful


class Rejection(BaseModel):
    """The FAIL handoff (3B2g writes the ``apollo_rejected_problems`` row). ``reason``
    ∈ {'unparseable_judge', 'not_paired', 'unfaithful_claims'}."""

    stage: Literal["pairing_gate"] = "pairing_gate"
    reason: str
    diagnostic: str
    failed_claims: tuple[str, ...] = ()

    @field_validator("failed_claims", mode="before")
    @classmethod
    def _coerce_failed_claims(cls, v: Any) -> Any:
        if isinstance(v, list):
            return tuple(v)
        return v


def _coerce_confidence(value: Any) -> float:
    """Clamp a judge-reported confidence to [0, 1]; a non-numeric → 0.0 (mirrors
    ``leakage_judge`` :134-137)."""
    try:
        c = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, c))


def _grounding_text(spans: Sequence[GroundingSpan]) -> str:
    """Render the grounding spans the judge entails against (course material only,
    no PII)."""
    return "\n\n".join(s.text for s in spans)


def _judge_or_fail_closed(
    judge_fn: Callable[..., str], *, purpose: str, payload: dict
) -> dict | None:
    """The SINGLE ``judge_fn`` invocation point. Calls the judge, parses the JSON,
    and returns the parsed dict. FAIL-CLOSED: a parse error / exception / non-dict
    response returns ``None`` (the caller maps ``None`` to a REJECT verdict — the
    inversion of ``leakage_judge``'s fail-OPEN default at :126-129). A future judge
    call MUST route through here so the fail-closed default cannot be skipped."""
    try:
        raw = judge_fn(
            purpose=purpose,
            messages=[
                {"role": "user", "content": json.dumps(payload)},
            ],
            response_format={"type": "json_object"},
            temperature=0.0,
        )
        parsed = json.loads(raw)
    except Exception as exc:  # noqa: BLE001 - any failure → fail-CLOSED (reject)
        _LOG.warning(
            "pairing_judge fail-closed (reject) on parse error: %s", exc
        )
        return None
    if not isinstance(parsed, dict):
        _LOG.warning("pairing_judge fail-closed (reject): non-object response")
        return None
    return parsed


def _fail_closed_verdict() -> PairingVerdict:
    """The fail-CLOSED verdict — a REJECT, never an approval (confidence 0.0)."""
    return PairingVerdict(
        paired=False,
        faithful=False,
        failed_claims=(_UNPARSEABLE_MARKER,),
        confidence=0.0,
    )


async def validate_pair(
    question,
    draft: ReferenceSolutionDraft,
    *,
    retrieve_fn: Callable[..., Awaitable[Sequence[GroundingSpan]]],
    judge_fn: Callable[..., str],
) -> PairingVerdict:
    """Two-phase span-grounded validation of a (question, draft) pair. See the
    module docstring for the full contract. FAIL-CLOSED on any unparseable judge
    response at EITHER phase.

    The judge sees ``draft.grounding`` when present, else re-grounds via
    ``retrieve_fn(question)``. Phase A runs first (cheaper short-circuit); Phase B
    only runs when Phase A says paired."""
    spans: tuple[GroundingSpan, ...] = tuple(draft.grounding)
    if not spans:
        spans = tuple(await retrieve_fn(question))
    grounding_text = _grounding_text(spans)

    problem_text = getattr(question, "problem_text", "")

    # --- Phase A — pairing / answer-relevance ------------------------------- #
    phase_a = _judge_or_fail_closed(
        judge_fn,
        purpose="pairing_phase_a",
        payload={
            "task": "pairing",
            "problem_text": problem_text,
            "reference_solution": draft.reference_solution,
            "grounding": grounding_text,
        },
    )
    if phase_a is None:
        verdict = _fail_closed_verdict()
        _log_verdict(verdict, draft)
        return verdict

    paired = bool(phase_a.get("paired", False))
    confidence = _coerce_confidence(phase_a.get("confidence", 0.0))
    if not paired:
        verdict = PairingVerdict(
            paired=False,
            faithful=False,
            failed_claims=(),
            confidence=confidence,
        )
        _log_verdict(verdict, draft)
        return verdict

    # --- Phase B — claim-decomposed faithfulness ---------------------------- #
    phase_b = _judge_or_fail_closed(
        judge_fn,
        purpose="pairing_phase_b",
        payload={
            "task": "faithfulness",
            "reference_solution": draft.reference_solution,
            "grounding": grounding_text,
        },
    )
    if phase_b is None:
        verdict = PairingVerdict(
            paired=True,
            faithful=False,
            failed_claims=(_UNPARSEABLE_MARKER,),
            confidence=confidence,
        )
        _log_verdict(verdict, draft)
        return verdict

    claims = phase_b.get("claims", []) or []
    parsed = [c for c in claims if isinstance(c, dict)]
    if not parsed:
        # Fail-CLOSED on a degenerate Phase-B response: a faithfulness judge that
        # decomposes ZERO claims gives NO positive evidence of faithfulness (a
        # real reference solution always has >=1 checkable claim). Reject rather
        # than vacuously approve — mirrors the unparseable path, distinguished by
        # _NO_CLAIMS_MARKER for an honest reject reason.
        verdict = PairingVerdict(
            paired=True,
            faithful=False,
            failed_claims=(_NO_CLAIMS_MARKER,),
            confidence=confidence,
        )
        _log_verdict(verdict, draft)
        return verdict
    failed = tuple(
        str(c.get("claim", ""))
        for c in parsed
        if not c.get("entailed", False)
    )
    faithful = not failed
    verdict = PairingVerdict(
        paired=True,
        faithful=faithful,
        failed_claims=failed,
        confidence=confidence,
    )
    _log_verdict(verdict, draft)
    return verdict


def _log_verdict(verdict: PairingVerdict, draft: ReferenceSolutionDraft) -> None:
    """Emit ONE structured line per validation — counts + booleans only, NO
    solution/question/passage text (the ``_llm._log_call`` convention)."""
    _LOG.info(
        "pairing_verdict",
        extra={
            "event": "pairing_verdict",
            "paired": verdict.paired,
            "faithful": verdict.faithful,
            "n_failed_claims": len(verdict.failed_claims),
            "confidence": verdict.confidence,
            "solution_source": draft.solution_source,
        },
    )


def rejection_from_verdict(verdict: PairingVerdict) -> Rejection | None:
    """Map a ``PairingVerdict`` to a typed ``Rejection`` — the SINGLE fail-mapping
    point. Returns ``None`` when approved (paired AND faithful); else a typed
    ``Rejection`` whose ``reason`` distinguishes an unparseable-judge reject, a
    mispairing, and an unfaithful-claim reject (3B2g writes the
    ``apollo_rejected_problems`` row)."""
    if verdict.approved:
        return None

    if _UNPARSEABLE_MARKER in verdict.failed_claims:
        return Rejection(
            reason="unparseable_judge",
            diagnostic="the judge response was unparseable (fail-closed reject)",
            failed_claims=verdict.failed_claims,
        )
    if _NO_CLAIMS_MARKER in verdict.failed_claims:
        return Rejection(
            reason="no_claims_decomposed",
            diagnostic="the faithfulness judge decomposed zero claims (fail-closed reject)",
            failed_claims=verdict.failed_claims,
        )
    if not verdict.paired:
        return Rejection(
            reason="not_paired",
            diagnostic="the solution does not answer this question (Phase A)",
            failed_claims=verdict.failed_claims,
        )
    return Rejection(
        reason="unfaithful_claims",
        diagnostic="one or more solution claims are not entailed by the grounding",
        failed_claims=verdict.failed_claims,
    )
