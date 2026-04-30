"""Apollo output filter — two-stage structural-leakage barrier (V3).

Stage 1 (deterministic, fast):
    Concept-scoped pre-filter against `concept.forbidden_named_laws`.
    Rejects on the first verbatim word in the draft that is forbidden
    for this concept AND not in the student's vocabulary (history +
    KG summary). Same semantics as V2 but the term list is per-concept,
    not a global fluid-mechanics frozenset.

Stage 2 (LLM-as-judge):
    Catches paraphrases the deterministic stage cannot (e.g. "speed times
    area is constant" leaking continuity). Runs on a cheap model. Acts on
    leaks only above `leakage_judge.CONFIDENCE_THRESHOLD`.

Policy: see `apollo/agent/LEAKAGE_POLICY.md` — this file is the
enforcement of that contract. The judge prompt cites the policy verbatim.

NO FALLBACK: rejection raises FilterRejectedError; the caller surfaces
the error in the UI.
"""
from __future__ import annotations

import logging
import re
from typing import Iterable

from apollo.agent.leakage_judge import (
    CONFIDENCE_THRESHOLD,
    LeakageJudge,
    llm_leakage_judge,
)
from apollo.errors import FilterRejectedError
from apollo.subjects import ConceptDefinition

_LOG = logging.getLogger(__name__)

_WORD_RE = re.compile(r"[A-Za-z]+(?:'[A-Za-z]+)?")


def _tokenize(text: str) -> list[str]:
    return [m.group(0).lower().strip("'") for m in _WORD_RE.finditer(text)]


def _student_vocabulary(
    history: list[dict[str, str]],
    kg_summary: str,
) -> set[str]:
    """All words the student has actually introduced — history + summary.

    The summary is the deliberate vocabulary mirror (see
    `KGStore.summarize_for_apollo` and `LEAKAGE_POLICY.md`); it surfaces
    student-chosen labels, equation symbols, mapped terms.
    """
    vocab: set[str] = set()
    for msg in history:
        if msg.get("role") == "user":
            vocab.update(_tokenize(msg.get("content", "")))
    vocab.update(_tokenize(kg_summary))
    return vocab


def _pre_filter_offender(
    draft: str,
    concept: ConceptDefinition,
    history: list[dict[str, str]],
    kg_summary: str,
) -> str | None:
    """Return the first forbidden word in the draft that the student has
    not introduced, or None if the draft is clean."""
    forbidden: Iterable[str] = concept.forbidden_named_laws.all_terms()
    forbidden_set = set(forbidden)
    if not forbidden_set:
        return None
    allowed = _student_vocabulary(history, kg_summary)
    for token in _tokenize(draft):
        if token in forbidden_set and token not in allowed:
            return token
    return None


def validate_or_raise(
    draft: str,
    *,
    concept: ConceptDefinition,
    history: list[dict[str, str]],
    kg_summary: str,
    judge: LeakageJudge | None = None,
) -> str:
    """Two-stage check. Returns the draft unchanged if clean.

    Raises FilterRejectedError on:
    - first forbidden word the student hasn't introduced (pre-filter), OR
    - judge verdict `leaks=true` with confidence >= CONFIDENCE_THRESHOLD.

    `judge` is dependency-injected for tests. Production callers pass
    None and get the default LLM judge.
    """
    offender = _pre_filter_offender(draft, concept, history, kg_summary)
    if offender is not None:
        _LOG.info(
            "filter_rejected_pre",
            extra={
                "event": "filter_rejected",
                "stage": "pre_filter",
                "rejected_term": offender,
                "concept_id": concept.concept_id,
            },
        )
        raise FilterRejectedError(rejected_term=offender, draft=draft)

    judge_fn: LeakageJudge = judge or llm_leakage_judge
    verdict = judge_fn(
        draft=draft,
        concept=concept,
        history=history,
        kg_summary=kg_summary,
    )
    if verdict.leaks and verdict.confidence >= CONFIDENCE_THRESHOLD:
        rejected = verdict.offending_phrase or "<paraphrase>"
        _LOG.info(
            "filter_rejected_judge",
            extra={
                "event": "filter_rejected",
                "stage": "llm_judge",
                "rejected_term": rejected,
                "reason": verdict.reason,
                "confidence": verdict.confidence,
                "concept_id": concept.concept_id,
            },
        )
        raise FilterRejectedError(rejected_term=rejected, draft=draft)
    if verdict.leaks:
        # Below-threshold leak — log for tuning, don't block.
        _LOG.info(
            "filter_low_confidence_leak",
            extra={
                "event": "filter_low_confidence_leak",
                "rejected_term": verdict.offending_phrase,
                "reason": verdict.reason,
                "confidence": verdict.confidence,
                "concept_id": concept.concept_id,
            },
        )

    return draft


__all__ = ["validate_or_raise"]
