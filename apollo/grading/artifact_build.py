"""Pure artifact builder for the permanent transcript/topic grading path."""

from __future__ import annotations

import logging
from typing import Any

from apollo.overseer.topic_score import TopicScoreResult
from apollo.overseer.topic_score_serialize import serialize_topic_score

_LOG = logging.getLogger(__name__)

GRADER_USED_LLM_FALLBACK = "llm_fallback"
GRADER_USED_LLM_TRANSCRIPT = "llm_transcript"
_GRADER_VERSION_LLM_FALLBACK = "llm-fallback-v1"


LEDGER_STATUS_UNPROBED = "unprobed"


def _normalized_score(value: float) -> float:
    return round(max(0.0, min(1.0, value)), 6)


def _unprobed_keys(topic_score: TopicScoreResult | None) -> frozenset[str]:
    """Canonical keys P1.2b left out of the grade (2026-08-07 bimodal fix).

    `coverage["per_step"]` still carries these nodes — the adjudicator DID
    return verdicts for them — so without this they would land in `node_ledger`
    as `unresolved` and render into the scorecard's *missing or unclear* list as
    "Next time, explain X", while the very same payload's `topics[]` says X was
    not part of this grade. They keep a ledger row (the record must stay
    complete) under their own status instead.

    Downstream, "a new status" is NOT automatically "safely ignored": the
    student-facing `projections/scorecard` and `projections/mastery` do want it
    gone and drop it by construction, but `projections/classroom`'s
    lowest-coverage query used to catch these very nodes through its
    `unresolved` + NULL-span branch — a never-taught concept surfacing as a
    worst offender is the signal the teacher needs. It matches
    `LEDGER_STATUS_UNPROBED` explicitly so that signal survives the rename
    (review fix, 2026-08-08).
    """
    if topic_score is None:
        return frozenset()
    return frozenset(
        topic.canonical_key for topic in topic_score.topics if topic.status == "unprobed"
    )


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
    # 2026-08-08: the adjudicator's declared evidence class per node
    # (stated / used / implied / absent). OPTIONAL both ways — the dormant graph
    # lane emits none, and every artifact written before today has none — so it
    # is read with `.get` and lands as None rather than being required.
    basis: dict[str, str] = coverage.get("basis") or {}
    unprobed_keys = _unprobed_keys(topic_score)
    covered = [
        key for key, status in per_step.items() if status == "covered" and key not in unprobed_keys
    ]
    missing = [
        key for key, status in per_step.items() if status != "covered" and key not in unprobed_keys
    ]
    unprobed = [key for key in per_step if key in unprobed_keys]
    # `node_coverage` is a ratio over the nodes that were actually graded, so an
    # unprobed node must leave BOTH sides of it — otherwise excluding a node
    # from the grade would still depress the artifact's coverage number.
    total = len(covered) + len(missing)
    node_coverage = (len(covered) / total) if total else 0.0
    overall_score = (rubric or {}).get("overall", {}).get("score")
    score = _normalized_score(float(overall_score) / 100.0) if overall_score is not None else 0.0
    node_ledger = [
        {
            "canonical_key": key,
            "status": status,
            # `method` stays the GRAPH lane's resolver vocabulary (exact /
            # fuzzy / semantic / nli / clarification, per migration 034's row
            # comment). `basis` is the transcript adjudicator's evidence class
            # and gets its own key — two enums in one field would make both
            # unreadable, and the pair (status, basis) is the whole diagnostic.
            "method": None,
            "basis": basis.get(key),
            "confidence": confidences.get(key),
            "evidence_span": None,
        }
        for status, keys in (
            ("credited", covered),
            ("unresolved", missing),
            (LEDGER_STATUS_UNPROBED, unprobed),
        )
        for key in keys
    ]
    _LOG.debug(
        "build_llm_artifact node_ledger: %d credited, %d unresolved, %d unprobed",
        len(covered),
        len(missing),
        len(unprobed),
    )
    # Annotated because the payload is a heterogeneous JSONB document: without it
    # mypy narrows the literal to a union of every value type and then rejects the
    # `scores.topic_score` assignment below (3 pre-existing errors, 2026-08-08).
    artifact: dict[str, Any] = {
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
