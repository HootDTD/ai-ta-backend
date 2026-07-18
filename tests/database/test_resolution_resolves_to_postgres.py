"""WU-3C2 (test 53) — full WU-3B -> 3C1 -> 3C2 chain (real Postgres + real Neo4j).

Optional end-to-end integration: seed Layer-1 ``apollo_kg_entities`` in Postgres,
``project_canon`` (WU-3C1) into the Testcontainers Neo4j, build the resolver
candidate set with the REAL ``:Canon`` surrogate keys, resolve a small student
graph, ``write_resolution``, and assert each ``RESOLVES_TO`` edge lands on the
correct ``:Canon`` node by key. Proves the resolver's ``resolved_canon_key``
matches the projection's surrogate id — the property the per-attempt edge writer
depends on.

Uses the real ``db_session`` (pgvector container) + ``neo4j_client``
(Testcontainers Neo4j) from ``tests/conftest.py`` — never Aura, never remote
Supabase. Skips cleanly when Docker is down.
"""

from __future__ import annotations

import pytest

from apollo.knowledge_graph.canon_projection import project_canon
from apollo.knowledge_graph.resolution_store import write_resolution
from apollo.knowledge_graph.store import _NODE_CREATE_CYPHER, _node_to_neo4j_props
from apollo.ontology import NODE_LABELS, build_node
from apollo.ontology.graph import KGGraph
from apollo.persistence.models import Concept, KGEntity, Subject
from apollo.resolution import resolve_attempt
from apollo.resolution.candidates import Candidate
from database.models import Course

pytestmark = pytest.mark.integration

_RESOLVED_AT = "2026-06-16T00:00:00+00:00"


async def _seed_entities(db, *, course_slug, entities):
    space = Course(name=f"Course {course_slug}", slug=course_slug, subject_name="Physics")
    db.add(space)
    await db.flush()
    subj = Subject(slug=f"s-{course_slug}", display_name="Sub", search_space_id=space.id)
    db.add(subj)
    await db.flush()
    concept = Concept(subject_id=subj.id, slug=f"k-{course_slug}", display_name="Concept")
    db.add(concept)
    await db.flush()
    key_by_canonical: dict[str, int] = {}
    for canonical_key, kind, name in entities:
        ent = KGEntity(
            concept_id=concept.id,
            canonical_key=canonical_key,
            kind=kind,
            display_name=name,
            payload={},
            aliases=[],
        )
        db.add(ent)
        await db.flush()
        key_by_canonical[canonical_key] = ent.id
    return concept.id, key_by_canonical


async def _write_node_direct(neo, attempt_id, node):
    async with neo.session() as s:
        label = NODE_LABELS[node.node_type]
        row = _node_to_neo4j_props(node)
        row["source"] = "parser"
        row["attempt_id"] = attempt_id
        await s.run(_NODE_CREATE_CYPHER[label], rows=[row])


@pytest.mark.asyncio
async def test_resolves_to_targets_projected_canon(db_session, neo4j_client):
    aid = -801
    concept_id, key_by_canonical = await _seed_entities(
        db_session,
        course_slug="e2e-resolve",
        entities=[
            ("cond.incompressibility", "condition", "Incompressibility"),
            ("misc.density_ignored", "misconception", "Density ignored"),
        ],
    )

    # WU-3C1 projection into the real container -> :Canon nodes keyed on entity id.
    await project_canon(db_session, neo4j_client, concept_id=concept_id)

    # Build the resolver candidate set with the REAL :Canon surrogate keys.
    incompressibility = Candidate(
        canonical_key="cond.incompressibility",
        canon_key=key_by_canonical["cond.incompressibility"],
        node_type="condition",
        is_misconception=False,
        symbolic=None,
        aliases=("density is constant",),
        display_name="Incompressibility",
        opposes_key=None,
    )
    candidates = (incompressibility,)

    # A single student node that resolves to incompressibility via its alias.
    student_node = build_node(
        node_type="condition",
        node_id="s1",
        attempt_id=aid,
        source="parser",
        content={"applies_when": "density is constant", "label": ""},
    )
    await _write_node_direct(neo4j_client, aid, student_node)

    result = resolve_attempt(KGGraph(nodes=[student_node]), candidates)
    assert result.resolved[0].resolved_key == "cond.incompressibility"
    expected_canon = key_by_canonical["cond.incompressibility"]
    assert result.resolved[0].resolved_canon_key == expected_canon

    out = await write_resolution(neo4j_client, aid, result, resolved_at=_RESOLVED_AT)
    assert out.edges == 1

    # The RESOLVES_TO edge lands on the correct :Canon node by key.
    async with neo4j_client.session() as s:
        rec = await (
            await s.run(
                "MATCH (n:_KGNode {attempt_id: $aid})-[:RESOLVES_TO]->(c:Canon) "
                "RETURN c.key AS key, c.canonical_key AS canonical_key",
                aid=aid,
            )
        ).single()
    assert rec["key"] == expected_canon
    assert rec["canonical_key"] == "cond.incompressibility"
