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

import pytest
from sqlalchemy import select

from apollo.persistence.learner_model_seed import (
    _ENTRY_TYPE_TO_KIND_PREFIX,
    reference_solution_to_entities,
)
from apollo.persistence.models import (
    Concept,
    EntityPrereq,
    KGEntity,
    Misconception,
    Subject,
)
from apollo.provisioning import run_promotion_lint
from apollo.provisioning.tag_mint import (
    ApprovedPair,
    MintPlan,
    TagMintError,
    tag_and_mint,
)
from database.models import SearchSpace

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
    """Seed SearchSpace -> Subject for one course. Returns (search_space_id,
    subject_id). The concept is resolved/created by tag_and_mint itself."""
    space = SearchSpace(name=f"Course {slug}", slug=slug, subject_name="Physics")
    db.add(space)
    await db.flush()
    subj = Subject(slug=f"s-{slug}", display_name="Sub", search_space_id=space.id)
    db.add(subj)
    await db.flush()
    return space.id, subj.id


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
    assert isinstance(concept.canonical_symbols.get("symbols"), list)
    assert concept.canonical_symbols["symbols"]  # non-empty symbol list
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
    definition = await load_concept_definition(db_session, concept_id=plan.concept_id)
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
    first_symbols = list(concept.canonical_symbols["symbols"])

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
    union_symbols = list(concept.canonical_symbols["symbols"])
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
        (await db_session.execute(select(KGEntity).where(KGEntity.concept_id == plan.concept_id)))
        .scalars()
        .all()
    )
    by_key = {r.canonical_key: r for r in rows}
    assert by_key["eq.bernoulli"].kind == "equation"
    assert by_key["proc.solve_p2"].kind == "procedure"
    # scope_summary authored (the dedup embedding source) — non-null.
    assert by_key["eq.bernoulli"].scope_summary


async def test_minted_misconception_is_kg_entity(db_session):
    """THE DEVIATION. An apollo_kg_entities row with kind='misconception' and
    payload['opposes_entity_key'] exists (via misconceptions_to_entities); NO
    write to apollo_misconceptions."""
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
                select(KGEntity)
                .where(KGEntity.concept_id == plan.concept_id)
                .where(KGEntity.kind == "misconception")
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    payload = rows[0].payload
    assert payload["opposes_entity_key"] == "eq.bernoulli"
    # the second-pass link resolved opposes_entity_key → opposes_entity_id.
    assert "opposes_entity_id" in payload

    # NO write to apollo_misconceptions (the DEVIATION — entities ONLY).
    misc_rows = (await db_session.execute(select(Misconception))).scalars().all()
    assert misc_rows == []


async def test_tag_and_mint_dedups_via_resolve_candidate(db_session):
    """When a candidate entity's scope_summary matches an existing in-course
    entity (≥0.92 cosine), tag_and_mint MERGES (reuses the id, no new row) and
    records it in MintPlan.merged_entity_keys."""
    ss_id, subj_id = await _seed_course(db_session, slug="c-dedup")
    # Pre-seed a concept + an entity whose canonical_key EXACTLY matches one the
    # mint will produce (eq.bernoulli) so the slug tier merges it deterministically.
    concept = Concept(subject_id=subj_id, slug="bernoulli_principle", display_name="Bernoulli")
    db_session.add(concept)
    await db_session.flush()
    existing = KGEntity(
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
                select(KGEntity)
                .where(KGEntity.concept_id == concept.id)
                .where(KGEntity.canonical_key == "eq.bernoulli")
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


async def test_tag_and_mint_links_opposes_bare_id(db_session):
    """REGRESSION (H1): link_opposes shares the BLOCKER's bare/prefixed key bug.
    A misconception whose opposes names a reference node by BARE id (bernoulli)
    must link to the minted entity (eq.bernoulli), not raise. DISCRIMINATING:
    reverting the bare-id alias REDs with TagMintError ('misconception opposes an
    unknown entity key')."""
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
    rows = (
        (
            await db_session.execute(
                select(KGEntity)
                .where(KGEntity.concept_id == plan.concept_id)
                .where(KGEntity.kind == "misconception")
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    payload = rows[0].payload
    assert payload["opposes_entity_key"] == "bernoulli"  # raw draft key preserved
    assert payload["opposes_entity_id"] == plan.minted_entity_ids["eq.bernoulli"]


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
        (await db_session.execute(_count(KGEntity, concept_id=plan1.concept_id))).scalars().all()
    )
    edges1 = (await db_session.execute(select(EntityPrereq))).scalars().all()

    plan2 = await tag_and_mint(db_session, pair, chat_fn=chat, embed_fn=_embed_distinct)
    rows2 = (
        (await db_session.execute(_count(KGEntity, concept_id=plan2.concept_id))).scalars().all()
    )
    edges2 = (await db_session.execute(select(EntityPrereq))).scalars().all()

    assert plan2.concept_id == plan1.concept_id
    assert len(rows2) == len(rows1)  # no new entities
    assert len(edges2) == len(edges1)  # no new prereqs
    assert plan2.authored_symbols == []  # nothing new to author (all unioned)


async def test_tag_and_mint_unmappable_tag_raises(db_session):
    """FAIL-CLOSED. A misconception opposes a key that resolves to no entity →
    TagMintError; no partial mint visible after the caller rolls back."""
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
    with pytest.raises(TagMintError):
        await tag_and_mint(
            db_session,
            pair,
            chat_fn=_chat_returning(_tag_payload()),
            embed_fn=_embed_distinct,
        )


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
    tag = _tag_payload(
        prereqs=[["solve_p2", "bernoulli"], ["eq.bernoulli", "eq.nonexistent"]]
    )
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


async def test_tag_and_mint_escalates_to_judge(db_session):
    """A candidate whose scope_summary lands in the 0.82–0.92 band against a pre-
    seeded entity escalates to tag/mint's injected ``_judge_distinct`` tier (which
    returns 'distinct', so the candidate mints fresh). Covers the band judge
    callable. The pre-seeded entity uses the 'BANDSEED' scope_summary that
    ``_embed_in_band`` places at cosine 0.87."""
    ss_id, subj_id = await _seed_course(db_session, slug="c-band")
    concept = Concept(subject_id=subj_id, slug="bernoulli_principle", display_name="Bernoulli")
    db_session.add(concept)
    await db_session.flush()
    seed_entity = KGEntity(
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
        concept_id=plan1.concept_id,
        spec=spec,
        scope_summary="updated summary",
    )
    assert inserted is False  # the RE-UPDATE branch
    assert entity_id == plan1.minted_entity_ids["eq.bernoulli"]
    row = (await db_session.execute(select(KGEntity).where(KGEntity.id == entity_id))).scalar_one()
    assert row.display_name == "Bernoulli (renamed)"
    assert row.scope_summary == "updated summary"


async def test_resolve_or_create_concept_creates_subject_when_absent(db_session):
    """``resolve_or_create_concept`` on a course with NO Subject creates a
    provisional subject first (covers the no-subject branch)."""
    from apollo.provisioning.tag_mint_persist import resolve_or_create_concept

    space = SearchSpace(name="No-subj tag", slug="c-tag-nosubj", subject_name="X")
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
    from apollo.persistence.models import KGEntity as _KGE
    from apollo.provisioning.tag_mint_persist import link_opposes

    ss_id, subj_id = await _seed_course(db_session, slug="c-noopp")
    concept = Concept(subject_id=subj_id, slug="bernoulli_principle", display_name="Bernoulli")
    db_session.add(concept)
    await db_session.flush()
    # A misconception entity carrying NO opposes_entity_key in its payload.
    misc = _KGE(
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
