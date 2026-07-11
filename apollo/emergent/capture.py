"""Emergent misconception map — capture seams (2026-07-10 design §5.3, plan
Wave 2 T2/T3).

Small, single-purpose write helpers on top of ``apollo.emergent.store``'s
idempotent ``record_observation``. Each function here builds the node-anchored
``emergent.<entity_key>`` signature (spec §5.2) for one capture source and
appends the observation. Neither function commits — the caller (``done.py`` /
``resolve_turn.py``) owns its own failure domain (its own try/except + own
commit/rollback), so a capture failure here can never affect the grade or
resolution outcome it is riding alongside.

Callers are expected to have already checked the relevant flag
(``emergent_map_capture_enabled``) before invoking either function — this
module stays a pure write helper, mirroring ``apollo.emergent.store``'s own
flag-agnostic design.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from apollo.emergent.store import record_observation
from apollo.overseer.misconception_detector.types import ConceptFinding

_SIGNATURE_PREFIX = "emergent."


def _emergent_signature(entity_key: str) -> str:
    return f"{_SIGNATURE_PREFIX}{entity_key}"


async def record_detector_births(
    db: AsyncSession,
    *,
    search_space_id: int,
    concept_id: int | None,
    user_id: str,
    attempt_id: int,
    births: tuple[ConceptFinding, ...],
    node_entity_key: dict[str, str],
) -> int:
    """Write one ``source='detector_unkeyed'`` observation per birth finding
    whose node carries an ``entity_key`` (spec §5.2 scope boundary — a finding
    at a node with no stable key is skipped, never captured). Returns the
    number of rows actually INSERTED (idempotent on ``(attempt_id,
    signature)``, post-dedup via ``record_observation``'s shared insert core).

    ``node_entity_key`` is a ``{node_id: entity_key}`` map built caller-side
    from the reference graph (the inverse direction of
    ``opposes_index.py``'s ``key_to_node_id``) — ``ConceptFinding`` carries no
    ``entity_key`` of its own (design correction #2)."""
    inserted = 0
    for finding in births:
        entity_key = node_entity_key.get(finding.concept_key)
        if entity_key is None:
            continue
        signature = _emergent_signature(entity_key)
        inserted += await record_observation(
            db,
            search_space_id=search_space_id,
            concept_id=concept_id,
            user_id=user_id,
            attempt_id=attempt_id,
            signature=signature,
            confidence=finding.confidence,
            opposes=entity_key,
            evidence_span=finding.evidence_span or None,
            source="detector_unkeyed",
        )
    return inserted


async def record_clarification_refuted(
    db: AsyncSession,
    *,
    search_space_id: int,
    concept_id: int | None,
    user_id: str,
    attempt_id: int,
    signature: str,
    opposes: str,
    confidence: float,
    evidence_span: str | None,
) -> int:
    """Write one ``source='clarification_refuted'`` observation (spec
    §5.3.2). Thin wrapper over ``record_observation`` — the caller
    (``apollo.clarification.resolve_turn``) has already resolved
    ``signature``/``opposes`` from the refuted candidate/clarification row
    (R2: the emergent signature is ALWAYS ``emergent.<entity_key of the
    opposed reference node>`` — see ``resolve_turn.py`` for the exact
    resolution). Returns the number of rows INSERTED (0 or 1, idempotent on
    ``(attempt_id, signature)``). Does NOT commit and does NOT swallow
    exceptions — the caller owns its own failure domain."""
    return await record_observation(
        db,
        search_space_id=search_space_id,
        concept_id=concept_id,
        user_id=user_id,
        attempt_id=attempt_id,
        signature=signature,
        confidence=confidence,
        opposes=opposes,
        evidence_span=evidence_span,
        source="clarification_refuted",
    )


__all__ = ["record_detector_births", "record_clarification_refuted"]
