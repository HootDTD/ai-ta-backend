"""Emergent misconception map — eager τ_project materialization (2026-07-10
design §5.5 D4, Q3; plan Wave 3 T7).

``materialize_if_promotable`` is invoked from BOTH capture seams
(``apollo/handlers/done.py``'s detector-birth block, ``apollo/clarification/
resolve_turn.py``'s ``_capture_refuted``) right after their observation write
succeeds, inside that SAME failure domain. It recomputes the just-written
signature's trust via the existing derived-on-read
``apollo.emergent.store.load_class_misconceptions`` (reused — no
reimplemented trust math), and when trust crosses ``TAU_PROJECT`` it upserts
the signature as a first-class ``apollo_kg_entities kind='misconception'``
row, links its ``opposes`` edge, and projects it to ``:Canon`` — all via the
EXISTING ``apollo.provisioning.tag_mint_persist`` / ``apollo.knowledge_graph.
canon_projection`` primitives (reused, not reinvented).

Q3 rationale (plan §1): eager because there is no other read site for the
map today (the τ_assert candidate path is independent), because eager keeps
"the map is a graph" (D4) a durable, inspectable artifact the moment trust
crosses, and because every write here is idempotent
(``upsert_entity``/``link_opposes``/``MERGE``) so re-crossing re-projects
harmlessly.

Failure domain: this function itself never raises for a Neo4j-side failure
(``project_canon``) — a projection failure is logged and swallowed so the
already-flushed Postgres entity/link stays durable and the caller's grade or
resolution outcome is never affected (the plan's "own failure domain"
requirement is satisfied INSIDE this function for the projection step, on
top of the callers already wrapping the whole capture+materialize sequence
in their own try/except). A Postgres-side failure (entity upsert / link) is
NOT swallowed here — it propagates so the caller's own try/except (which
already wraps the observation write) can log + roll back the whole capture
attempt as one unit, exactly like a store-write failure would.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apollo.emergent.config import TAU_PROJECT
from apollo.emergent.store import load_class_misconceptions
from apollo.knowledge_graph.canon_projection import project_canon
from apollo.persistence.learner_model_seed import EntitySpec
from apollo.persistence.models import KGEntity
from apollo.persistence.neo4j_client import Neo4jClient
from apollo.provisioning.tag_mint_persist import link_opposes, upsert_entity

_LOG = logging.getLogger(__name__)


async def _find_entity_id(
    db: AsyncSession, *, concept_id: int, canonical_key: str
) -> int | None:
    row = (
        await db.execute(
            select(KGEntity.id)
            .where(KGEntity.concept_id == concept_id)
            .where(KGEntity.canonical_key == canonical_key)
        )
    ).scalar_one_or_none()
    return int(row) if row is not None else None


async def materialize_if_promotable(
    db: AsyncSession,
    neo: Neo4jClient | None,
    *,
    search_space_id: int,
    concept_id: int | None,
    signature: str,
    opposes_entity_key: str,
) -> None:
    """Materialize ``signature`` as a ``:Canon`` opposes-entity iff its
    derived trust has crossed ``TAU_PROJECT`` (spec §5.5, D4).

    No-ops (returns without writing anything) when:
      * ``concept_id`` is ``None`` (mirrors ``aggregate_signatures``' own
        NULL-concept no-op — a signature never keyed to a concept has no
        promotable bank to materialize into);
      * the signature is not present in this class+concept's ledger yet
        (defensive — the caller only invokes this right after its own write,
        so the row should always be there, but a re-read miss is a no-op,
        never a raise);
      * trust is below ``TAU_PROJECT``.

    ``neo=None`` (unit tests, or Neo4j not configured) skips the ``:Canon``
    projection step with a logged note — the Postgres entity + opposes link
    still materialize (that half needs no Neo4j).
    """
    if concept_id is None:
        return

    misconceptions = await load_class_misconceptions(
        db, search_space_id=search_space_id, concept_id=concept_id
    )
    match = next((m for m in misconceptions if m.signature == signature), None)
    if match is None:
        return
    if match.trust < TAU_PROJECT:
        return

    spec = EntitySpec(
        canonical_key=signature,
        kind="misconception",
        display_name=signature,
        payload={"opposes_entity_key": opposes_entity_key},
    )
    entity_id, _inserted = await upsert_entity(
        db, concept_id=concept_id, spec=spec, scope_summary=None
    )

    opposed_id = await _find_entity_id(
        db, concept_id=concept_id, canonical_key=opposes_entity_key
    )
    if opposed_id is None:
        _LOG.warning(
            "opposes_entity_missing signature=%s opposes_entity_key=%s concept_id=%s "
            "-- misconception entity materialized without a resolved opposes link",
            signature,
            opposes_entity_key,
            concept_id,
        )
    else:
        await link_opposes(
            db, concept_id=concept_id, key_to_id={opposes_entity_key: opposed_id}
        )

    if neo is None:
        _LOG.info(
            "canon_projection_skipped signature=%s concept_id=%s reason=neo_client_absent",
            signature,
            concept_id,
        )
        return

    try:
        await project_canon(db, neo, concept_id=concept_id)
    except Exception:
        _LOG.exception(
            "canon_projection_failed signature=%s concept_id=%s entity_id=%s -- "
            "Postgres entity/opposes link already durable, grade/resolution unaffected",
            signature,
            concept_id,
            entity_id,
        )


__all__ = ["materialize_if_promotable"]
