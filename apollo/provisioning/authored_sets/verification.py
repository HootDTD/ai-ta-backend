"""Low-OCR-confidence cross-check for authored references (WU-AAS).

When the extracted reference's grounding OCR'd below threshold (or the problem
doc is itself low-confidence), generate an independent solution and compare.
Material divergence (different final answer / contradictory core equation)
flags the problem for review; agreement trusts the extraction. High-confidence
extractions skip this entirely for cost control.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from pydantic import BaseModel

from apollo.provisioning.solution import ReferenceSolutionDraft, find_or_generate

_LOG = logging.getLogger(__name__)

_COMPARE_SYSTEM = (
    "You compare two worked solutions to the SAME problem. Decide if they are "
    "MATERIALLY equivalent: same final answer AND no directly contradictory core "
    "equation. Different-but-valid procedures that reach the same answer ARE "
    'equivalent. Respond ONLY as JSON: {"equivalent": <bool>, "reason": "<short>"}.'
)


class VerificationVerdict(BaseModel):
    review_required: bool
    reason: str | None = None
    generated_alt: dict | None = None
    ocr_confidence: float | None = None
    match_method: str | None = None


async def _empty_retrieve(_question: Any) -> tuple:
    return ()


async def _independent_generate(db: Any, candidate: Any, *, chat_fn: Any) -> ReferenceSolutionDraft:
    """Generate from the problem alone, independent of OCR'd solution text."""
    return await find_or_generate(db, candidate, retrieve_fn=_empty_retrieve, chat_fn=chat_fn)


def _step_symbolic(step: dict) -> str:
    symbolic = step.get("symbolic")
    if symbolic is None and isinstance(step.get("content"), dict):
        symbolic = step["content"].get("symbolic")
    return (symbolic or "").strip()


def _final_answer(draft: ReferenceSolutionDraft) -> str:
    for step in reversed(draft.reference_solution or []):
        sym = _step_symbolic(step)
        if sym:
            return sym
    return ""


async def verify_against_generated(
    db: Any,
    *,
    candidate: Any,
    draft: ReferenceSolutionDraft,
    min_conf: float | None,
    problem_low_conf: bool,
    match_method: str | None,
    metered_chat: Any,
    conf_threshold: float,
) -> VerificationVerdict:
    low_confidence = (min_conf is not None and min_conf < conf_threshold) or problem_low_conf
    base = VerificationVerdict(
        review_required=False,
        ocr_confidence=min_conf,
        match_method=match_method,
    )
    if not low_confidence:
        return base

    generated = await _independent_generate(db, candidate, chat_fn=metered_chat.main)

    if _final_answer(generated) and _final_answer(generated) == _final_answer(draft):
        return base

    raw = metered_chat.cheap(
        purpose="authored_ocr_compare",
        messages=[
            {"role": "system", "content": _COMPARE_SYSTEM},
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "problem_text": getattr(candidate, "problem_text", ""),
                        "solution_a_extracted": draft.reference_solution,
                        "solution_b_generated": generated.reference_solution,
                    }
                ),
            },
        ],
        response_format={"type": "json_object"},
    )
    try:
        equivalent = bool(json.loads(raw).get("equivalent"))
    except (json.JSONDecodeError, TypeError, AttributeError):
        equivalent = False
        _LOG.info("authored_ocr_compare_unparseable")

    if equivalent:
        return base
    return VerificationVerdict(
        review_required=True,
        reason="ocr_divergence",
        generated_alt=generated.model_dump(),
        ocr_confidence=min_conf,
        match_method=match_method,
    )
