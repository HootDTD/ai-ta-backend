"""WU-3B2d — tag/mint (stage 4) tests (Tier-1 unit + real-PG).

Tier-1 ONLY — NO network. The concept-tag / prereq-draft LLM (`chat_fn`) and the
scope_summary embedder (`embed_fn`, used via `resolve_candidate`) are
DETERMINISTIC injected stubs; there is NO real OpenAI / `embed_text` /
`cheap_chat` call anywhere in this module (ADJ #10). Real-PG tests request the
``db_session`` fixture (re-exported in ``apollo/conftest.py``) and Docker-skip
cleanly when the daemon is down — but the WU-3B2d gate REQUIRES they run
GREEN-not-skipped (like 3B2c).

DISCRIMINATING by design (independent-mutation discipline):
  * ``test_variable_mapping_entry_type_mints`` REDs if the additive
    ``variable_mapping`` key is reverted from ``_ENTRY_TYPE_TO_KIND_PREFIX``.
  * ``test_tag_and_mint_authors_canonical_symbols`` REDs if symbol authoring is
    dropped (gate-4 would be vacuous).
  * ``test_tag_and_mint_idempotent`` REDs if the (concept_id, canonical_key)
    upsert guard is dropped (duplicate entities).
  * ``test_variable_mapping_passes_gate1_mintmap_subcheck`` ties the frozen-map
    extension to its 3B2b consumer.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from apollo.persistence.learner_model_seed import (
    _ENTRY_TYPE_TO_KIND_PREFIX,
    reference_solution_to_entities,
)
from apollo.persistence.models import (
    Concept,
    EntityPrereq,
    LearnerEntity,
)
from apollo.provisioning import run_promotion_lint
from apollo.provisioning.tag_mint import (
    ApprovedPair,
    MintPlan,
    TagMintError,
    tag_and_mint,
)
from database.models import Course

# pytest.ini sets asyncio_mode = auto, so async tests need no mark and the pure
# tests stay sync.


# --------------------------------------------------------------------------- #
# Deterministic stubs (NO network) — chat_fn (concept tag + prereqs), embed_fn
# --------------------------------------------------------------------------- #


def _chat_returning(payload: dict):
    """A cheap_chat-shaped sync stub returning a fixed JSON string. The mocked
    LLM template (test_leakage_judge.py:31-36 / test_dedup.py's `_judge_*`)."""

    def _chat(*_a, **_k) -> str:
        return json.dumps(payload)

    return _chat


# A concept-tag + prereq-draft response: the LLM proposes a concept slug + a
# couple of prereq edges between minted entity canonical_keys.
def _tag_payload(
    *,
    concept_slug: str = "bernoulli_principle",
    display_name: str = "Bernoulli Principle",
    prereqs: list[list[str]] | None = None,
) -> dict:
    return {
        "concept_slug": concept_slug,
        "display_name": display_name,
        "prereqs": prereqs if prereqs is not None else [],
    }


def _embed_distinct(text: str) -> list[float]:
    """Deterministic embedder: every distinct scope_summary maps to a DIFFERENT
    near-orthogonal high-dimensional vector, so resolve_candidate finds NO
    embedding merge (cos < 0.82) for distinct entities and every candidate mints a
    fresh entity. Stable per text (idempotency proof needs identical-text →
    identical-vector). A deterministic per-text random vector in R^64 has expected
    pairwise cosine ≈ 0 (far below the 0.82 band), so distinct texts never merge;
    identical text yields the identical seed → identical vector → cosine 1.0."""
    import random

    rng = random.Random(text)  # seed on the text → deterministic per text
    return [rng.gauss(0.0, 1.0) for _ in range(64)]


def _judge_distinct(*_a, **_k) -> str:
    return "distinct"


def _embed_in_band(text: str) -> list[float]:
    """A deterministic embedder that places the FIRST entity to be resolved at a
    cosine inside the escalate-to-judge band (0.82<=cos<0.92) relative to a
    pre-seeded in-course entity, so resolve_candidate escalates to the injected
    judge tier (exercising tag_and_mint's ``_judge_distinct`` callable). All texts
    that are not the pre-seeded one map onto a fixed reference axis; the pre-seeded
    'BANDSEED' text maps to a unit vector at cosine 0.87 with that axis."""
    import math

    if text == "BANDSEED":
        return [0.87, math.sqrt(max(0.0, 1.0 - 0.87 * 0.87)), 0.0, 0.0]
    return [1.0, 0.0, 0.0, 0.0]


# --------------------------------------------------------------------------- #
# Fixtures: a Problem-validatable approved pair (a minimal bernoulli-shaped one)
# --------------------------------------------------------------------------- #


def _problem_dict(
    *,
    problem_id: str = "scrape.p1",
    concept_slug: str = "bernoulli_principle",
    given: dict[str, float] | None = None,
    target: str = "P2",
    extra_steps: list[dict] | None = None,
) -> dict:
    """A minimal Problem-validatable dict (schema: id, concept_id, difficulty,
    problem_text, given_values, target_unknown, reference_solution[...])."""
    given = given if given is not None else {"P1": 200000.0, "v1": 2.0, "rho": 1000.0}
    steps: list[dict] = [
        {
            "step": 1,
            "entry_type": "equation",
            "id": "bernoulli",
            "content": {
                "label": "Bernoulli equation",
                "symbolic": "P1 + 0.5*rho*v1**2 - P2 - 0.5*rho*v2**2",
            },
            "depends_on": [],
        },
        {
            "step": 2,
            "entry_type": "procedure_step",
            "id": "solve_p2",
            "content": {
                "action": "Solve for P2",
                "purpose": "isolate the unknown pressure",
                "order": 1,
                "uses_equations": ["bernoulli"],
            },
            "depends_on": ["bernoulli"],
        },
    ]
    if extra_steps:
        steps.extend(extra_steps)
    return {
        "id": problem_id,
        "concept_id": concept_slug,
        "difficulty": "intro",
        "problem_text": "A fluid speeds up in a pipe; find the downstream pressure P2.",
        "given_values": given,
        "target_unknown": target,
        "reference_solution": steps,
    }


def _approved_pair(
    *,
    problem: dict | None = None,
    search_space_id: int,
    misconceptions: list[dict] | None = None,
) -> ApprovedPair:
    return ApprovedPair(
        problem=problem if problem is not None else _problem_dict(),
        search_space_id=search_space_id,
        solution_source="extracted",
        misconceptions=misconceptions or [],
    )


async def _seed_course(db, *, slug: str):
    """Seed Course -> Subject for one course. Returns (search_space_id,
    subject_id). The concept is resolved/created by tag_and_mint itself."""
    space = Course(name=f"Course {slug}", slug=slug, subject_name="Physics")
    db.add(space)
    await db.flush()
    subj = SimpleNamespace(slug=f"s-{slug}", display_name="Sub", search_space_id=space.id)
    return space.id, subj


# --------------------------------------------------------------------------- #
# Step 1 — frozen-map extension (pure, no DB)
# --------------------------------------------------------------------------- #


def test_variable_mapping_entry_type_mints():
    """``reference_solution_to_entities`` on a ``variable_mapping`` step yields an
    EntitySpec with kind='variable' and key 'varmap.<id>' (no KeyError).
    DISCRIMINATING: reverting the additive map key REDs (KeyError)."""
    problem = {
        "reference_solution": [
            {
                "step": 1,
                "entry_type": "variable_mapping",
                "id": "p_to_pressure",
                "content": {"label": "P maps to pressure"},
            }
        ]
    }
    specs = reference_solution_to_entities(problem)
    assert len(specs) == 1
    assert specs[0].kind == "variable"
    assert specs[0].canonical_key == "varmap.p_to_pressure"


def test_variable_mapping_in_mint_map():
    """The frozen map now contains the additive key (the load-bearing membership
    the 3B2b gate-1 sub-check reads)."""
    assert _ENTRY_TYPE_TO_KIND_PREFIX["variable_mapping"] == ("variable", "varmap")


def test_approvedpair_and_mintplan_shapes():
    """Pydantic round-trip of ApprovedPair / MintPlan; required fields enforced."""
    pair = ApprovedPair(problem={"id": "p"}, search_space_id=1, solution_source="extracted")
    assert pair.misconceptions == []
    plan = MintPlan(
        concept_id=5,
        concept_slug="bernoulli_principle",
        authored_symbols=["P", "v"],
        minted_entity_ids={"eq.bernoulli": 9},
        merged_entity_keys=[],
        prereq_pairs=[("eq.bernoulli", "var.P")],
        misconception_keys=[],
    )
    assert plan.minted_entity_ids["eq.bernoulli"] == 9
    with pytest.raises(Exception):  # noqa: B017
        ApprovedPair(problem={"id": "p"})  # missing required fields


def test_author_symbol_set_from_problem():
    """``_author_symbol_set`` derives a non-empty, deterministic symbol set from
    given_values keys + target_unknown + equation free-symbols, each reduced to
    its subscript base (P1/P2 → P)."""
    from apollo.provisioning.tag_mint import _author_symbol_set

    problem = _problem_dict()
    symbols, normalization = _author_symbol_set(problem)
    assert symbols  # non-empty
    assert symbols == _author_symbol_set(problem)[0]  # deterministic
    assert "P" in symbols  # P1/P2 reduced to base P
    assert "v" in symbols
    # the subscripted raw symbols normalize to their base.
    assert normalization.get("P1") == "P"
    assert normalization.get("P2") == "P"


# --------------------------------------------------------------------------- #
# Real-PG — concept resolution + symbol authoring
# --------------------------------------------------------------------------- #


async def test_resolve_or_create_concept_slug_to_bigint(db_session):
    """slug → BIGINT; creates the concept if absent; re-resolves to the SAME id
    (idempotent; the §6 namespace contract — key on BIGINT, never slug)."""
    from apollo.provisioning.tag_mint_persist import resolve_or_create_concept

    ss_id, _subj = await _seed_course(db_session, slug="c-resolve")
    cid1 = await resolve_or_create_concept(
        db_session,
        search_space_id=ss_id,
        slug="bernoulli_principle",
        display_name="Bernoulli",
    )
    cid2 = await resolve_or_create_concept(
        db_session,
        search_space_id=ss_id,
        slug="bernoulli_principle",
        display_name="Bernoulli",
    )
    assert isinstance(cid1, int)
    assert cid1 == cid2


async def test_tag_and_mint_authors_canonical_symbols(db_session):
    """GATE-4 NON-VACUITY. After mint, reload the concept; canonical_symbols AND
    normalization_map are NON-EMPTY (so a 3B2b gate-4 over it can fire)."""
    ss_id, _subj = await _seed_course(db_session, slug="c-author")
    pair = _approved_pair(search_space_id=ss_id)
    plan = await tag_and_mint(
        db_session,
        pair,
        chat_fn=_chat_returning(_tag_payload()),
        embed_fn=_embed_distinct,
    )
    assert plan.authored_symbols  # non-empty this call
    concept = (
        await db_session.execute(select(Concept).where(Concept.id == plan.concept_id))
    ).scalar_one()
    assert concept.canonical_symbols  # non-empty dict
    # Stored in the CanonicalSymbols shape ({"symbols": [...]}), NOT a flat
    # {symbol: True} map — the runtime reader requires the list form.
    assert isinstance(concept.canonical_symbols, list)
    assert concept.canonical_symbols  # non-empty symbol list
    assert concept.normalization_map  # non-empty (P1/P2 → P)


async def test_authored_canonical_symbols_round_trip_through_reader(db_session):
    """REGRESSION (HIGH): a tag_and_mint-authored concept MUST load through the
    real runtime reader ``load_concept_definition`` →
    ``CanonicalSymbols.model_validate`` (apollo/subjects/curriculum_db.py:101),
    which is hit on every Apollo teaching session. The authored
    ``canonical_symbols`` therefore has to be a CanonicalSymbols-validatable dict
    ({"symbols": [...], ...}), NOT a flat {symbol: True} map. DISCRIMINATING:
    reverting the author shape to {symbol: True} REDs this with a pydantic
    ValidationError (symbols Field required)."""
    from apollo.subjects.curriculum_db import load_concept_definition

    ss_id, _subj = await _seed_course(db_session, slug="c-roundtrip")
    pair = _approved_pair(search_space_id=ss_id)
    plan = await tag_and_mint(
        db_session,
        pair,
        chat_fn=_chat_returning(_tag_payload()),
        embed_fn=_embed_distinct,
    )

    # The real teaching-session reader must accept the authored columns.
    definition = await load_concept_definition(
        db_session, concept_id=plan.concept_id, search_space_id=ss_id
    )
    assert definition.canonical_symbols.symbols  # list[str], non-empty
    # the authored symbols survive the round-trip (base symbols P/v/rho present).
    for sym in plan.authored_symbols:
        assert sym in definition.canonical_symbols.symbols


async def test_author_symbols_first_writer_wins_union(db_session):
    """A SECOND tag_and_mint with a DIFFERENT problem UNIONs new symbols and does
    NOT rewrite the first writer's existing canonical symbols (§8B.5)."""
    ss_id, _subj = await _seed_course(db_session, slug="c-union")
    # first problem: symbols {P, v, rho}
    pair1 = _approved_pair(search_space_id=ss_id)
    plan1 = await tag_and_mint(
        db_session,
        pair1,
        chat_fn=_chat_returning(_tag_payload()),
        embed_fn=_embed_distinct,
    )
    concept = (
        await db_session.execute(select(Concept).where(Concept.id == plan1.concept_id))
    ).scalar_one()
    # canonical_symbols is the CanonicalSymbols shape ({"symbols": [...]}); the
    # union operates over that LIST, not over dict keys.
    first_symbols = list(concept.canonical_symbols)

    # second problem: introduces a NEW symbol Q (different given/target).
    problem2 = _problem_dict(
        problem_id="scrape.p2",
        given={"Q": 5.0, "P1": 100000.0},
        target="A",
        extra_steps=[],
    )
    pair2 = _approved_pair(problem=problem2, search_space_id=ss_id)
    await tag_and_mint(
        db_session,
        pair2,
        chat_fn=_chat_returning(_tag_payload()),
        embed_fn=_embed_distinct,
    )
    await db_session.refresh(concept)
    union_symbols = list(concept.canonical_symbols)
    # the first writer's symbols all survive (never rewritten)...
    for sym in first_symbols:
        assert sym in union_symbols
    # ...and the new symbol is unioned in.
    assert "Q" in union_symbols
    assert "A" in union_symbols


# --------------------------------------------------------------------------- #
# Real-PG — mint reference + misconception entities, dedup, prereqs
# --------------------------------------------------------------------------- #


async def test_tag_and_mint_mints_reference_entities(db_session):
    """The reference steps become apollo_kg_entities rows with the frozen
    converter's keys/kinds, reachable from MintPlan.minted_entity_ids."""
    ss_id, _subj = await _seed_course(db_session, slug="c-mint")
    pair = _approved_pair(search_space_id=ss_id)
    plan = await tag_and_mint(
        db_session,
        pair,
        chat_fn=_chat_returning(_tag_payload()),
        embed_fn=_embed_distinct,
    )
    # the bernoulli equation + the solve_p2 procedure step mint.
    assert "eq.bernoulli" in plan.minted_entity_ids
    assert "proc.solve_p2" in plan.minted_entity_ids
    rows = (
        (await db_session.execute(select(LearnerEntity).where(LearnerEntity.concept_id == plan.concept_id)))
        .scalars()
        .all()
    )
    by_key = {r.canonical_key: r for r in rows}
    assert by_key["eq.bernoulli"].kind == "equation"
    assert by_key["proc.solve_p2"].kind == "procedure"
    # scope_summary authored (the dedup embedding source) — non-null.
    assert by_key["eq.bernoulli"].scope_summary


async def test_minted_misconception_is_observability_only_not_persisted(db_session):
    """DB-13: the app-schema ``learner_entities__kind__check`` dropped
    'misconception', so a misconception is surfaced ONLY via
    ``MintPlan.misconception_keys`` (observability) — it is NEVER written as a
    ``LearnerEntity`` row (no write to ``apollo_misconceptions`` either)."""
    ss_id, _subj = await _seed_course(db_session, slug="c-misc")
    misc = [
        {
            "key": "misc.pressure_follows_speed",
            "display_name": "Pressure follows speed",
            "description": "thinks higher speed means higher pressure",
            "opposes": "eq.bernoulli",
            "trigger_phrases": ["pressure goes up with speed"],
        }
    ]
    pair = _approved_pair(search_space_id=ss_id, misconceptions=misc)
    plan = await tag_and_mint(
        db_session,
        pair,
        chat_fn=_chat_returning(_tag_payload()),
        embed_fn=_embed_distinct,
    )
    assert "misc.pressure_follows_speed" in plan.misconception_keys
    rows = (
        (
            await db_session.execute(
                select(LearnerEntity)
                .where(LearnerEntity.concept_id == plan.concept_id)
                .where(LearnerEntity.kind == "misconception")
            )
        )
        .scalars()
        .all()
    )
    assert rows == []  # no LearnerEntity row ever minted for the misconception



async def test_tag_and_mint_dedups_via_resolve_candidate(db_session):
    """When a candidate entity's scope_summary matches an existing in-course
    entity (≥0.92 cosine), tag_and_mint MERGES (reuses the id, no new row) and
    records it in MintPlan.merged_entity_keys."""
    ss_id, subj_id = await _seed_course(db_session, slug="c-dedup")
    # Pre-seed a concept + an entity whose canonical_key EXACTLY matches one the
    # mint will produce (eq.bernoulli) so the slug tier merges it deterministically.
    concept = Concept(course_id=subj_id.search_space_id, subject_slug=subj_id.slug, subject_display_name=subj_id.display_name, slug="bernoulli_principle", display_name="Bernoulli")
    db_session.add(concept)
    await db_session.flush()
    existing = LearnerEntity(
        course_id=ss_id,
        concept_id=concept.id,
        canonical_key="eq.bernoulli",
        kind="equation",
        display_name="Bernoulli equation (pre-existing)",
        payload={},
        aliases=[],
        scope_summary="pre-existing bernoulli",
    )
    db_session.add(existing)
    await db_session.flush()
    existing_id = existing.id

    pair = _approved_pair(search_space_id=ss_id)
    plan = await tag_and_mint(
        db_session,
        pair,
        chat_fn=_chat_returning(_tag_payload()),
        embed_fn=_embed_distinct,
    )
    # eq.bernoulli merged onto the pre-existing entity (slug-exact), not re-minted.
    assert "eq.bernoulli" in plan.merged_entity_keys
    assert "eq.bernoulli" not in plan.minted_entity_ids
    rows = (
        (
            await db_session.execute(
                select(LearnerEntity)
                .where(LearnerEntity.concept_id == concept.id)
                .where(LearnerEntity.canonical_key == "eq.bernoulli")
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1  # no duplicate row
    assert rows[0].id == existing_id


def _embed_collide(_text: str) -> list[float]:
    """Every scope_summary maps to the SAME vector, so any two specs embed at
    cosine 1.0 and WOULD fuse on the embedding tier -- UNLESS the dedup pool
    excludes entities minted earlier in the same mint. Models the audit's m≡M
    fusion (distinct variables whose thin scope_summaries embed identically)."""
    return [1.0, 0.0, 0.0, 0.0]


async def test_tag_and_mint_keeps_same_problem_specs_distinct(db_session):
    """PR2 Part B: two distinct reference-solution nodes of ONE problem must mint as
    DISTINCT entities even when their scope_summaries embed identically (the m≡M
    fusion). The dedup pool excludes entities minted earlier in the SAME call, so
    nothing fuses within a problem. DISCRIMINATING: without the same-mint exclusion
    the 2nd spec merges into the 1st (merged_entity_keys non-empty, 1 minted)."""
    ss_id, _subj = await _seed_course(db_session, slug="c-samemint")
    pair = _approved_pair(search_space_id=ss_id)  # default 2-spec bernoulli problem
    plan = await tag_and_mint(
        db_session, pair, chat_fn=_chat_returning(_tag_payload()), embed_fn=_embed_collide
    )
    assert plan.merged_entity_keys == []  # nothing fused within the problem
    assert set(plan.minted_entity_ids) == {"eq.bernoulli", "proc.solve_p2"}
    assert len(set(plan.minted_entity_ids.values())) == 2  # two DISTINCT entity ids


async def test_tag_and_mint_prereqs_inserted(db_session):
    """Drafted prereq pairs land in apollo_entity_prereqs (skip on re-run)."""
    ss_id, _subj = await _seed_course(db_session, slug="c-prereq")
    pair = _approved_pair(search_space_id=ss_id)
    tag = _tag_payload(prereqs=[["proc.solve_p2", "eq.bernoulli"]])
    plan = await tag_and_mint(
        db_session,
        pair,
        chat_fn=_chat_returning(tag),
        embed_fn=_embed_distinct,
    )
    assert plan.prereq_pairs == [("proc.solve_p2", "eq.bernoulli")]
    from_id = plan.minted_entity_ids["proc.solve_p2"]
    to_id = plan.minted_entity_ids["eq.bernoulli"]
    edges = (
        (
            await db_session.execute(
                select(EntityPrereq)
                .where(EntityPrereq.from_entity_id == from_id)
                .where(EntityPrereq.to_entity_id == to_id)
            )
        )
        .scalars()
        .all()
    )
    assert len(edges) == 1


async def test_tag_and_mint_prereqs_accept_bare_ids(db_session):
    """REGRESSION (BLOCKER): the LLM tag prompt never sees the canonical-key
    prefix scheme, so it drafts prereqs by BARE reference-node id (solve_p2 /
    bernoulli), not the prefixed canonical_key (proc.solve_p2 / eq.bernoulli).
    tag_and_mint must resolve the bare ids to the minted entities and insert the
    edge. DISCRIMINATING: reverting the bare-id alias REDs here — the edge would
    no longer resolve and would be dropped (5b), leaving 0 edges instead of 1."""
    ss_id, _subj = await _seed_course(db_session, slug="c-bareid")
    pair = _approved_pair(search_space_id=ss_id)
    tag = _tag_payload(prereqs=[["solve_p2", "bernoulli"]])  # BARE ids
    plan = await tag_and_mint(
        db_session, pair, chat_fn=_chat_returning(tag), embed_fn=_embed_distinct
    )
    from_id = plan.minted_entity_ids["proc.solve_p2"]
    to_id = plan.minted_entity_ids["eq.bernoulli"]
    edges = (
        (
            await db_session.execute(
                select(EntityPrereq)
                .where(EntityPrereq.from_entity_id == from_id)
                .where(EntityPrereq.to_entity_id == to_id)
            )
        )
        .scalars()
        .all()
    )
    assert len(edges) == 1


# --------------------------------------------------------------------------- #
# PR3 — acyclicity guard at mint (audit bug #3): a drafted prereq cycle or
# self-loop must NOT persist in apollo_entity_prereqs. The guard runs over the
# resolved ENTITY-ID graph (via key_to_id), so dedup-merged / bare-id-aliased
# keys that collapse onto ONE id are caught too.
# --------------------------------------------------------------------------- #
def test_acyclic_prereq_pairs_drops_self_loop():
    from apollo.provisioning.tag_mint import _acyclic_prereq_pairs

    kept, dropped = _acyclic_prereq_pairs([("a", "a")], {"a": 1})
    assert kept == []
    assert dropped == [("a", "a")]


def test_acyclic_prereq_pairs_breaks_two_cycle_keeping_first():
    from apollo.provisioning.tag_mint import _acyclic_prereq_pairs

    kept, dropped = _acyclic_prereq_pairs([("a", "b"), ("b", "a")], {"a": 1, "b": 2})
    assert kept == [("a", "b")]  # first edge wins (deterministic input order)
    assert dropped == [("b", "a")]


def test_acyclic_prereq_pairs_breaks_three_cycle():
    from apollo.provisioning.tag_mint import _acyclic_prereq_pairs

    key_to_id = {"a": 1, "b": 2, "c": 3}
    kept, dropped = _acyclic_prereq_pairs([("a", "b"), ("b", "c"), ("c", "a")], key_to_id)
    assert kept == [("a", "b"), ("b", "c")]
    assert dropped == [("c", "a")]


def test_acyclic_prereq_pairs_preserves_dag():
    from apollo.provisioning.tag_mint import _acyclic_prereq_pairs

    key_to_id = {"a": 1, "b": 2, "c": 3}
    pairs = [("a", "b"), ("a", "c"), ("b", "c")]
    kept, dropped = _acyclic_prereq_pairs(pairs, key_to_id)
    assert kept == pairs
    assert dropped == []


def test_acyclic_prereq_pairs_handles_diamond_reconvergence():
    """A diamond (x->a->c, x->b->c) makes the reachability DFS reach a shared node
    by two paths; the guard must still keep an acyclic edge into the diamond root
    (no false cycle) while traversing the reconvergence."""
    from apollo.provisioning.tag_mint import _acyclic_prereq_pairs

    key_to_id = {"x": 1, "a": 2, "b": 3, "c": 4, "d": 5}
    pairs = [("x", "a"), ("x", "b"), ("a", "c"), ("b", "c"), ("d", "x")]
    kept, dropped = _acyclic_prereq_pairs(pairs, key_to_id)
    assert kept == pairs  # d->x does not reach back into the diamond -> no cycle
    assert dropped == []


def test_acyclic_prereq_pairs_drops_merge_collapsed_self_loop():
    """When dedup merges two distinct keys onto ONE entity id, an edge between them
    collapses to a self-loop in the ID graph — the guard must catch it even though
    the KEYS differ (this is why the check resolves through key_to_id). Mirrors the
    audit's m≡M fusion (both keys -> entity 751)."""
    from apollo.provisioning.tag_mint import _acyclic_prereq_pairs

    key_to_id = {"varmap.vm_m": 751, "varmap.vm_M": 751, "eq.tension": 755}
    kept, dropped = _acyclic_prereq_pairs(
        [("varmap.vm_m", "varmap.vm_M"), ("eq.tension", "varmap.vm_M")], key_to_id
    )
    assert ("varmap.vm_m", "varmap.vm_M") in dropped  # 751 -> 751 self-loop
    assert kept == [("eq.tension", "varmap.vm_M")]  # 755 -> 751, fine


async def test_tag_and_mint_drops_cyclic_prereq_edge(db_session):
    """A drafted 2-cycle (A->B, B->A) must not persist as two directed rows: the
    acyclicity guard drops the reverse edge so apollo_entity_prereqs stays a DAG.
    DISCRIMINATING: removing the guard REDs (both directed rows would persist)."""
    ss_id, _subj = await _seed_course(db_session, slug="c-cycle")
    pair = _approved_pair(search_space_id=ss_id)
    tag = _tag_payload(
        prereqs=[["eq.bernoulli", "proc.solve_p2"], ["proc.solve_p2", "eq.bernoulli"]]
    )
    plan = await tag_and_mint(
        db_session, pair, chat_fn=_chat_returning(tag), embed_fn=_embed_distinct
    )
    a = plan.minted_entity_ids["eq.bernoulli"]
    b = plan.minted_entity_ids["proc.solve_p2"]
    rows = (
        (
            await db_session.execute(
                select(EntityPrereq).where(
                    EntityPrereq.from_entity_id.in_([a, b]),
                    EntityPrereq.to_entity_id.in_([a, b]),
                )
            )
        )
        .scalars()
        .all()
    )
    directed = {(r.from_entity_id, r.to_entity_id) for r in rows}
    assert directed == {(a, b)}  # only the first edge; the reverse was dropped
    assert plan.prereq_pairs == [("eq.bernoulli", "proc.solve_p2")]


# --------------------------------------------------------------------------- #
# M2 — cross-mint prereq cycle guard: two SEPARATE ``tag_and_mint`` calls into
# the SAME shared concept must not, together, persist a directed cycle. The
# in-mint-only acyclicity check (above) cannot see this by construction — each
# call starts ``adj`` empty — so the guard must SEED from persisted
# ``apollo_entity_prereqs`` rows (``load_concept_prereq_adjacency``).
# --------------------------------------------------------------------------- #


async def test_cross_mint_prereq_cycle_dropped(db_session):
    """Mint 1 persists A->B (proc.solve_p2 -> eq.bernoulli). Mint 2, drafting into
    the SAME shared concept, proposes the REVERSE B->A. DISCRIMINATING: without
    seeding the acyclicity DFS from mint 1's persisted edge, mint 2 passes its own
    (empty-``adj``) check and the reverse edge is inserted, persisting a 2-cycle."""
    ss_id, _subj = await _seed_course(db_session, slug="c-crossmint")
    pair = _approved_pair(search_space_id=ss_id)

    tag1 = _tag_payload(prereqs=[["proc.solve_p2", "eq.bernoulli"]])
    plan1 = await tag_and_mint(
        db_session, pair, chat_fn=_chat_returning(tag1), embed_fn=_embed_distinct
    )
    assert plan1.prereq_pairs == [("proc.solve_p2", "eq.bernoulli")]

    tag2 = _tag_payload(prereqs=[["eq.bernoulli", "proc.solve_p2"]])
    plan2 = await tag_and_mint(
        db_session, pair, chat_fn=_chat_returning(tag2), embed_fn=_embed_distinct
    )
    assert plan2.prereq_pairs == []
    assert ("eq.bernoulli", "proc.solve_p2") in plan2.dropped_prereq_pairs

    a = plan1.minted_entity_ids["proc.solve_p2"]
    b = plan1.minted_entity_ids["eq.bernoulli"]
    rows = (
        (
            await db_session.execute(
                select(EntityPrereq).where(
                    EntityPrereq.from_entity_id.in_([a, b]),
                    EntityPrereq.to_entity_id.in_([a, b]),
                )
            )
        )
        .scalars()
        .all()
    )
    directed = {(r.from_entity_id, r.to_entity_id) for r in rows}
    assert directed == {(a, b)}  # only mint 1's original edge persists


async def test_cross_mint_prereq_longer_chain_cycle_dropped(db_session):
    """A longer cross-mint cycle is caught too: mint 1 persists A->B. Mint 2 (into
    the same shared concept, introducing a new node C) drafts B->C AND C->A. The
    first (B->C) is legitimate on arrival and inserts; the second (C->A) closes
    A->B->C->A and is dropped."""
    ss_id, _subj = await _seed_course(db_session, slug="c-crosschain")
    pair1 = _approved_pair(search_space_id=ss_id)
    tag1 = _tag_payload(prereqs=[["proc.solve_p2", "eq.bernoulli"]])
    plan1 = await tag_and_mint(
        db_session, pair1, chat_fn=_chat_returning(tag1), embed_fn=_embed_distinct
    )
    assert plan1.prereq_pairs == [("proc.solve_p2", "eq.bernoulli")]

    problem2 = _problem_dict(
        problem_id="scrape.p2-chain",
        extra_steps=[
            {
                "step": 3,
                "entry_type": "equation",
                "id": "third",
                "content": {"label": "third eq", "symbolic": "P1 - P2"},
                "depends_on": [],
            }
        ],
    )
    pair2 = _approved_pair(problem=problem2, search_space_id=ss_id)
    tag2 = _tag_payload(prereqs=[["eq.bernoulli", "eq.third"], ["eq.third", "proc.solve_p2"]])
    plan2 = await tag_and_mint(
        db_session, pair2, chat_fn=_chat_returning(tag2), embed_fn=_embed_distinct
    )
    assert plan2.prereq_pairs == [("eq.bernoulli", "eq.third")]
    assert ("eq.third", "proc.solve_p2") in plan2.dropped_prereq_pairs

    a = plan1.minted_entity_ids["proc.solve_p2"]
    b = plan1.minted_entity_ids["eq.bernoulli"]
    c = plan2.minted_entity_ids["eq.third"]
    rows = (
        (
            await db_session.execute(
                select(EntityPrereq).where(
                    EntityPrereq.from_entity_id.in_([a, b, c]),
                    EntityPrereq.to_entity_id.in_([a, b, c]),
                )
            )
        )
        .scalars()
        .all()
    )
    directed = {(r.from_entity_id, r.to_entity_id) for r in rows}
    assert directed == {(a, b), (b, c)}  # the closing edge (c, a) never persisted


async def test_cross_mint_legit_dag_addition_still_inserts(db_session):
    """A SECOND mint into the shared concept that adds a genuinely acyclic edge
    (A->C, no path back to A) inserts normally — the cross-mint seed only drops
    edges that would actually close a cycle, not every edge touching a
    previously-minted entity."""
    ss_id, _subj = await _seed_course(db_session, slug="c-crosslegit")
    pair1 = _approved_pair(search_space_id=ss_id)
    tag1 = _tag_payload(prereqs=[["proc.solve_p2", "eq.bernoulli"]])
    plan1 = await tag_and_mint(
        db_session, pair1, chat_fn=_chat_returning(tag1), embed_fn=_embed_distinct
    )

    problem2 = _problem_dict(
        problem_id="scrape.p2-legit",
        extra_steps=[
            {
                "step": 3,
                "entry_type": "equation",
                "id": "third",
                "content": {"label": "third eq", "symbolic": "P1 - P2"},
                "depends_on": [],
            }
        ],
    )
    pair2 = _approved_pair(problem=problem2, search_space_id=ss_id)
    tag2 = _tag_payload(prereqs=[["proc.solve_p2", "eq.third"]])
    plan2 = await tag_and_mint(
        db_session, pair2, chat_fn=_chat_returning(tag2), embed_fn=_embed_distinct
    )
    assert plan2.prereq_pairs == [("proc.solve_p2", "eq.third")]
    assert plan2.dropped_prereq_pairs == []

    a = plan1.minted_entity_ids["proc.solve_p2"]
    c = plan2.minted_entity_ids["eq.third"]
    rows = (
        (
            await db_session.execute(
                select(EntityPrereq)
                .where(EntityPrereq.from_entity_id == a)
                .where(EntityPrereq.to_entity_id == c)
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1


def test_reaches_handles_cycle_without_infinite_loop():
    """``_reaches`` (the writer-boundary guard's DFS) must terminate and skip an
    ALREADY-VISITED node rather than re-descending it — covers the ``continue``
    branch a simple acyclic adjacency never exercises."""
    from apollo.provisioning.tag_mint_persist import _reaches

    adj = {1: {2}, 2: {1}}
    assert _reaches(adj, 1, 99) is False
    assert _reaches(adj, 1, 2) is True


async def test_insert_prereqs_rejects_persisted_cycle_at_writer_boundary(db_session):
    """Defense-in-depth (M2): ``insert_prereqs`` itself must re-derive the
    persisted concept subgraph and refuse an edge that would close a cycle
    against it, even when called directly (bypassing ``tag_and_mint``'s
    pre-filter) — mirroring the existing cross-concept writer-boundary guard.
    DISCRIMINATING: removing this guard REDs (the reverse edge persists)."""
    from apollo.provisioning.tag_mint_persist import insert_prereqs

    _ss_id, subj_id = await _seed_course(db_session, slug="c-writercycle")
    concept = Concept(course_id=subj_id.search_space_id, subject_slug=subj_id.slug, subject_display_name=subj_id.display_name, slug="writer-cycle", display_name="WC")
    db_session.add(concept)
    await db_session.flush()
    ea = LearnerEntity(
        course_id=_ss_id,
        concept_id=concept.id,
        canonical_key="eq.a",
        kind="equation",
        display_name="a",
        payload={},
        aliases=[],
    )
    eb = LearnerEntity(
        course_id=_ss_id,
        concept_id=concept.id,
        canonical_key="eq.b",
        kind="equation",
        display_name="b",
        payload={},
        aliases=[],
    )
    db_session.add_all([ea, eb])
    await db_session.flush()
    key_to_id = {"eq.a": int(ea.id), "eq.b": int(eb.id)}

    # Persist A->B directly (as if minted by an earlier call).
    inserted, _skipped, dropped = await insert_prereqs(
        db_session,
        course_id=_ss_id,
        concept_id=int(concept.id),
        key_to_id=key_to_id,
        pairs=[("eq.a", "eq.b")],
    )
    assert inserted == 1
    assert dropped == []

    # A second, direct call drafting the REVERSE must be refused.
    inserted2, _skipped2, dropped2 = await insert_prereqs(
        db_session,
        course_id=_ss_id,
        concept_id=int(concept.id),
        key_to_id=key_to_id,
        pairs=[("eq.b", "eq.a")],
    )
    assert inserted2 == 0
    assert ("eq.b", "eq.a") in dropped2

    rows = (
        await db_session.execute(select(EntityPrereq.from_entity_id, EntityPrereq.to_entity_id))
    ).all()
    directed = {(r[0], r[1]) for r in rows}
    assert directed == {(int(ea.id), int(eb.id))}


async def test_tag_and_mint_drops_self_loop_prereq(db_session):
    """A drafted self-loop (A->A) is never inserted."""
    ss_id, _subj = await _seed_course(db_session, slug="c-selfloop")
    pair = _approved_pair(search_space_id=ss_id)
    tag = _tag_payload(prereqs=[["eq.bernoulli", "eq.bernoulli"]])
    plan = await tag_and_mint(
        db_session, pair, chat_fn=_chat_returning(tag), embed_fn=_embed_distinct
    )
    a = plan.minted_entity_ids["eq.bernoulli"]
    rows = (
        (
            await db_session.execute(
                select(EntityPrereq)
                .where(EntityPrereq.from_entity_id == a)
                .where(EntityPrereq.to_entity_id == a)
            )
        )
        .scalars()
        .all()
    )
    assert rows == []
    assert plan.prereq_pairs == []


async def test_tag_and_mint_bare_id_opposes_does_not_raise(db_session):
    """A misconception whose opposes names a reference node by BARE id
    (bernoulli, not eq.bernoulli) must not raise TagMintError. Historically
    (H1) this exercised link_opposes's bare/prefixed key resolution against a
    persisted misconception row; DB-13 made misconception persistence a
    permanent no-op (no LearnerEntity row is ever minted for it), so this is
    now a smoke test that the bare-id draft mints cleanly regardless."""
    ss_id, _subj = await _seed_course(db_session, slug="c-bareopp")
    misc = [
        {
            "key": "misc.pressure_follows_speed",
            "display_name": "Pressure follows speed",
            "description": "thinks higher speed means higher pressure",
            "opposes": "bernoulli",  # BARE id (not eq.bernoulli)
            "trigger_phrases": ["pressure goes up with speed"],
        }
    ]
    pair = _approved_pair(search_space_id=ss_id, misconceptions=misc)
    plan = await tag_and_mint(
        db_session, pair, chat_fn=_chat_returning(_tag_payload()), embed_fn=_embed_distinct
    )
    assert "misc.pressure_follows_speed" in plan.misconception_keys
    rows = (
        (
            await db_session.execute(
                select(LearnerEntity)
                .where(LearnerEntity.concept_id == plan.concept_id)
                .where(LearnerEntity.kind == "misconception")
            )
        )
        .scalars()
        .all()
    )
    assert rows == []  # no LearnerEntity row ever minted for the misconception


async def test_tag_and_mint_idempotent(db_session):
    """Running the same ApprovedPair twice inserts no new entities/prereqs and
    unions no new symbols. DISCRIMINATING: a dropped (concept_id, canonical_key)
    upsert guard would duplicate entities."""
    ss_id, _subj = await _seed_course(db_session, slug="c-idem")
    pair = _approved_pair(search_space_id=ss_id)
    tag = _tag_payload(prereqs=[["proc.solve_p2", "eq.bernoulli"]])
    chat = _chat_returning(tag)
    plan1 = await tag_and_mint(db_session, pair, chat_fn=chat, embed_fn=_embed_distinct)

    def _count(model, **filt):
        stmt = select(model)
        for k, v in filt.items():
            stmt = stmt.where(getattr(model, k) == v)
        return stmt

    rows1 = (
        (await db_session.execute(_count(LearnerEntity, concept_id=plan1.concept_id))).scalars().all()
    )
    edges1 = (await db_session.execute(select(EntityPrereq))).scalars().all()

    plan2 = await tag_and_mint(db_session, pair, chat_fn=chat, embed_fn=_embed_distinct)
    rows2 = (
        (await db_session.execute(_count(LearnerEntity, concept_id=plan2.concept_id))).scalars().all()
    )
    edges2 = (await db_session.execute(select(EntityPrereq))).scalars().all()

    assert plan2.concept_id == plan1.concept_id
    assert len(rows2) == len(rows1)  # no new entities
    assert len(edges2) == len(edges1)  # no new prereqs
    assert plan2.authored_symbols == []  # nothing new to author (all unioned)


async def test_tag_and_mint_unresolvable_misconception_opposes_is_inert(db_session, caplog):
    """A misconception opposing a key that resolves to no entity mints cleanly
    and does not raise. Historically (2026-07-14 policy change, staging set 12)
    this exercised drop_unlinkable_minted_misconceptions deleting THIS mint's
    rogue row and logging a drop event; DB-13 made misconception persistence a
    permanent no-op (nothing is ever minted for a misconception, so there is
    nothing to drop) — the drop event never fires and the candidate still
    mints, which is what this now proves."""
    import logging

    ss_id, _subj = await _seed_course(db_session, slug="c-fail")
    misc = [
        {
            "key": "misc.bad",
            "display_name": "Bad",
            "description": "x",
            "opposes": "eq.does_not_exist",  # no entity will carry this key
            "trigger_phrases": [],
        }
    ]
    pair = _approved_pair(search_space_id=ss_id, misconceptions=misc)
    caplog.set_level(logging.WARNING, logger="apollo.provisioning.tag_mint")
    plan = await tag_and_mint(
        db_session,
        pair,
        chat_fn=_chat_returning(_tag_payload()),
        embed_fn=_embed_distinct,
    )

    assert "misc.bad" not in plan.minted_entity_ids
    assert "misc.bad" in plan.misconception_keys
    rogue = (
        (
            await db_session.execute(
                select(LearnerEntity)
                .where(LearnerEntity.concept_id == plan.concept_id)
                .where(LearnerEntity.canonical_key == "misc.bad")
            )
        )
        .scalars()
        .all()
    )
    assert rogue == []
    events = [
        r
        for r in caplog.records
        if getattr(r, "event", None) == "tag_mint_dropped_unlinkable_misconceptions"
    ]
    assert events == []  # nothing was minted, so nothing was dropped


async def test_preexisting_unlinkable_misconception_stays_fail_closed(db_session):
    """A PRE-EXISTING misconception row that is UNLINKED (no opposes_entity_id)
    and whose opposes key does not resolve keeps the fail-closed contract: only
    THIS mint's rows are droppable, and only ALREADY-LINKED prior rows are
    skippable (see the linked-row test below)."""
    ss_id, _subj = await _seed_course(db_session, slug="c-fail-prior")
    pair = _approved_pair(search_space_id=ss_id)
    plan = await tag_and_mint(
        db_session,
        pair,
        chat_fn=_chat_returning(_tag_payload()),
        embed_fn=_embed_distinct,
    )
    db_session.add(
        LearnerEntity(
            course_id=ss_id,
            concept_id=plan.concept_id,
            canonical_key="misc.prior_rogue",
            kind="misconception",
            display_name="Prior rogue",
            payload={"opposes_entity_key": "eq.never_minted"},
            aliases=[],
        )
    )
    await db_session.flush()

    with pytest.raises(TagMintError):
        await tag_and_mint(
            db_session,
            pair,
            chat_fn=_chat_returning(_tag_payload()),
            embed_fn=_embed_distinct,
        )


async def test_preexisting_linked_misconception_is_skipped_not_fatal(db_session):
    """The staging set-13 regression: a PRE-EXISTING misconception that an
    earlier mint (or emergent materialize) ALREADY LINKED must not fail later
    mints into the same concept — its key can never resolve from a later mint's
    key_to_id and the link is durable. link_opposes skips it untouched."""
    ss_id, _subj = await _seed_course(db_session, slug="c-linked-prior")
    pair = _approved_pair(search_space_id=ss_id)
    plan = await tag_and_mint(
        db_session,
        pair,
        chat_fn=_chat_returning(_tag_payload()),
        embed_fn=_embed_distinct,
    )
    db_session.add(
        LearnerEntity(
            course_id=ss_id,
            concept_id=plan.concept_id,
            canonical_key="emergent.proc.prior_linked",
            kind="misconception",
            display_name="Prior linked",
            payload={
                "opposes_entity_key": "proc.from_an_earlier_mint",
                "opposes_entity_id": 999,
            },
            aliases=[],
        )
    )
    await db_session.flush()

    plan2 = await tag_and_mint(
        db_session,
        pair,
        chat_fn=_chat_returning(_tag_payload()),
        embed_fn=_embed_distinct,
    )

    assert plan2.concept_id == plan.concept_id
    row = (
        (
            await db_session.execute(
                select(LearnerEntity)
                .where(LearnerEntity.concept_id == plan.concept_id)
                .where(LearnerEntity.canonical_key == "emergent.proc.prior_linked")
            )
        )
        .scalars()
        .one()
    )
    assert dict(row.payload)["opposes_entity_id"] == 999  # untouched


async def test_tag_and_mint_malformed_tag_raises(db_session):
    """A concept-tag LLM response missing concept_slug → TagMintError (fail-closed,
    NO silent mislink)."""
    ss_id, _subj = await _seed_course(db_session, slug="c-badtag")
    pair = _approved_pair(search_space_id=ss_id)
    with pytest.raises(TagMintError):
        await tag_and_mint(
            db_session,
            pair,
            chat_fn=_chat_returning({"display_name": "no slug here"}),
            embed_fn=_embed_distinct,
        )


async def test_tag_and_mint_bad_prereq_entry_raises(db_session):
    """A malformed prereq draft entry → TagMintError (fail-closed)."""
    ss_id, _subj = await _seed_course(db_session, slug="c-badprq")
    pair = _approved_pair(search_space_id=ss_id)
    tag = _tag_payload(prereqs=[["only-one-element"]])
    with pytest.raises(TagMintError):
        await tag_and_mint(db_session, pair, chat_fn=_chat_returning(tag), embed_fn=_embed_distinct)


async def test_tag_and_mint_non_json_tag_raises(db_session):
    """A non-JSON concept-tag response → TagMintError (the json.loads except)."""
    ss_id, _subj = await _seed_course(db_session, slug="c-nonjson")
    pair = _approved_pair(search_space_id=ss_id)

    def _chat(*_a, **_k) -> str:
        return "definitely not json {"

    with pytest.raises(TagMintError):
        await tag_and_mint(db_session, pair, chat_fn=_chat, embed_fn=_embed_distinct)


async def test_tag_and_mint_non_object_tag_raises(db_session):
    """A JSON array (not an object) concept-tag response → TagMintError."""
    ss_id, _subj = await _seed_course(db_session, slug="c-nonobj")
    pair = _approved_pair(search_space_id=ss_id)

    def _chat(*_a, **_k) -> str:
        return json.dumps(["not", "an", "object"])

    with pytest.raises(TagMintError):
        await tag_and_mint(db_session, pair, chat_fn=_chat, embed_fn=_embed_distinct)


async def test_tag_and_mint_prereq_dict_form(db_session):
    """A prereq draft entry in ``{"from","to"}`` dict form is accepted (covers the
    dict branch)."""
    ss_id, _subj = await _seed_course(db_session, slug="c-prqdict")
    pair = _approved_pair(search_space_id=ss_id)
    tag = _tag_payload(prereqs=[{"from": "proc.solve_p2", "to": "eq.bernoulli"}])
    plan = await tag_and_mint(
        db_session, pair, chat_fn=_chat_returning(tag), embed_fn=_embed_distinct
    )
    assert plan.prereq_pairs == [("proc.solve_p2", "eq.bernoulli")]


async def test_tag_and_mint_prereq_unminted_key_dropped(db_session):
    """A prereq draft edge naming a key no entity carries is DROPPED, not fatal
    (intent decision 2026-06-30): prereqs are optional KG enrichment and the LLM
    routinely names a problem given. The mint succeeds, the edge is not inserted,
    and ``prereq_pairs`` reflects only the kept (resolvable) edges."""
    ss_id, _subj = await _seed_course(db_session, slug="c-prqkey")
    pair = _approved_pair(search_space_id=ss_id)
    tag = _tag_payload(prereqs=[["eq.bernoulli", "eq.nonexistent"]])
    plan = await tag_and_mint(
        db_session, pair, chat_fn=_chat_returning(tag), embed_fn=_embed_distinct
    )
    assert plan.prereq_pairs == []
    from_id = plan.minted_entity_ids["eq.bernoulli"]
    edges = (
        (
            await db_session.execute(
                select(EntityPrereq).where(EntityPrereq.from_entity_id == from_id)
            )
        )
        .scalars()
        .all()
    )
    assert edges == []


async def test_tag_and_mint_prereq_drops_only_the_unresolvable_edge(db_session):
    """A mixed draft keeps the resolvable edge and drops only the bad one."""
    ss_id, _subj = await _seed_course(db_session, slug="c-prqmix")
    pair = _approved_pair(search_space_id=ss_id)
    tag = _tag_payload(prereqs=[["solve_p2", "bernoulli"], ["eq.bernoulli", "eq.nonexistent"]])
    plan = await tag_and_mint(
        db_session, pair, chat_fn=_chat_returning(tag), embed_fn=_embed_distinct
    )
    assert plan.prereq_pairs == [("solve_p2", "bernoulli")]
    from_id = plan.minted_entity_ids["proc.solve_p2"]
    to_id = plan.minted_entity_ids["eq.bernoulli"]
    edges = (
        (
            await db_session.execute(
                select(EntityPrereq)
                .where(EntityPrereq.from_entity_id == from_id)
                .where(EntityPrereq.to_entity_id == to_id)
            )
        )
        .scalars()
        .all()
    )
    assert len(edges) == 1


async def test_insert_prereqs_drops_cross_concept_endpoint(db_session):
    """Defense-in-depth (audit bug #4): ``insert_prereqs`` must DROP any edge whose
    endpoint entity belongs to a FOREIGN concept, even when its key resolves — a
    dedup ``merged``-to-foreign id is still 'resolvable', so the unminted-key drop
    never catches it. With PR2's concept-scoped dedup this should never fire, but
    the writer enforces concept scope exactly as the reader does
    (personalization_read.py:148-150). DISCRIMINATING: removing the concept check
    REDs (the foreign edge persists)."""
    from apollo.provisioning.tag_mint_persist import insert_prereqs

    _ss_id, subj_id = await _seed_course(db_session, slug="c-xconcept")
    ca = Concept(course_id=subj_id.search_space_id, subject_slug=subj_id.slug, subject_display_name=subj_id.display_name, slug="concept-a", display_name="A")
    cb = Concept(course_id=subj_id.search_space_id, subject_slug=subj_id.slug, subject_display_name=subj_id.display_name, slug="concept-b", display_name="B")
    db_session.add_all([ca, cb])
    await db_session.flush()
    ea = LearnerEntity(
        course_id=_ss_id,
        concept_id=ca.id,
        canonical_key="eq.local",
        kind="equation",
        display_name="local",
        payload={},
        aliases=[],
    )
    eb = LearnerEntity(
        course_id=_ss_id,
        concept_id=cb.id,
        canonical_key="eq.foreign",
        kind="equation",
        display_name="foreign",
        payload={},
        aliases=[],
    )
    db_session.add_all([ea, eb])
    await db_session.flush()
    # key_to_id resolves the 'to' endpoint to a FOREIGN-concept entity id.
    key_to_id = {"eq.local": int(ea.id), "eq.foreign": int(eb.id)}
    inserted, _skipped, dropped = await insert_prereqs(
        db_session,
        course_id=_ss_id,
        concept_id=int(ca.id),
        key_to_id=key_to_id,
        pairs=[("eq.local", "eq.foreign")],
    )
    assert inserted == 0
    assert ("eq.local", "eq.foreign") in dropped
    rows = (await db_session.execute(select(EntityPrereq))).scalars().all()
    assert rows == []  # nothing crossed the concept boundary into the table


async def test_insert_prereqs_keeps_same_concept_edge(db_session):
    """An edge whose BOTH endpoints belong to ``concept_id`` is inserted normally
    (the concept-scope guard only drops cross-concept endpoints)."""
    from apollo.provisioning.tag_mint_persist import insert_prereqs

    _ss_id, subj_id = await _seed_course(db_session, slug="c-sameconcept")
    ca = Concept(course_id=subj_id.search_space_id, subject_slug=subj_id.slug, subject_display_name=subj_id.display_name, slug="concept-a2", display_name="A")
    db_session.add(ca)
    await db_session.flush()
    e1 = LearnerEntity(
        course_id=_ss_id,
        concept_id=ca.id,
        canonical_key="eq.one",
        kind="equation",
        display_name="one",
        payload={},
        aliases=[],
    )
    e2 = LearnerEntity(
        course_id=_ss_id,
        concept_id=ca.id,
        canonical_key="proc.two",
        kind="procedure",
        display_name="two",
        payload={},
        aliases=[],
    )
    db_session.add_all([e1, e2])
    await db_session.flush()
    key_to_id = {"eq.one": int(e1.id), "proc.two": int(e2.id)}
    inserted, _skipped, dropped = await insert_prereqs(
        db_session,
        course_id=_ss_id,
        concept_id=int(ca.id),
        key_to_id=key_to_id,
        pairs=[("proc.two", "eq.one")],
    )
    assert inserted == 1
    assert dropped == []


async def test_mint_plan_surfaces_dropped_prereq_pairs(db_session):
    """Audit bug D: unresolvable-key AND cyclic prereq drops are SURFACED on
    ``MintPlan.dropped_prereq_pairs`` (previously only INFO-logged). One field
    carries both drop reasons in one place (PR3's cyclic drops folded in now that
    #73 is merged). DISCRIMINATING: without the field/population this REDs."""
    ss_id, _subj = await _seed_course(db_session, slug="c-dropsurface")
    pair = _approved_pair(search_space_id=ss_id)
    tag = _tag_payload(
        prereqs=[
            ["eq.bernoulli", "eq.nonexistent"],  # unresolvable key -> dropped
            ["eq.bernoulli", "proc.solve_p2"],  # kept
            ["proc.solve_p2", "eq.bernoulli"],  # cyclic reverse -> dropped
        ]
    )
    plan = await tag_and_mint(
        db_session, pair, chat_fn=_chat_returning(tag), embed_fn=_embed_distinct
    )
    assert plan.prereq_pairs == [("eq.bernoulli", "proc.solve_p2")]
    assert ("eq.bernoulli", "eq.nonexistent") in plan.dropped_prereq_pairs
    assert ("proc.solve_p2", "eq.bernoulli") in plan.dropped_prereq_pairs


async def test_tag_and_mint_drops_cross_concept_edge_before_acyclicity(db_session, monkeypatch):
    """Ordering regression (reviewer, PR2b): the cross-concept endpoint drop must
    run BEFORE the acyclicity guard, so a foreign-concept endpoint (a dedup MERGE
    onto another concept — the exact bug PR2 removes at the source) cannot act as a
    PHANTOM BRIDGE node that fakes a cycle and discards a legitimate within-concept
    edge. Reproduces the reviewer's scenario: drafted edges ``a->x``, ``x->b``,
    ``b->a`` where ``x`` merged onto a FOREIGN entity. The only real (within-concept)
    edge is ``b->a``; ``a->x`` and ``x->b`` are cross-concept. If acyclicity runs
    first it sees the phantom path ``a->x->b`` and drops ``b->a`` as cyclic — losing
    a valid edge. DISCRIMINATING: with the buggy order ``("b","a")`` is absent from
    ``prereq_pairs`` and never inserted."""
    from apollo.provisioning import tag_mint as tm
    from apollo.provisioning.dedup import DedupVerdict

    ss_id, subj_id = await _seed_course(db_session, slug="c-phantom")
    # A FOREIGN concept in the same course, holding the merge-target entity F.
    foreign = Concept(course_id=subj_id.search_space_id, subject_slug=subj_id.slug, subject_display_name=subj_id.display_name, slug="concept-foreign", display_name="Foreign")
    db_session.add(foreign)
    await db_session.flush()
    f_entity = LearnerEntity(
        course_id=ss_id,
        concept_id=foreign.id,
        canonical_key="eq.x",
        kind="equation",
        display_name="foreign x",
        payload={},
        aliases=[],
    )
    db_session.add(f_entity)
    await db_session.flush()

    # Force the candidate keyed ``eq.x`` to MERGE onto the foreign entity; mint the
    # rest (eq.a, eq.b) distinct within the tagged concept.
    async def _fake_resolve(db, *, candidate, **_kw):
        if candidate.canonical_key == "eq.x":
            return DedupVerdict(
                verdict="merged",
                method="embedding",
                similarity=0.95,
                matched_entity_id=int(f_entity.id),
            )
        return DedupVerdict(
            verdict="distinct", method="slug", similarity=None, matched_entity_id=None
        )

    monkeypatch.setattr(tm, "resolve_candidate", _fake_resolve)

    problem = {
        "id": "scrape.phantom",
        "concept_id": "concept-main",
        "difficulty": "intro",
        "problem_text": "phantom-bridge repro",
        "given_values": {"A": 1.0},
        "target_unknown": "B",
        # Three GENUINELY-DISTINCT equations (not sign-equivalent) so the G4.3
        # content-collapse leaves all three candidates for the phantom-bridge
        # resolution under test; the scenario is about cross-concept edge ordering,
        # not equation equivalence.
        "reference_solution": [
            {
                "step": 1,
                "entry_type": "equation",
                "id": "a",
                "content": {"label": "a", "symbolic": "A - B"},
                "depends_on": [],
            },
            {
                "step": 2,
                "entry_type": "equation",
                "id": "b",
                "content": {"label": "b", "symbolic": "B - 2*A"},
                "depends_on": [],
            },
            {
                "step": 3,
                "entry_type": "equation",
                "id": "x",
                "content": {"label": "x", "symbolic": "A + B"},
                "depends_on": [],
            },
        ],
    }
    pair = _approved_pair(problem=problem, search_space_id=ss_id)
    tag = _tag_payload(
        concept_slug="concept-main",
        prereqs=[["a", "x"], ["x", "b"], ["b", "a"]],
    )
    plan = await tag_and_mint(
        db_session, pair, chat_fn=_chat_returning(tag), embed_fn=_embed_distinct
    )
    # The within-concept edge survives (cross-concept bridge edges removed FIRST).
    assert ("b", "a") in plan.prereq_pairs
    # The two cross-concept edges are surfaced as drops, not persisted.
    assert ("a", "x") in plan.dropped_prereq_pairs
    assert ("x", "b") in plan.dropped_prereq_pairs
    b_id = plan.minted_entity_ids["eq.b"]
    a_id = plan.minted_entity_ids["eq.a"]
    rows = (
        await db_session.execute(select(EntityPrereq.from_entity_id, EntityPrereq.to_entity_id))
    ).all()
    directed = {(r[0], r[1]) for r in rows}
    assert (b_id, a_id) in directed  # the legit edge was inserted
    assert not any(r[0] == int(f_entity.id) or r[1] == int(f_entity.id) for r in rows)


async def test_tag_and_mint_escalates_to_judge(db_session):
    """A candidate whose scope_summary lands in the 0.82–0.92 band against a pre-
    seeded entity escalates to tag/mint's injected ``_judge_distinct`` tier (which
    returns 'distinct', so the candidate mints fresh). Covers the band judge
    callable. The pre-seeded entity uses the 'BANDSEED' scope_summary that
    ``_embed_in_band`` places at cosine 0.87."""
    ss_id, subj_id = await _seed_course(db_session, slug="c-band")
    concept = Concept(course_id=subj_id.search_space_id, subject_slug=subj_id.slug, subject_display_name=subj_id.display_name, slug="bernoulli_principle", display_name="Bernoulli")
    db_session.add(concept)
    await db_session.flush()
    seed_entity = LearnerEntity(
        course_id=ss_id,
        concept_id=concept.id,
        canonical_key="eq.preexisting",  # different slug → no slug-tier merge
        kind="equation",
        display_name="pre",
        payload={},
        aliases=[],
        scope_summary="BANDSEED",
    )
    db_session.add(seed_entity)
    await db_session.flush()

    pair = _approved_pair(search_space_id=ss_id)
    plan = await tag_and_mint(
        db_session,
        pair,
        chat_fn=_chat_returning(_tag_payload()),
        embed_fn=_embed_in_band,
    )
    # judge said distinct → eq.bernoulli minted fresh, not merged onto the seed.
    assert "eq.bernoulli" in plan.minted_entity_ids


async def test_tag_and_mint_re_update_existing_entity(db_session):
    """When an entity already exists under the concept with the SAME canonical_key
    but the dedup ladder does NOT slug-merge it (because it carries no
    scope_summary so the slug tier still matches — wait: slug DOES match). To force
    the upsert RE-UPDATE branch we pre-seed an entity whose canonical_key matches a
    minted key under a DIFFERENT concept so the in-course slug merge does not fire,
    then mint twice into the SAME concept: the second pass re-updates in place."""
    ss_id, subj_id = await _seed_course(db_session, slug="c-reupd")
    # Mint once. The reference entities are created.
    pair = _approved_pair(search_space_id=ss_id)
    plan1 = await tag_and_mint(
        db_session,
        pair,
        chat_fn=_chat_returning(_tag_payload()),
        embed_fn=_embed_distinct,
    )
    # Manually clear the scope_summary of a minted entity AND its canonical_key is
    # unchanged; re-running mints again. On the second pass resolve_candidate
    # slug-merges (so it never reaches upsert) — to hit the in-place UPDATE branch
    # we call upsert_entity directly with a changed display_name.
    from apollo.persistence.learner_model_seed import EntitySpec
    from apollo.provisioning.tag_mint_persist import upsert_entity

    spec = EntitySpec(
        canonical_key="eq.bernoulli",
        kind="equation",
        display_name="Bernoulli (renamed)",
        payload={"entry_type": "equation"},
        aliases=(),
    )
    entity_id, inserted = await upsert_entity(
        db_session,
        course_id=ss_id,
        concept_id=plan1.concept_id,
        spec=spec,
        scope_summary="updated summary",
    )
    assert inserted is False  # the RE-UPDATE branch
    assert entity_id == plan1.minted_entity_ids["eq.bernoulli"]
    row = (await db_session.execute(select(LearnerEntity).where(LearnerEntity.id == entity_id))).scalar_one()
    assert row.display_name == "Bernoulli (renamed)"
    assert row.scope_summary == "updated summary"


async def test_resolve_or_create_concept_creates_subject_when_absent(db_session):
    """``resolve_or_create_concept`` on a course with NO Subject creates a
    provisional subject first (covers the no-subject branch)."""
    from apollo.provisioning.tag_mint_persist import resolve_or_create_concept

    space = Course(name="No-subj tag", slug="c-tag-nosubj", subject_name="X")
    db_session.add(space)
    await db_session.flush()
    cid = await resolve_or_create_concept(
        db_session,
        search_space_id=space.id,
        slug="newconcept",
        display_name="New Concept",
    )
    assert isinstance(cid, int)
    concept = (await db_session.execute(select(Concept).where(Concept.id == cid))).scalar_one()
    assert concept.slug == "newconcept"


async def test_link_opposes_skips_empty_opposes(db_session):
    """A misconception with NO opposes key is SKIPPED by link_opposes (the
    ``if not opposes_key: continue`` branch). Asserted via a misconception entity
    whose payload lacks opposes_entity_key minted through tag_and_mint with a
    misconception that has an EMPTY opposes value."""
    from apollo.persistence.models import LearnerEntity as _KGE
    from apollo.provisioning.tag_mint_persist import link_opposes

    ss_id, subj_id = await _seed_course(db_session, slug="c-noopp")
    concept = Concept(course_id=subj_id.search_space_id, subject_slug=subj_id.slug, subject_display_name=subj_id.display_name, slug="bernoulli_principle", display_name="Bernoulli")
    db_session.add(concept)
    await db_session.flush()
    # A misconception entity carrying NO opposes_entity_key in its payload.
    misc = _KGE(
        course_id=ss_id,
        concept_id=concept.id,
        canonical_key="misc.noopposes",
        kind="misconception",
        display_name="No opposes",
        payload={"description": "x"},  # no opposes_entity_key
        aliases=[],
    )
    db_session.add(misc)
    await db_session.flush()
    linked = await link_opposes(db_session, concept_id=concept.id, key_to_id={})
    assert linked == 0  # the empty-opposes misconception is skipped


def test_equation_symbols_skips_no_symbolic_and_malformed():
    """``_equation_symbols`` skips an equation step with no ``symbolic`` content
    AND drops a malformed expression (sympify raises → no symbols)."""
    from apollo.provisioning.tag_mint import _equation_symbols

    problem = {
        "reference_solution": [
            {"entry_type": "equation", "id": "no_sym", "content": {"label": "x"}},
            {
                "entry_type": "equation",
                "id": "bad",
                "content": {"symbolic": "P1 + + )("},  # malformed
            },
            {
                "entry_type": "equation",
                "id": "good",
                "content": {"symbolic": "P1 - P2"},
            },
        ]
    }
    symbols = _equation_symbols(problem)
    assert symbols == {"P1", "P2"}  # only the good one contributes


def test_scope_summary_includes_symbol_for_variable_entity():
    """``_scope_summary_for`` appends the symbol for a variable entity carrying a
    ``payload['symbol']`` (covers the symbol branch)."""
    from apollo.persistence.learner_model_seed import EntitySpec
    from apollo.provisioning.tag_mint import _scope_summary_for

    spec = EntitySpec(
        canonical_key="var.P",
        kind="variable",
        display_name="Pressure",
        payload={"symbol": "P"},
        aliases=(),
    )
    summary = _scope_summary_for(spec)
    assert "symbol P" in summary
    assert "Pressure" in summary


# --------------------------------------------------------------------------- #
# Real-PG — the frozen-map extension's 3B2b gate-1 consumer
# --------------------------------------------------------------------------- #


def test_variable_mapping_passes_gate1_mintmap_subcheck():
    """Build a Problem with a variable_mapping step; run run_promotion_lint and
    assert gate-1's mint-map membership sub-check PASSES (pre-Step-1 it failed
    CLOSED). Ties the frozen-map extension to its 3B2b consumer. The lint is
    PURE — no DB — so this is a sync test."""
    graph = {
        "id": "p-varmap",
        "concept_id": "bernoulli_principle",
        "difficulty": "intro",
        "problem_text": "Map P to pressure then solve.",
        "given_values": {"P1": 100000.0, "v1": 2.0, "rho": 1000.0},
        "target_unknown": "P2",
        "reference_solution": [
            {
                "step": 1,
                "entry_type": "variable_mapping",
                "id": "map_p",
                "content": {"term": "static pressure", "symbol": "P"},
                "depends_on": [],
                "entity_key": "varmap.map_p",
            },
            {
                "step": 2,
                "entry_type": "equation",
                "id": "bernoulli",
                "content": {
                    "label": "Bernoulli",
                    "symbolic": "P1 + 0.5*rho*v1**2 - P2 - 0.5*rho*v2**2",
                },
                "depends_on": [],
                "entity_key": "eq.bernoulli",
            },
            {
                "step": 3,
                "entry_type": "procedure_step",
                "id": "solve",
                "content": {
                    "action": "solve for P2",
                    "purpose": "find pressure",
                    "order": 1,
                    "uses_equations": ["bernoulli"],
                },
                "depends_on": ["bernoulli"],
                "entity_key": "proc.solve",
            },
        ],
        "declared_paths": [["map_p", "bernoulli", "solve"]],
    }
    # Author symbols that cover the equation so gate 4 also passes; the key point
    # is that gate 1's mint-map sub-check no longer rejects variable_mapping.
    result = run_promotion_lint(
        graph,
        canonical_symbols={"P", "v", "rho"},
        normalization_map={"P1": "P", "P2": "P", "v1": "v", "v2": "v"},
        existing_problem_hashes=set(),
    )
    # gate 1 must NOT be the failing gate (variable_mapping is now in the map).
    assert result.failed_gate != 1, result.diagnostic


# --------------------------------------------------------------------------- #
# Public-API re-export surface (the package-level import paths apollo.md advertises)
# --------------------------------------------------------------------------- #


def test_tag_mint_public_api_reexport():
    """``from apollo.provisioning import tag_and_mint, ApprovedPair, MintPlan,
    TagMintError`` returns the SAME objects as the ``tag_mint`` module — the
    package-level paths apollo.md documents must resolve. DISCRIMINATING: drop a
    re-export from ``apollo/provisioning/__init__.py`` and this REDs."""
    from apollo.provisioning import (
        ApprovedPair as ReexportApprovedPair,
    )
    from apollo.provisioning import (
        MintPlan as ReexportMintPlan,
    )
    from apollo.provisioning import (
        TagMintError as ReexportTagMintError,
    )
    from apollo.provisioning import (
        tag_and_mint as reexport_tag_and_mint,
    )
    from apollo.provisioning import tag_mint as tag_mint_mod

    assert ReexportApprovedPair is tag_mint_mod.ApprovedPair
    assert ReexportMintPlan is tag_mint_mod.MintPlan
    assert ReexportTagMintError is tag_mint_mod.TagMintError
    assert reexport_tag_and_mint is tag_mint_mod.tag_and_mint


# --------------------------------------------------------------------------- #
# G4.3 (lane B2.3) — mint-time entity dedup + equation payloads.
#
# The WU-AAS mint over the campaign's 2-problem linear_motion PDF produced 37
# entities: 2-4 near-identical candidates PER ROLE (e.g. eq.eq_motion /
# eq.eq_velocity_formula / eq1 all == ``v = v0 + a*t``) with NO ``equation``
# payload to disambiguate them downstream (Findings D + E,
# campaign/out/f1/provisioning-notes.md). The dedup ladder failed to merge them
# because the candidates carry DIFFERENT synthetic keys (slug tier misses) and a
# thin ``display_name | kind`` scope_summary (embedding tier misses).
#
# The fix collapses candidates minted from ONE authored set by a DETERMINISTIC,
# CASE-SENSITIVE, concept-scoped equivalence key (kind + normalized equation /
# raw symbol / display-name), merges their provenance, and attaches the source
# equation to each candidate. Being deterministic + case-sensitive, it CANNOT
# reintroduce the 2026-06-30 ``m≡M`` embedding false-merge class (case-distinct
# symbols and distinct roles keep distinct signatures).
# --------------------------------------------------------------------------- #


def _eq_spec(key: str, symbolic: str, display: str):
    from apollo.persistence.learner_model_seed import EntitySpec

    return EntitySpec(
        canonical_key=key,
        kind="equation",
        display_name=display,
        payload={"entry_type": "equation", "symbolic": symbolic},
        aliases=(),
    )


def _var_spec(key: str, symbol: str, display: str):
    from apollo.persistence.learner_model_seed import EntitySpec

    return EntitySpec(
        canonical_key=key,
        kind="variable",
        display_name=display,
        payload={"symbol": symbol},
        aliases=(),
    )


def test_normalize_equation_sign_and_order_invariant():
    """The same equation written ``lhs = rhs`` or ``rhs = lhs`` (or as an already-
    moved expression) normalizes to ONE canonical string, so two extractions of
    the SAME physics collapse. Distinct equations do NOT."""
    from apollo.provisioning.tag_mint import _normalize_equation

    a = _normalize_equation("v = v0 + a*t")
    b = _normalize_equation("v0 + a*t = v")  # sides swapped
    c = _normalize_equation("v - v0 - a*t")  # already moved to one side
    assert a == b == c
    # a genuinely different equation is NOT collapsed onto it.
    assert _normalize_equation("x = v0*t + (1/2)*a*t**2") != a


def test_normalize_equation_case_sensitive_m_vs_big_m():
    """The audit's canonical hazard: ``m`` (hanging mass) must NOT normalize to
    the same string as ``M`` (block mass). sympy symbols are case-sensitive, so
    the normalized equations differ — no ``m≡M`` collapse."""
    from apollo.provisioning.tag_mint import _normalize_equation

    assert _normalize_equation("F = m*a") != _normalize_equation("F = M*a")


def test_normalize_equation_malformed_falls_back_to_raw():
    """A malformed / chained-equality expression sympify cannot parse falls back
    to a whitespace-collapsed raw string (deterministic; case-preserved)."""
    from apollo.provisioning.tag_mint import _normalize_equation

    # chained equality (two '=') is exactly the campaign's Finding-A notation.
    out = _normalize_equation("v = 0 + (2.0)(5.0) = 10.0")
    assert out == "v=0+(2.0)(5.0)=10.0"


def test_equivalence_signature_collapses_same_equation():
    """Three equation candidates encoding the SAME equation under different keys /
    labels share ONE equivalence signature."""
    from apollo.provisioning.tag_mint import _equivalence_signature

    s1 = _equivalence_signature(_eq_spec("eq.eq_motion", "v = v0 + a*t", "Velocity formula"))
    s2 = _equivalence_signature(
        _eq_spec("eq.eq_velocity_formula", "v0 + a*t = v", "Velocity equation")
    )
    s3 = _equivalence_signature(_eq_spec("eq.eq1", "v - v0 - a*t", "Calculated final velocity"))
    assert s1 == s2 == s3


def test_equivalence_signature_distinct_equations_differ():
    """A velocity equation and a position equation get DIFFERENT signatures."""
    from apollo.provisioning.tag_mint import _equivalence_signature

    vel = _equivalence_signature(_eq_spec("eq.a", "v = v0 + a*t", "vel"))
    pos = _equivalence_signature(_eq_spec("eq.b", "x = v0*t + (1/2)*a*t**2", "pos"))
    assert vel != pos


def test_equivalence_signature_case_distinct_symbols_differ():
    """``m`` and ``M`` variable candidates get DIFFERENT signatures (case-sensitive
    symbol branch) — the counter to the 2026-06-30 false-merge."""
    from apollo.provisioning.tag_mint import _equivalence_signature

    lower = _equivalence_signature(_var_spec("varmap.vm_m", "m", "hanging mass"))
    upper = _equivalence_signature(_var_spec("varmap.vm_M", "M", "block mass"))
    assert lower != upper


def test_equivalence_signature_distinct_roles_same_prefix_differ():
    """``p1`` and ``p2`` (same symbol prefix, distinct physical quantities) get
    DIFFERENT signatures — subscripts are preserved, not stripped."""
    from apollo.provisioning.tag_mint import _equivalence_signature

    p1 = _equivalence_signature(_var_spec("varmap.vm_p1", "p1", "box-1 pressure"))
    p2 = _equivalence_signature(_var_spec("varmap.vm_p2", "p2", "box-2 pressure"))
    assert p1 != p2


def test_equivalence_signature_cross_kind_differs():
    """Same content text but different KIND never shares a signature (an equation
    and a definition are separate roles)."""
    from apollo.persistence.learner_model_seed import EntitySpec
    from apollo.provisioning.tag_mint import _equivalence_signature

    eq = _equivalence_signature(_eq_spec("eq.x", "v = v0 + a*t", "vel"))
    df = _equivalence_signature(
        EntitySpec(
            canonical_key="def.x",
            kind="definition",
            display_name="v = v0 + a*t",
            payload={},
        )
    )
    assert eq != df


def test_collapse_equivalent_candidates_merges_provenance_and_equation():
    """N equation specs with the same equation collapse to ONE representative that
    (a) preserves the earliest key, (b) merges every contributor's node id into
    ``payload['provenance']``, and (c) carries the source ``payload['equation']``.
    A distinct equation survives as its own candidate."""
    from apollo.provisioning.tag_mint import _collapse_equivalent_candidates

    specs = [
        _eq_spec("eq.eq_motion", "v = v0 + a*t", "Velocity formula"),
        _eq_spec("eq.eq_velocity_formula", "v0 + a*t = v", "Velocity equation"),
        _eq_spec("eq.eq1", "v - v0 - a*t", "Calculated final velocity"),
        _eq_spec("eq.eq_position", "x = v0*t + (1/2)*a*t**2", "Position formula"),
    ]
    collapsed, alias_map = _collapse_equivalent_candidates(specs)
    # 3 velocity dups -> 1; the position eq stays -> 2 total.
    assert len(collapsed) == 2
    keys = [s.canonical_key for s in collapsed]
    assert "eq.eq_motion" in keys  # earliest velocity rep kept
    assert "eq.eq_position" in keys
    rep = next(s for s in collapsed if s.canonical_key == "eq.eq_motion")
    # provenance records EVERY contributing node id (which steps contributed).
    prov_nodes = {p["node_id"] for p in rep.payload["provenance"]}
    assert prov_nodes == {"eq_motion", "eq_velocity_formula", "eq1"}
    # the source equation is attached for downstream role disambiguation.
    assert rep.payload["equation"] == "v = v0 + a*t"
    # the dropped duplicate keys alias to the representative key.
    assert alias_map == {"eq.eq_velocity_formula": "eq.eq_motion", "eq.eq1": "eq.eq_motion"}


def test_collapse_does_not_merge_case_distinct_symbols():
    """Two variable candidates differing ONLY by symbol case (m vs M) do NOT
    collapse — the equivalence key is case-sensitive."""
    from apollo.provisioning.tag_mint import _collapse_equivalent_candidates

    specs = [
        _var_spec("varmap.vm_m", "m", "hanging mass"),
        _var_spec("varmap.vm_M", "M", "block mass"),
    ]
    collapsed, alias_map = _collapse_equivalent_candidates(specs)
    assert len(collapsed) == 2
    assert alias_map == {}


def test_scope_summary_includes_equation_content():
    """An equation candidate's scope_summary carries the NORMALIZED equation, so
    the embedding tier merges cross-mint equation duplicates (identical content ->
    identical summary) while keeping distinct equations apart."""
    from apollo.provisioning.tag_mint import _scope_summary_for

    s1 = _scope_summary_for(_eq_spec("eq.a", "v = v0 + a*t", "Velocity formula"))
    s2 = _scope_summary_for(_eq_spec("eq.b", "v0 + a*t = v", "Some other label"))
    # different labels, same equation -> byte-identical summary (drives the merge).
    assert s1 == s2
    # a distinct equation yields a distinct summary.
    s3 = _scope_summary_for(_eq_spec("eq.c", "x = v0*t + (1/2)*a*t**2", "Position"))
    assert s3 != s1


# ---- Real-PG: mint-time collapse end-to-end -------------------------------- #


def _dup_role_problem() -> dict:
    """A single-problem reference solution reproducing the campaign's duplicate-
    per-role pattern: three equation steps that all encode ``v = v0 + a*t`` under
    different ids/labels, plus a DISTINCT position equation. Carries a page-level
    provenance so merged provenance records the page."""
    return {
        "id": "scrape.dup",
        "concept_id": "linear_motion",
        "difficulty": "intro",
        "problem_text": "Constant-acceleration kinematics.",
        "given_values": {"v0": 0.0, "a": 2.0, "t": 5.0},
        "target_unknown": "v",
        "provenance": {"document_id": 2, "page": 7},
        "reference_solution": [
            {
                "step": 1,
                "entry_type": "equation",
                "id": "eq_motion",
                "content": {"label": "Velocity formula", "symbolic": "v = v0 + a*t"},
                "depends_on": [],
            },
            {
                "step": 2,
                "entry_type": "equation",
                "id": "eq_velocity_formula",
                "content": {"label": "Velocity equation", "symbolic": "v0 + a*t = v"},
                "depends_on": [],
            },
            {
                "step": 3,
                "entry_type": "equation",
                "id": "eq1",
                "content": {"label": "Calculated final velocity", "symbolic": "v - v0 - a*t"},
                "depends_on": [],
            },
            {
                "step": 4,
                "entry_type": "equation",
                "id": "eq_position",
                "content": {"label": "Position formula", "symbolic": "x = v0*t + (1/2)*a*t**2"},
                "depends_on": [],
            },
        ],
    }


async def test_tag_and_mint_collapses_duplicate_role_entities(db_session):
    """END-TO-END: minting a reference solution with 3 duplicate velocity-equation
    steps + 1 distinct position equation yields 2 equation entities (not 4). The
    representative carries the merged provenance + the source equation payload.
    DISCRIMINATING: without the collapse, 4 equation rows persist."""
    ss_id, _subj = await _seed_course(db_session, slug="c-collapse")
    pair = _approved_pair(problem=_dup_role_problem(), search_space_id=ss_id)
    plan = await tag_and_mint(
        db_session,
        pair,
        chat_fn=_chat_returning(_tag_payload(concept_slug="linear_motion")),
        embed_fn=_embed_distinct,
    )
    rows = (
        (
            await db_session.execute(
                select(LearnerEntity)
                .where(LearnerEntity.concept_id == plan.concept_id)
                .where(LearnerEntity.kind == "equation")
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 2, [r.canonical_key for r in rows]
    by_key = {r.canonical_key: r for r in rows}
    # earliest velocity rep survived; the two later dups did not mint.
    assert "eq.eq_motion" in by_key
    assert "eq.eq_velocity_formula" not in by_key
    assert "eq.eq1" not in by_key
    assert "eq.eq_position" in by_key
    # collapse is surfaced on the MintPlan.
    assert set(plan.collapsed_entity_keys) == {"eq.eq_velocity_formula", "eq.eq1"}
    # the representative carries the source equation + merged provenance.
    rep = by_key["eq.eq_motion"]
    assert rep.payload["equation"] == "v = v0 + a*t"
    prov_nodes = {p["node_id"] for p in rep.payload["provenance"]}
    assert prov_nodes == {"eq_motion", "eq_velocity_formula", "eq1"}
    # provenance records the source page (which page contributed).
    assert all(p["page_ref"] == 7 for p in rep.payload["provenance"])


async def test_tag_and_mint_collapsed_prereq_resolves_to_representative(db_session):
    """A prereq edge naming a COLLAPSED duplicate (by bare id or canonical key)
    resolves to the surviving representative entity, so the edge is not silently
    dropped. DISCRIMINATING: without the collapse alias, the edge names an unminted
    key and is dropped (0 edges)."""
    ss_id, _subj = await _seed_course(db_session, slug="c-collapse-prq")
    pair = _approved_pair(problem=_dup_role_problem(), search_space_id=ss_id)
    # draft a prereq from the DISTINCT position eq to a COLLAPSED velocity dup
    # (bare id ``eq1``, which merged into eq.eq_motion).
    tag = _tag_payload(concept_slug="linear_motion", prereqs=[["eq_position", "eq1"]])
    plan = await tag_and_mint(
        db_session, pair, chat_fn=_chat_returning(tag), embed_fn=_embed_distinct
    )
    assert plan.prereq_pairs == [("eq_position", "eq1")]
    pos_id = plan.minted_entity_ids["eq.eq_position"]
    rep_id = plan.minted_entity_ids["eq.eq_motion"]
    edges = (
        (
            await db_session.execute(
                select(EntityPrereq)
                .where(EntityPrereq.from_entity_id == pos_id)
                .where(EntityPrereq.to_entity_id == rep_id)
            )
        )
        .scalars()
        .all()
    )
    assert len(edges) == 1  # resolved to the representative, not dropped


async def test_tag_and_mint_cross_mint_equivalent_equation_merges(db_session):
    """CROSS-MINT: a second authored set minting the SAME equation under a DIFFERENT
    key merges onto the first mint's entity via the equation-enriched scope_summary
    (identical content -> identical summary -> cosine 1.0 embedding merge). No new
    equation row for the duplicate. DISCRIMINATING: with the old thin summary the
    labels differ and the duplicate mints fresh."""
    ss_id, _subj = await _seed_course(db_session, slug="c-crossmint-eq")
    problem1 = _problem_dict(
        problem_id="scrape.m1",
        concept_slug="linear_motion",
        given={"v0": 0.0, "a": 2.0, "t": 5.0},
        target="v",
    )
    # replace the default bernoulli equation step with a velocity equation.
    problem1["reference_solution"] = [
        {
            "step": 1,
            "entry_type": "equation",
            "id": "eq_motion",
            "content": {"label": "Velocity formula", "symbolic": "v = v0 + a*t"},
            "depends_on": [],
        },
    ]
    plan1 = await tag_and_mint(
        db_session,
        _approved_pair(problem=problem1, search_space_id=ss_id),
        chat_fn=_chat_returning(_tag_payload(concept_slug="linear_motion")),
        embed_fn=_embed_distinct,
    )
    assert "eq.eq_motion" in plan1.minted_entity_ids

    problem2 = dict(problem1)
    problem2["id"] = "scrape.m2"
    problem2["reference_solution"] = [
        {
            "step": 1,
            "entry_type": "equation",
            "id": "eq_velocity_formula",
            "content": {"label": "A totally different label", "symbolic": "v0 + a*t = v"},
            "depends_on": [],
        },
    ]
    plan2 = await tag_and_mint(
        db_session,
        _approved_pair(problem=problem2, search_space_id=ss_id),
        chat_fn=_chat_returning(_tag_payload(concept_slug="linear_motion")),
        embed_fn=_embed_distinct,
    )
    # the second mint's equivalent equation merged onto mint 1's entity.
    assert "eq.eq_velocity_formula" in plan2.merged_entity_keys
    assert "eq.eq_velocity_formula" not in plan2.minted_entity_ids
    rows = (
        (
            await db_session.execute(
                select(LearnerEntity)
                .where(LearnerEntity.concept_id == plan1.concept_id)
                .where(LearnerEntity.kind == "equation")
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1  # one equation entity across both mints


async def test_tag_and_mint_cross_concept_same_equation_not_merged(db_session):
    """COUNTER-TEST: the SAME equation minted into two DIFFERENT concepts stays two
    distinct entities (concept-scoped dedup pool — the PR#74 invariant this fix must
    preserve)."""
    ss_id, _subj = await _seed_course(db_session, slug="c-xconcept-eq")
    problem_a = _problem_dict(problem_id="scrape.ca", concept_slug="concept-alpha")
    problem_a["reference_solution"] = [
        {
            "step": 1,
            "entry_type": "equation",
            "id": "eq_motion",
            "content": {"label": "vel", "symbolic": "v = v0 + a*t"},
            "depends_on": [],
        },
    ]
    problem_b = dict(problem_a)
    problem_b["id"] = "scrape.cb"
    problem_b["concept_id"] = "concept-beta"

    plan_a = await tag_and_mint(
        db_session,
        _approved_pair(problem=problem_a, search_space_id=ss_id),
        chat_fn=_chat_returning(_tag_payload(concept_slug="concept-alpha")),
        embed_fn=_embed_distinct,
    )
    plan_b = await tag_and_mint(
        db_session,
        _approved_pair(problem=problem_b, search_space_id=ss_id),
        chat_fn=_chat_returning(_tag_payload(concept_slug="concept-beta")),
        embed_fn=_embed_distinct,
    )
    assert plan_a.concept_id != plan_b.concept_id
    # each concept minted its own equation entity — no cross-concept fusion.
    assert "eq.eq_motion" in plan_a.minted_entity_ids
    assert "eq.eq_motion" in plan_b.minted_entity_ids
    assert plan_b.merged_entity_keys == []


# --------------------------------------------------------------------------- #
# G4.3 rework (lane B2.3 cross-review) — the CROSS-UPLOAD half of Finding D.
#
# Finding D (campaign/out/f1/provisioning-notes.md:70-83) is CROSS-UPLOAD
# duplication: two problems ingested as two SEPARATE POST /apollo/authored-sets
# calls into the SAME concept ran entity extraction twice with no cross-run dedup,
# so ~37 entities minted where ~15-18 were meaningful. The duplication is DOMINATED
# by NON-EQUATION kinds (def.def1..4, varmap.var1.., proc, simp) under DIFFERENT
# synthetic keys but the SAME content. The within-mint collapse only folds one
# call's own specs; the display-name scope_summary leaves these as embedding-tier
# misses. These tests reproduce the two-upload topology and pin the deterministic
# cross-mint content-equality pre-match that closes it for BOTH equation and non-
# equation kinds.
# --------------------------------------------------------------------------- #


def _finding_d_upload_1() -> dict:
    """First authored set: the ``named``-key extraction (eq_motion / def_acceleration
    / … ) — the survivors Finding D's reconciliation table treats as canonical."""
    return {
        "id": "scrape.up1",
        "concept_id": "linear_motion",
        "difficulty": "intro",
        "problem_text": "A cyclist accelerates from rest; find the final velocity.",
        "given_values": {"v0": 0.0, "a": 2.0, "t": 5.0},
        "target_unknown": "v",
        "provenance": {"document_id": 2, "page": 3},
        "reference_solution": [
            {
                "step": 1,
                "entry_type": "equation",
                "id": "eq_motion",
                "content": {"label": "Velocity formula", "symbolic": "v = v0 + a*t"},
                "depends_on": [],
            },
            {
                "step": 2,
                "entry_type": "definition",
                "id": "def_acceleration",
                "content": {"label": "Acceleration"},
                "depends_on": [],
            },
            {
                "step": 3,
                "entry_type": "definition",
                "id": "def_velocity",
                "content": {"label": "Velocity"},
                "depends_on": [],
            },
            {
                "step": 4,
                "entry_type": "variable_mapping",
                "id": "map_acceleration",
                "content": {"label": "Acceleration symbol"},
                "depends_on": [],
            },
            {
                "step": 5,
                "entry_type": "procedure_step",
                "id": "proc_substitute",
                "content": {"label": "Substitute values"},
                "depends_on": [],
            },
            {
                "step": 6,
                "entry_type": "simplification",
                "id": "simp_calc_velocity",
                "content": {"label": "Calculate velocity"},
                "depends_on": [],
            },
        ],
    }


def _finding_d_upload_2() -> dict:
    """Second authored set into the SAME concept: the ``synthetic``-key extraction
    (eq1 / def1.. / var1 / proc1 / simp1) whose steps are CONTENT-DUPLICATES of
    upload 1 under different ids — plus ONE genuinely new definition (``def_time``)
    that MUST still mint fresh (proves the pre-match is exact, not greedy). The
    equation duplicate carries a DIFFERENT label to prove equation dedup is label-
    independent (normalized-equation basis)."""
    return {
        "id": "scrape.up2",
        "concept_id": "linear_motion",
        "difficulty": "intro",
        "problem_text": "The same cyclist problem, re-scraped independently.",
        "given_values": {"v0": 0.0, "a": 2.0, "t": 5.0},
        "target_unknown": "v",
        "provenance": {"document_id": 2, "page": 4},
        "reference_solution": [
            {
                "step": 1,
                "entry_type": "equation",
                "id": "eq1",
                "content": {"label": "Calculated final velocity", "symbolic": "v0 + a*t = v"},
                "depends_on": [],
            },
            {
                "step": 2,
                "entry_type": "definition",
                "id": "def1",
                "content": {"label": "Acceleration"},
                "depends_on": [],
            },
            {
                "step": 3,
                "entry_type": "definition",
                "id": "def2",
                "content": {"label": "Velocity"},
                "depends_on": [],
            },
            {
                "step": 4,
                "entry_type": "variable_mapping",
                "id": "var1",
                "content": {"label": "Acceleration symbol"},
                "depends_on": [],
            },
            {
                "step": 5,
                "entry_type": "procedure_step",
                "id": "proc1",
                "content": {"label": "Substitute values"},
                "depends_on": [],
            },
            {
                "step": 6,
                "entry_type": "simplification",
                "id": "simp1",
                "content": {"label": "Calculate velocity"},
                "depends_on": [],
            },
            {
                "step": 7,
                "entry_type": "definition",
                "id": "def_time",
                "content": {"label": "Time"},
                "depends_on": [],
            },
        ],
    }


async def test_two_upload_finding_d_dedupes_all_kinds(db_session):
    """CROSS-UPLOAD Finding D: two authored sets into ONE concept. Upload 1 mints 6
    entities; upload 2's 6 content-duplicates (equation + definition ×2 + variable +
    procedure + simplification) each MERGE onto upload 1's entity via the
    deterministic content-equality pre-match, while its 1 genuinely-new definition
    mints fresh. Net 7 entities, not 13 — dedup holds for BOTH equation and NON-
    equation kinds. DISCRIMINATING: without the cross-mint pre-match the 6 non-slug
    duplicates mint fresh (13 rows) because their display-name scope_summary differs
    or the embedding tier misses."""
    ss_id, _subj = await _seed_course(db_session, slug="c-finding-d")
    tag = _chat_returning(_tag_payload(concept_slug="linear_motion"))

    plan1 = await tag_and_mint(
        db_session,
        _approved_pair(problem=_finding_d_upload_1(), search_space_id=ss_id),
        chat_fn=tag,
        embed_fn=_embed_distinct,
    )
    after1 = (
        (await db_session.execute(select(LearnerEntity).where(LearnerEntity.concept_id == plan1.concept_id)))
        .scalars()
        .all()
    )
    assert len(after1) == 6, [e.canonical_key for e in after1]

    plan2 = await tag_and_mint(
        db_session,
        _approved_pair(problem=_finding_d_upload_2(), search_space_id=ss_id),
        chat_fn=tag,
        embed_fn=_embed_distinct,
    )
    # every content-duplicate merged; only the new definition minted.
    assert set(plan2.merged_entity_keys) == {
        "eq.eq1",
        "def.def1",
        "def.def2",
        "varmap.var1",
        "proc.proc1",
        "simp.simp1",
    }
    assert set(plan2.minted_entity_ids) == {"def.def_time"}

    rows = (
        (await db_session.execute(select(LearnerEntity).where(LearnerEntity.concept_id == plan1.concept_id)))
        .scalars()
        .all()
    )
    assert len(rows) == 7, sorted(r.canonical_key for r in rows)
    # each duplicate reuses the FIRST upload's entity id (first-writer-wins).
    by_key = {r.canonical_key: r.id for r in rows}
    assert plan2.merged_entity_keys and all(
        k not in by_key for k in ("eq.eq1", "def.def1", "varmap.var1", "proc.proc1", "simp.simp1")
    )
    assert by_key["def.def_time"]  # the new quantity minted its own row


async def test_two_upload_symbol_bearing_variable_dedup_is_label_independent(db_session):
    """A variable candidate that DOES carry ``payload['symbol']`` dedups CROSS-UPLOAD
    on the SYMBOL (case-sensitive), independent of its display label — so a second
    upload's differently-labelled ``a`` merges, but an ``A`` (distinct symbol) does
    NOT (the m≡M guard, absolute in the deterministic pre-match). NOTE: the frozen
    reference-solution converter does not itself emit ``symbol`` payloads for
    ``variable_mapping`` steps (varmap entities dedup on the NAME basis, exercised by
    ``test_two_upload_finding_d_dedupes_all_kinds``); this test pins the symbol-basis
    behaviour directly against pre-seeded rows so the m≡M invariant is covered wherever
    a symbol payload IS present."""
    ss_id, subj_id = await _seed_course(db_session, slug="c-var-symbol")
    concept = Concept(course_id=subj_id.search_space_id, subject_slug=subj_id.slug, subject_display_name=subj_id.display_name, slug="linear_motion", display_name="Linear motion")
    db_session.add(concept)
    await db_session.flush()
    # Pre-seed two variable entities differing ONLY by symbol case (a vs A).
    db_session.add_all(
        [
            LearnerEntity(
                course_id=ss_id,
                concept_id=concept.id,
                canonical_key="varmap.a",
                kind="variable",
                display_name="acceleration",
                payload={"symbol": "a"},
                aliases=[],
                scope_summary="acceleration | symbol a | kind variable",
            ),
            LearnerEntity(
                course_id=ss_id,
                concept_id=concept.id,
                canonical_key="varmap.big_a",
                kind="variable",
                display_name="area",
                payload={"symbol": "A"},
                aliases=[],
                scope_summary="area | symbol A | kind variable",
            ),
        ]
    )
    await db_session.flush()

    from apollo.persistence.learner_model_seed import EntitySpec
    from apollo.provisioning.tag_mint import _equivalence_signature
    from apollo.provisioning.tag_mint_persist import load_concept_entities

    prior = {
        _equivalence_signature(e): int(e.id)
        for e in await load_concept_entities(db_session, concept_id=concept.id)
    }
    # a candidate for symbol 'a' with a DIFFERENT label matches the pre-seeded 'a'…
    dup_a = EntitySpec(
        canonical_key="varmap.dup",
        kind="variable",
        display_name="a totally different label",
        payload={"symbol": "a"},
    )
    assert _equivalence_signature(dup_a) in prior
    # …but a candidate for symbol 'A' matches the 'A' row, NOT the 'a' row (m≡M).
    dup_bigA = EntitySpec(
        canonical_key="varmap.dup2",
        kind="variable",
        display_name="acceleration",
        payload={"symbol": "A"},
    )
    assert prior[_equivalence_signature(dup_a)] != prior[_equivalence_signature(dup_bigA)]


async def test_two_upload_different_label_definition_not_deterministically_merged(db_session):
    """RESIDUAL-GAP GUARD (honest boundary): the deterministic pre-match is EXACT, not
    fuzzy. A non-equation duplicate whose display_name DIFFERS across uploads and
    carries no symbol/equation content signal is NOT merged by the content pre-match
    (with a distinct-embedding stub it mints fresh). Such near-duplicates remain the
    embedding/LLM-judge tier's job — the deterministic pass never guesses. This pins
    the boundary so a future fuzzy-widening change is a conscious decision."""
    ss_id, subj_id = await _seed_course(db_session, slug="c-diff-label")
    concept = Concept(course_id=subj_id.search_space_id, subject_slug=subj_id.slug, subject_display_name=subj_id.display_name, slug="linear_motion", display_name="Linear motion")
    db_session.add(concept)
    await db_session.flush()
    db_session.add(
        LearnerEntity(
            course_id=ss_id,
            concept_id=concept.id,
            canonical_key="def.def_acceleration",
            kind="definition",
            display_name="Acceleration",
            payload={},
            aliases=[],
            scope_summary="Acceleration | kind definition",
        )
    )
    await db_session.flush()

    problem = _problem_dict(problem_id="scrape.dl", concept_slug="linear_motion")
    problem["reference_solution"] = [
        {
            "step": 1,
            "entry_type": "definition",
            "id": "def1",
            "content": {"label": "Rate of change of velocity"},  # SAME concept, DIFFERENT label
            "depends_on": [],
        },
    ]
    plan = await tag_and_mint(
        db_session,
        _approved_pair(problem=problem, search_space_id=ss_id),
        chat_fn=_chat_returning(_tag_payload(concept_slug="linear_motion")),
        embed_fn=_embed_distinct,
    )
    # different label + no symbol/equation signal → deterministic pass does NOT merge.
    assert "def.def1" in plan.minted_entity_ids
    assert "def.def1" not in plan.merged_entity_keys


def test_equivalence_signature_payloadless_same_name_same_kind_merges():
    """DOCUMENTED, INTENTIONAL (adversarial display_name-fallback case): two PAYLOADLESS
    specs with the SAME kind + SAME display_name share ONE signature, so both the
    within-mint collapse AND the cross-mint pre-match FOLD them together. This is
    CORRECT, not the 2026-06-30 m≡M hazard: with no payload the display_name is the
    ONLY content signal, so identical kind+name entities in ONE concept are genuinely
    indistinguishable and folding them is the conservative outcome (the m≡M hazard is
    DISTINCT content wrongly merging — here the content is identical). The
    discriminators that DO keep payloadless specs apart are kind and name; concept
    scope (enforced by the caller — the pre-match loads only one concept_id's rows)
    bounds the merge to a single concept."""
    from apollo.persistence.learner_model_seed import EntitySpec
    from apollo.provisioning.tag_mint import _equivalence_signature

    a = EntitySpec(canonical_key="def.a", kind="definition", display_name="Net force", payload={})
    b = EntitySpec(canonical_key="def.b", kind="definition", display_name="Net force", payload={})
    assert _equivalence_signature(a) == _equivalence_signature(b)  # merge (correct)
    # kind is a discriminator: a definition and a condition never fuse.
    c = EntitySpec(canonical_key="cond.c", kind="condition", display_name="Net force", payload={})
    assert _equivalence_signature(a) != _equivalence_signature(c)
    # name is a discriminator: a different label stays distinct.
    d = EntitySpec(canonical_key="def.d", kind="definition", display_name="Net torque", payload={})
    assert _equivalence_signature(a) != _equivalence_signature(d)
