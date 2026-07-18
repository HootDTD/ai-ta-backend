"""H4 — the apollo_entity_prereqs two-kind contract guard.

apollo_entity_prereqs legitimately holds BOTH concept-level edges (seed path,
concept->concept) AND ref-node-level edges (auto path, ref-node->ref-node).
read_learner_profile's WITHIN-CONCEPT filter must consume the auto ref-node edges
and EXCLUDE the seed concept-level cross-concept edges, so prereq readers never
conflate the two kinds. DISCRIMINATING: dropping the within-concept .in_()
predicates in read_learner_profile would pull the cross-concept edge in and RED.

Tier-1 real-PG: requires the db_session fixture (re-exported in apollo/conftest.py)
and Docker-skips cleanly when the daemon is down.
"""

from __future__ import annotations

from apollo.learner_model.personalization_read import read_learner_profile
from apollo.persistence.models import Concept, EntityPrereq, KGEntity, Subject
from database.models import Course


async def test_within_concept_filter_excludes_concept_level_edges(db_session):
    space = Course(name="Course h4", slug="c-h4", subject_name="Physics")
    db_session.add(space)
    await db_session.flush()
    subj = Subject(slug="s-h4", display_name="Sub", search_space_id=space.id)
    db_session.add(subj)
    await db_session.flush()

    concept_a = Concept(subject_id=subj.id, slug="bernoulli", display_name="Bernoulli")
    concept_b = Concept(subject_id=subj.id, slug="fluids", display_name="Fluids")
    db_session.add_all([concept_a, concept_b])
    await db_session.flush()

    # concept A: two ref-node entities (the AUTO shape) + a concept-kind entity.
    eq = KGEntity(
        concept_id=concept_a.id,
        canonical_key="eq.bernoulli",
        kind="equation",
        display_name="Bernoulli eq",
        payload={},
        aliases=[],
        scope_summary="x",
    )
    proc = KGEntity(
        concept_id=concept_a.id,
        canonical_key="proc.solve_p2",
        kind="procedure",
        display_name="Solve P2",
        payload={},
        aliases=[],
        scope_summary="x",
    )
    concept_ent_a = KGEntity(
        concept_id=concept_a.id,
        canonical_key="concept.bernoulli",
        kind="concept",
        display_name="Bernoulli",
        payload={},
        aliases=[],
        scope_summary="x",
    )
    # concept B: a concept-kind entity (the cross-concept SEED edge target).
    concept_ent_b = KGEntity(
        concept_id=concept_b.id,
        canonical_key="concept.fluids",
        kind="concept",
        display_name="Fluids",
        payload={},
        aliases=[],
        scope_summary="x",
    )
    db_session.add_all([eq, proc, concept_ent_a, concept_ent_b])
    await db_session.flush()

    db_session.add_all(
        [
            EntityPrereq(from_entity_id=proc.id, to_entity_id=eq.id),  # AUTO, within A
            EntityPrereq(
                from_entity_id=concept_ent_a.id,
                to_entity_id=concept_ent_b.id,
            ),  # SEED, A->B
        ]
    )
    await db_session.flush()

    profile = await read_learner_profile(
        db_session,
        user_id="00000000-0000-0000-0000-000000000004",
        search_space_id=space.id,
        concept_id=concept_a.id,
    )
    # Only the within-concept auto ref-node edge survives; the cross-concept
    # concept-level edge (one endpoint in concept B) is excluded.
    assert profile.prereq_edges == ((proc.id, eq.id),)
