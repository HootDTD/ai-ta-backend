"""Reversed-provisioning mint mode: tag_and_mint with a PRE-RESOLVED concept.

With ``resolved_concept`` supplied (the closed-list matcher's output):
  * NO tag-draft LLM call (chat_fn never invoked);
  * NO concept creation — the given registered concept row is used verbatim
    (a missing row is a fail-closed TagMintError);
  * prereq edges are derived DETERMINISTICALLY from the reference graph's
    depends_on (FROM depends on TO — exactly what the S1 judge scores as
    DEPENDS_ON), replacing the LLM prereq draft that produced reversed/spurious
    edges.
The default path (resolved_concept=None) is pinned unchanged by
test_tag_mint.py. Tier-1: injected stubs, no network.
"""

from __future__ import annotations

import random
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from apollo.persistence.models import Concept, EntityPrereq, KGEntity
from apollo.provisioning.tag_mint import (
    ApprovedPair,
    ResolvedConcept,
    TagMintError,
    tag_and_mint,
)
from database.models import Course


def _never_called(*_a, **_k) -> str:
    raise AssertionError("chat_fn must NOT be called in resolved-concept mode")


def _embed_distinct(text: str) -> list[float]:
    rng = random.Random(text)
    return [rng.gauss(0.0, 1.0) for _ in range(64)]


def _problem_dict() -> dict:
    return {
        "id": "scrape.rc1",
        "concept_id": "integration-by-parts",
        "difficulty": "standard",
        "problem_text": "Evaluate integral x e^x dx.",
        "given_values": {},
        "target_unknown": "F",
        "reference_solution": [
            {
                "step": 1,
                "entry_type": "equation",
                "id": "ibp_formula",
                "content": {
                    "label": "Integration by parts",
                    "symbolic": "integral u dv = u*v - integral v du",
                    "display": True,
                },
                "depends_on": [],
            },
            {
                "step": 2,
                "entry_type": "definition",
                "id": "parts_assignment",
                "content": {"concept": "u = x, dv = e^x dx", "meaning": "split the product"},
                "depends_on": [],
            },
            {
                "step": 3,
                "entry_type": "procedure_step",
                "id": "apply_parts",
                "content": {
                    "action": "apply the parts formula",
                    "purpose": "reduce the integral",
                    "order": 1,
                    "uses_equations": ["ibp_formula"],
                },
                "depends_on": ["ibp_formula", "parts_assignment"],
            },
        ],
    }


async def _seed_concept(db, *, slug: str = "integration-by-parts") -> tuple[int, int]:
    """Returns (search_space_id, concept_id) for a registered premade concept."""
    space = Course(name=f"RC {slug}", slug=f"rc-{slug}-{random.random()}", subject_name="C2")
    db.add(space)
    await db.flush()
    subject = SimpleNamespace(slug="calculus_2", display_name="Calculus 2", search_space_id=space.id)
    concept = Concept(course_id=subject.search_space_id, subject_slug=subject.slug, subject_display_name=subject.display_name, slug=slug, display_name="Integration by Parts")
    db.add(concept)
    await db.flush()
    return int(space.id), int(concept.id)


async def test_resolved_concept_skips_tag_chat_and_uses_given_concept(db_session):
    ss, cid = await _seed_concept(db_session)
    pair = ApprovedPair(
        problem=_problem_dict(),
        search_space_id=ss,
        solution_source="extracted",
        misconceptions=[],
    )
    plan = await tag_and_mint(
        db_session,
        pair,
        chat_fn=_never_called,
        embed_fn=_embed_distinct,
        resolved_concept=ResolvedConcept(concept_id=cid, slug="integration-by-parts"),
    )
    assert plan.concept_id == cid
    assert plan.concept_slug == "integration-by-parts"
    # no NEW concept row was created
    n_concepts = (
        (await db_session.execute(select(Concept).where(Concept.slug != "provisional.inventory")))
        .scalars()
        .all()
    )
    assert len([c for c in n_concepts if int(c.id) == cid]) == 1
    # all three reference nodes minted under the resolved concept
    entities = (
        (await db_session.execute(select(KGEntity).where(KGEntity.concept_id == cid)))
        .scalars()
        .all()
    )
    keys = {str(e.canonical_key) for e in entities}
    assert {"eq.ibp_formula", "def.parts_assignment", "proc.apply_parts"} <= keys


async def test_resolved_concept_prereqs_mirror_depends_on(db_session):
    ss, cid = await _seed_concept(db_session)
    pair = ApprovedPair(
        problem=_problem_dict(),
        search_space_id=ss,
        solution_source="extracted",
        misconceptions=[],
    )
    plan = await tag_and_mint(
        db_session,
        pair,
        chat_fn=_never_called,
        embed_fn=_embed_distinct,
        resolved_concept=ResolvedConcept(concept_id=cid, slug="integration-by-parts"),
    )
    # step apply_parts depends_on [ibp_formula, parts_assignment] ==> exactly
    # those two (from, to) edges — no LLM-drafted extras, nothing reversed.
    assert sorted(plan.prereq_pairs) == [
        ("proc.apply_parts", "def.parts_assignment"),
        ("proc.apply_parts", "eq.ibp_formula"),
    ]
    assert plan.dropped_prereq_pairs == []
    rows = (
        (
            await db_session.execute(
                select(EntityPrereq)
                .join(KGEntity, EntityPrereq.from_entity_id == KGEntity.id)
                .where(KGEntity.concept_id == cid)
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 2


async def test_resolved_concept_missing_row_fails_closed(db_session):
    ss, _cid = await _seed_concept(db_session)
    pair = ApprovedPair(
        problem=_problem_dict(),
        search_space_id=ss,
        solution_source="extracted",
        misconceptions=[],
    )
    with pytest.raises(TagMintError):
        await tag_and_mint(
            db_session,
            pair,
            chat_fn=_never_called,
            embed_fn=_embed_distinct,
            resolved_concept=ResolvedConcept(concept_id=999999, slug="ghost"),
        )
