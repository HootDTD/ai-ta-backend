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


async def _resolve_full_opposes_key_to_id(
    db: AsyncSession, *, concept_id: int
) -> dict[str, int]:
    """Build the COMPLETE ``key_to_id`` map ``link_opposes`` needs (fix for
    the Finding-1 KeyError, 2026-07-10 fix wave).

    ``tag_mint_persist.link_opposes`` iterates EVERY ``kind='misconception'``
    ``KGEntity`` row in the concept and does a hard ``key_to_id[opposes_key]``
    lookup for each one's ``payload.opposes_entity_key`` — not just the
    single signature this call is materializing. A caller that passes a
    single-entry map (as this module used to) raises ``KeyError`` the moment
    the concept has >= 2 misconceptions with distinct ``opposes_entity_key``s
    (including an AUTHORED one minted at provisioning time, whose
    ``opposes_entity_key`` was never queried here before). This resolves the
    SAME set ``link_opposes`` will walk: every misconception entity's
    ``opposes_entity_key``, each looked up by ``canonical_key`` in one extra
    query. A key with no matching entity is simply absent from the returned
    map (mirrors ``_find_entity_id``'s own miss-is-None contract) —
    ``link_opposes`` still raises for that key, preserving provisioning's
    fail-closed KeyError semantics; this helper only widens the map to cover
    every key that legitimately resolves, so a healthy multi-misconception
    concept no longer trips a KeyError for keys that DO resolve.
    """
    misconception_rows = (
        (
            await db.execute(
                select(KGEntity.payload)
                .where(KGEntity.concept_id == concept_id)
                .where(KGEntity.kind == "misconception")
            )
        )
        .scalars()
        .all()
    )
    opposes_keys = {
        key
        for payload in misconception_rows
        if (key := (payload or {}).get("opposes_entity_key"))
    }
    if not opposes_keys:
        return {}

    rows = (
        await db.execute(
            select(KGEntity.canonical_key, KGEntity.id)
            .where(KGEntity.concept_id == concept_id)
            .where(KGEntity.canonical_key.in_(opposes_keys))
        )
    ).all()
    return {str(key): int(entity_id) for key, entity_id in rows}


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
        # Finding 1 fix: link_opposes re-walks EVERY misconception entity in
        # the concept (not just the one just upserted) and hard-looks-up each
        # one's opposes_entity_key in key_to_id -- a single-entry map raises
        # KeyError the moment a second misconception (emergent or authored)
        # already exists on this concept. Resolve the full map first.
        key_to_id = await _resolve_full_opposes_key_to_id(db, concept_id=concept_id)
        key_to_id.setdefault(opposes_entity_key, opposed_id)
        await link_opposes(db, concept_id=concept_id, key_to_id=key_to_id)

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
