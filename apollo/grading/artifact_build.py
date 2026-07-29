"""Pure artifact builder for the permanent transcript/topic grading path."""

from __future__ import annotations

import logging

from apollo.overseer.topic_score import TopicScoreResult
from apollo.overseer.topic_score_serialize import serialize_topic_score

_LOG = logging.getLogger(__name__)

GRADER_USED_LLM_FALLBACK = "llm_fallback"
GRADER_USED_LLM_TRANSCRIPT = "llm_transcript"
_GRADER_VERSION_LLM_FALLBACK = "llm-fallback-v1"


def _normalized_score(value: float) -> float:
    return round(max(0.0, min(1.0, value)), 6)


def build_llm_artifact(
    *,
    coverage: dict,
    rubric: dict,
    latency_ms: int | None,
    clarification_trace: list[dict],
    topic_score: TopicScoreResult | None = None,
) -> dict:
    """Build the canonical artifact from transcript coverage and topic scoring."""
    per_step: dict[str, str] = coverage.get("per_step") or {}
    confidences: dict[str, float] = coverage.get("confidences") or {}
    covered = [key for key, status in per_step.items() if status == "covered"]
    missing = [key for key, status in per_step.items() if status != "covered"]
    total = len(per_step)
    node_coverage = (len(covered) / total) if total else 0.0
    overall_score = (rubric or {}).get("overall", {}).get("score")
    score = (
        _normalized_score(float(overall_score) / 100.0)
        if overall_score is not None
        else 0.0
    )
    node_ledger = [
        {
            "canonical_key": key,
            "status": status,
            "method": None,
            "confidence": confidences.get(key),
            "evidence_span": None,
        }
        for status, keys in (("credited", covered), ("unresolved", missing))
        for key in keys
    ]
    _LOG.debug(
        "build_llm_artifact node_ledger: %d credited, %d unresolved",
        len(covered),
        len(missing),
    )
    artifact = {
        "grader_used": GRADER_USED_LLM_FALLBACK,
        "versions": {
            "grader": _GRADER_VERSION_LLM_FALLBACK,
            "reference_graph_hash": None,
        },
        "node_ledger": node_ledger,
        "edge_ledger": [],
        "misconceptions": [],
        "clarification_trace": list(clarification_trace),
        "scores": {
            "node_coverage": node_coverage,
            "composite": score,
            "llm_rubric": rubric,
        },
        "abstention": {
            "abstained": None,
            "reasons": [],
            "fallback_grade": overall_score,
        },
        "grading_latency_ms": latency_ms,
    }
    if topic_score is not None:
        artifact["scores"]["topic_score"] = serialize_topic_score(topic_score)
    return artifact
