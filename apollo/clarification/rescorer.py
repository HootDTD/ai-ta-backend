"""Re-score a clarified idea (spec §7). Reads (original ambiguous statement +
the student's committed clarification + the one candidate idea) and rules
correct / wrong / vague. This is NOT the deleted silent guess (which guessed
from nothing): it judges a committed answer to a pointed question — far more
decidable. DI'd + stubbed in tests; no live model in CI.

2026-07-10 emergent misconception map plan (T3/Q2): ``rescore_clarification``
returns a frozen ``RescoreResult{outcome, confidence}`` rather than the bare
``RescoreOutcome`` literal, so the plan's ``clarification_refuted`` capture
seam can carry a confidence weight (``opposes`` is NOT the rescorer's job —
the caller, ``resolve_turn.py``, resolves it from the ``Candidate``/
``Clarification`` row it already has in hand). Every pre-existing
``ClarificationJudge`` test stub returns a bare ``RescoreOutcome`` literal;
``rescore_clarification`` keeps accepting that shape via a back-compat shim
(``confidence=1.0``) so no existing caller/test needs to change (R3) — a judge
may ALSO return a ``RescoreResult`` directly, which passes through unchanged.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, cast

from apollo.agent._llm import main_chat
from apollo.errors import ResolutionUnavailableError

RescoreOutcome = Literal["confirmed", "refuted", "vague"]
_RESPONSE_FORMAT = {"type": "json_object"}
_PURPOSE = "clarification_rescore"
_VALID = {"confirmed", "refuted", "vague"}


@dataclass(frozen=True)
class ClarificationRequest:
    original_statement: str
    clarification_text: str
    candidate_display: str


@dataclass(frozen=True)
class RescoreResult:
    """The rescorer's full verdict: the three-way outcome plus a confidence
    weight (0..1). ``opposes`` is deliberately NOT a field here — the caller
    (``resolve_turn.py``) resolves it from the candidate/clarification row it
    already holds, never invented by the judge (Q2)."""

    outcome: RescoreOutcome
    confidence: float


# A judge may return either the legacy bare literal (every pre-existing test
# stub) or the richer RescoreResult directly — rescore_clarification accepts
# both (back-compat shim, R3).
ClarificationJudge = Callable[[ClarificationRequest], "RescoreOutcome | RescoreResult"]


def _build_messages(request: ClarificationRequest) -> list[dict[str, str]]:
    system = (
        "You judge whether a student's clarified explanation matches a target idea. "
        'Reply strict JSON {"verdict": "confirmed"|"refuted"|"vague", "confidence": 0.0-1.0}. '
        "confirmed = the clarification correctly expresses the target idea; "
        "refuted = it states the opposite or a wrong claim; "
        "vague = noncommittal / unclear. Judge meaning, not wording. "
        "confidence reflects how sure you are of the verdict."
    )
    user = json.dumps(
        {
            "original_statement": request.original_statement,
            "clarification": request.clarification_text,
            "target_idea": request.candidate_display,
        }
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _parse_confidence(raw: object) -> float:
    """Unknown/missing/non-numeric confidence -> 0.0 (no-weight), same
    convention as an unknown verdict defaulting to no-credit."""
    try:
        return float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


def default_clarification_judge(request: ClarificationRequest) -> RescoreResult:
    try:
        raw = main_chat(
            purpose=_PURPOSE,
            messages=_build_messages(request),
            response_format=_RESPONSE_FORMAT,
            temperature=0.0,
        )
        parsed = json.loads(raw or "{}")
        verdict = str(parsed.get("verdict", "vague"))
        if verdict not in _VALID:
            # unknown -> no credit, no weight.
            return RescoreResult(outcome="vague", confidence=0.0)
        return RescoreResult(
            outcome=cast(RescoreOutcome, verdict),
            confidence=_parse_confidence(parsed.get("confidence")),
        )
    except ResolutionUnavailableError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ResolutionUnavailableError(
            stage="clarification_rescore", last_error=str(exc)
        ) from exc


def rescore_clarification(
    *,
    original_statement: str,
    clarification_text: str,
    candidate_display: str,
    judge: ClarificationJudge,
) -> RescoreResult:
    """Judge a committed clarification against one candidate idea.

    ``judge`` may return either a bare ``RescoreOutcome`` literal (the legacy
    shape every pre-existing test stub uses — wrapped here with
    ``confidence=1.0``, R3 back-compat) or a ``RescoreResult`` directly (the
    native shape ``default_clarification_judge`` now returns), which passes
    through unchanged."""
    raw = judge(
        ClarificationRequest(
            original_statement=original_statement,
            clarification_text=clarification_text,
            candidate_display=candidate_display,
        )
    )
    if isinstance(raw, RescoreResult):
        return raw
    return RescoreResult(outcome=raw, confidence=1.0)
