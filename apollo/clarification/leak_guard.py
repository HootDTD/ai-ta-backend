"""Leakage-judge backstop for clarification replies (spec §6.4). The answer-blind
generator + the probe-hint no-leak test (Task 7) are the PRIMARY guarantee; this
is the second line: if the judge confidently flags a leak in a reply that carried
a probe, re-draft WITHOUT the probes rather than risk revealing the answer.
Soft-fail-open (spec §12): a judge error leaves the reply unchanged — teaching is
never blocked. Uses the judge callable directly, NOT the raising validate_or_raise."""

from __future__ import annotations

import logging
from collections.abc import Callable

from apollo.agent.leakage_judge import (
    CONFIDENCE_THRESHOLD,
    JudgeVerdict,
    LeakageJudge,
    llm_leakage_judge,
)
from apollo.subjects import ConceptDefinition

_LOG = logging.getLogger(__name__)


def guard_clarification_reply(
    *,
    draft: str,
    concept: ConceptDefinition,
    history: list[dict[str, str]],
    kg_summary: str,
    regenerate_without_probes: Callable[[], str],
    judge: LeakageJudge | None = None,
) -> str:
    judge_fn: LeakageJudge = judge or llm_leakage_judge
    try:
        verdict: JudgeVerdict = judge_fn(
            draft=draft, concept=concept, history=history, kg_summary=kg_summary
        )
    except Exception as exc:  # noqa: BLE001 - soft fail open (spec §12)
        _LOG.warning("clarification_leak_judge_failed error=%s", exc)
        return draft
    if verdict.leaks and verdict.confidence >= CONFIDENCE_THRESHOLD:
        _LOG.warning(
            "clarification_probe_leak_detected phrase=%s confidence=%.2f",
            verdict.offending_phrase,
            verdict.confidence,
        )
        return regenerate_without_probes()
    return draft
