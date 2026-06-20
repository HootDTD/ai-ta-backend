"""WU-3B2g Step 2 — promote pass/fail + idempotency unit tests (savepoint).

``promote`` is the promotion step: annotate the minted reference graph
(``annotate_reference_solution``, frozen) -> ``run_promotion_lint`` reading the
concept's AUTHORED ``canonical_symbols``/``normalization_map`` -> on PASS flip the
``apollo_concept_problems`` row tier 1->2, store the annotated reference solution
into ``payload``, set ``solution_source``, then ``project_canon`` (mocked here).
On FAIL it returns ``PromoteResult(promoted=False, failed_gate, diagnostic)`` so
the ORCHESTRATOR (not promote) writes the ``apollo_rejected_problems`` row.

``neo`` is an ``AsyncMock`` so ``project_canon`` is a no-op observable call; NO
Neo4j container, NO network, NO LLM. The savepoint ``db_session`` is real
pgvector. Tests Docker-skip cleanly but the gate requires GREEN-not-skipped.
"""

from __future__ import annotations

import sys
from unittest.mock import AsyncMock

import pytest

from apollo.knowledge_graph.canon_projection import CanonProjectionError
from apollo.persistence.models import Concept, ConceptProblem, Subject
from apollo.provisioning.promote import PromoteResult, promote
from apollo.provisioning.tag_mint import MintPlan
from database.models import SearchSpace

# The submodule object (the package re-exports the ``promote`` FUNCTION, which
# shadows the ``promote`` submodule attribute on the package — so resolve the
# module through sys.modules to monkeypatch ``project_canon`` on it).
promote_mod = sys.modules["apollo.provisioning.promote"]

# pytest.ini sets asyncio_mode = auto.


# --------------------------------------------------------------------------- #
# Fixtures — a known passing bernoulli problem + its authored symbols.
# --------------------------------------------------------------------------- #
def _bernoulli_problem() -> dict:
    """The problem dict an ApprovedPair carries (NO entity_key / declared_paths —
    promote annotates those). Same shape build_approved_pair produces."""
    return {
        "id": "bernoulli_horizontal_pipe_find_p2",
        "concept_id": "bernoulli_principle",
        "difficulty": "intro",
        "given_values": {
            "A1": 0.01,
            "A2": 0.005,
            "P1": 200000.0,
            "v1": 2.0,
            "rho": 1000.0,
        },
        "problem_text": (
            "Water flows through a horizontal pipe. Find the pressure P2."
        ),
        "target_unknown": "P2",
        "reference_solution": [
            {
                "id": "continuity",
                "step": 1,
                "entry_type": "equation",
                "content": {
                    "label": "Continuity (mass conservation)",
                    "symbolic": "rho*A1*v1 - rho*A2*v2",
                    "variables": ["rho", "A1", "v1", "A2", "v2"],
                },
                "depends_on": [],
            },
            {
                "id": "incompressibility",
                "step": 2,
                "entry_type": "condition",
                "content": {
                    "label": "Incompressibility assumption",
                    "applies_when": "density is constant",
                },
                "depends_on": [],
            },
            {
                "id": "bernoulli",
                "step": 3,
                "entry_type": "equation",
                "content": {
                    "label": "Bernoulli's equation",
                    "symbolic": (
                        "P1 + Rational(1,2)*rho*v1**2 + rho*g*h1 "
                        "- (P2 + Rational(1,2)*rho*v2**2 + rho*g*h2)"
                    ),
                    "variables": ["P1", "rho", "v1", "g", "h1", "P2", "v2", "h2"],
                },
                "depends_on": ["incompressibility"],
            },
            {
                "id": "horizontal_simplification",
                "step": 4,
                "entry_type": "simplification",
                "content": {
                    "applies_when": "h1 == h2",
                    "transformation": "rho*g*h1 and rho*g*h2 cancel",
                },
                "depends_on": ["bernoulli"],
            },
            {
                "id": "plan_apply_continuity",
                "step": 5,
                "entry_type": "procedure_step",
                "content": {
                    "order": 1,
                    "action": "use continuity with rho, A1, v1, A2 to solve for v2",
                    "purpose": "obtain v2 to plug into bernoulli at section 2",
                    "uses_equations": ["continuity"],
                },
                "depends_on": ["continuity"],
            },
            {
                "id": "plan_apply_horizontal_simplification",
                "step": 6,
                "entry_type": "procedure_step",
                "content": {
                    "order": 2,
                    "action": "set h1 == h2 so the gravitational terms cancel",
                    "purpose": "simplify bernoulli to relate P1, P2, v1, v2",
                    "uses_equations": ["bernoulli"],
                },
                "depends_on": ["bernoulli", "horizontal_simplification"],
            },
            {
                "id": "plan_solve_bernoulli_for_p2",
                "step": 7,
                "entry_type": "procedure_step",
                "content": {
                    "order": 3,
                    "action": "substitute v2 and known P1, rho, v1 and solve for P2",
                    "purpose": "produce the numerical answer for P2",
                    "uses_equations": ["bernoulli"],
                },
                "depends_on": [
                    "plan_apply_continuity",
                    "plan_apply_horizontal_simplification",
                ],
            },
        ],
    }


_AUTHORED_SYMBOLS = ["A", "P", "Q", "g", "h", "rho", "v"]
_NORMALIZATION = {
    "pressure": "P",
    "density": "rho",
    "velocity": "v",
    "area": "A",
    "height": "h",
    "gravity": "g",
    "flow rate": "Q",
}


async def _seed_concept_with_problem(
    db,
    *,
    slug: str,
    canonical_symbols: list[str] | None = None,
    normalization: dict | None = None,
):
    """Seed SearchSpace -> Subject -> Concept (with authored canonical_symbols) ->
    a Tier-1 ConceptProblem. Returns (search_space_id, concept_id, problem_id)."""
    space = SearchSpace(name=f"Course {slug}", slug=slug, subject_name="Physics")
    db.add(space)
    await db.flush()
    subj = Subject(slug=f"s-{slug}", display_name="Sub", search_space_id=space.id)
    db.add(subj)
    await db.flush()
    syms = _AUTHORED_SYMBOLS if canonical_symbols is None else canonical_symbols
    norm = _NORMALIZATION if normalization is None else normalization
    concept = Concept(
        subject_id=subj.id,
        slug=f"concept-{slug}",
        display_name="Concept",
        canonical_symbols={"symbols": list(syms), "description": {}},
        normalization_map=dict(norm),
    )
    db.add(concept)
    await db.flush()
    problem = ConceptProblem(
        concept_id=concept.id,
        problem_code="scrape.abc",
        difficulty="intro",
        payload={"id": "scrape.abc"},
        tier=1,
        solution_source=None,
        provenance={},
        search_space_id=space.id,
    )
    db.add(problem)
    await db.flush()
    return space.id, concept.id, problem.id


def _mint_plan(concept_id: int) -> MintPlan:
    return MintPlan(
        concept_id=concept_id,
        concept_slug="concept-x",
        authored_symbols=list(_AUTHORED_SYMBOLS),
        minted_entity_ids={},
        merged_entity_keys=[],
        prereq_pairs=[],
        misconception_keys=[],
    )


# --------------------------------------------------------------------------- #
# T-PR1 — pass flips tier + payload + calls project_canon
# --------------------------------------------------------------------------- #
async def test_promote_pass_flips_tier_and_payload(db_session):
    space, concept_id, problem_id = await _seed_concept_with_problem(
        db_session, slug="pr1"
    )
    neo = AsyncMock()
    result = await promote(
        db_session,
        neo,
        problem=_bernoulli_problem(),
        mint_plan=_mint_plan(concept_id),
        search_space_id=space,
        concept_problem_id=problem_id,
        existing_problem_hashes=set(),
    )

    assert isinstance(result, PromoteResult)
    assert result.promoted is True
    assert result.failed_gate is None

    row = await db_session.get(ConceptProblem, problem_id)
    assert row.tier == 2
    assert row.solution_source is not None
    # The annotated reference solution is stored (entity_key per step + paths).
    assert "reference_solution" in row.payload
    first_step = row.payload["reference_solution"][0]
    assert first_step.get("entity_key")
    assert row.payload.get("declared_paths")


# --------------------------------------------------------------------------- #
# T-PR1b — project_canon called with the mint plan's concept_id
# --------------------------------------------------------------------------- #
async def test_promote_pass_calls_project_canon_with_concept_id(db_session, monkeypatch):
    space, concept_id, problem_id = await _seed_concept_with_problem(
        db_session, slug="pr1b"
    )
    calls: list[dict] = []

    async def _fake_project_canon(db, neo, *, search_space_id, concept_id):
        calls.append({"search_space_id": search_space_id, "concept_id": concept_id})

    monkeypatch.setattr(
        promote_mod, "project_canon", _fake_project_canon
    )
    result = await promote(
        db_session,
        AsyncMock(),
        problem=_bernoulli_problem(),
        mint_plan=_mint_plan(concept_id),
        search_space_id=space,
        concept_problem_id=problem_id,
        existing_problem_hashes=set(),
    )
    assert result.promoted is True
    assert len(calls) == 1
    assert calls[0]["concept_id"] == concept_id
    assert calls[0]["search_space_id"] == space


# --------------------------------------------------------------------------- #
# T-PR2 — fail returns failed_gate, no flip, no project_canon
# --------------------------------------------------------------------------- #
async def test_promote_fail_returns_failed_gate_no_flip(db_session, monkeypatch):
    space, concept_id, problem_id = await _seed_concept_with_problem(
        db_session, slug="pr2"
    )
    # Inject a foreign symbol so gate 4 fails (the sole foreign-symbol guard).
    problem = _bernoulli_problem()
    problem["given_values"]["zzz_foreign"] = 1.0

    called = []

    async def _fake_project_canon(*a, **k):
        called.append(1)

    monkeypatch.setattr(
        promote_mod, "project_canon", _fake_project_canon
    )
    result = await promote(
        db_session,
        AsyncMock(),
        problem=problem,
        mint_plan=_mint_plan(concept_id),
        search_space_id=space,
        concept_problem_id=problem_id,
        existing_problem_hashes=set(),
    )

    assert result.promoted is False
    assert result.failed_gate == 4
    assert result.diagnostic
    row = await db_session.get(ConceptProblem, problem_id)
    assert row.tier == 1  # STAYS Tier-1
    assert not called  # project_canon NOT called on a lint failure


# --------------------------------------------------------------------------- #
# T-PR3 — promote reads the concept's AUTHORED symbols (non-vacuous gate 4)
# --------------------------------------------------------------------------- #
async def test_promote_reads_concept_authored_symbols(db_session):
    # With the concept's canonical_symbols EMPTIED, the same passing graph fails
    # gate 4 (foreign symbol) — proves promote reads the authored set, not a
    # vacuous one.
    space, concept_id, problem_id = await _seed_concept_with_problem(
        db_session, slug="pr3", canonical_symbols=[], normalization={}
    )
    result = await promote(
        db_session,
        AsyncMock(),
        problem=_bernoulli_problem(),
        mint_plan=_mint_plan(concept_id),
        search_space_id=space,
        concept_problem_id=problem_id,
        existing_problem_hashes=set(),
    )
    assert result.promoted is False
    assert result.failed_gate == 4


# --------------------------------------------------------------------------- #
# T-PR4 — promote passes existing hashes to gate 8
# --------------------------------------------------------------------------- #
async def test_promote_passes_existing_hashes_to_gate8(db_session):
    from apollo.provisioning import problem_dup_hash
    from apollo.schemas.problem import Problem

    space, concept_id, problem_id = await _seed_concept_with_problem(
        db_session, slug="pr4"
    )
    problem = _bernoulli_problem()
    # The dup hash gate 8 keys on is computed over the validated Problem (the lint
    # validates the annotated dict to a Problem before hashing).
    annotated = promote_mod._annotate(problem, _mint_plan(concept_id))
    dup = problem_dup_hash(Problem.model_validate(annotated))

    result = await promote(
        db_session,
        AsyncMock(),
        problem=problem,
        mint_plan=_mint_plan(concept_id),
        search_space_id=space,
        concept_problem_id=problem_id,
        existing_problem_hashes={dup},
    )
    assert result.promoted is False
    assert result.failed_gate == 8


# --------------------------------------------------------------------------- #
# T-PR5 — idempotent re-run (tier flip no-ops, project_canon re-called)
# --------------------------------------------------------------------------- #
async def test_promote_idempotent_rerun(db_session, monkeypatch):
    space, concept_id, problem_id = await _seed_concept_with_problem(
        db_session, slug="pr5"
    )
    n_canon = []

    async def _fake_project_canon(*a, **k):
        n_canon.append(1)

    monkeypatch.setattr(
        promote_mod, "project_canon", _fake_project_canon
    )

    r1 = await promote(
        db_session,
        AsyncMock(),
        problem=_bernoulli_problem(),
        mint_plan=_mint_plan(concept_id),
        search_space_id=space,
        concept_problem_id=problem_id,
        existing_problem_hashes=set(),
    )
    r2 = await promote(
        db_session,
        AsyncMock(),
        problem=_bernoulli_problem(),
        mint_plan=_mint_plan(concept_id),
        search_space_id=space,
        concept_problem_id=problem_id,
        existing_problem_hashes=set(),
    )
    assert r1.promoted is True and r2.promoted is True
    row = await db_session.get(ConceptProblem, problem_id)
    assert row.tier == 2  # still 2 (no-op flip)
    assert len(n_canon) == 2  # re-MERGEd idempotently both times


# --------------------------------------------------------------------------- #
# T-PR6 — canon error propagates; tier flip not rolled back inside promote
# --------------------------------------------------------------------------- #
async def test_promote_canon_error_propagates(db_session, monkeypatch):
    space, concept_id, problem_id = await _seed_concept_with_problem(
        db_session, slug="pr6"
    )

    async def _boom(*a, **k):
        raise CanonProjectionError(stage="merge_canon", last_error="neo down")

    monkeypatch.setattr(promote_mod, "project_canon", _boom)

    with pytest.raises(CanonProjectionError):
        await promote(
            db_session,
            AsyncMock(),
            problem=_bernoulli_problem(),
            mint_plan=_mint_plan(concept_id),
            search_space_id=space,
            concept_problem_id=problem_id,
            existing_problem_hashes=set(),
        )
    # The tier flip already flushed is NOT rolled back inside promote (the
    # caller's session owns rollback) — the row is tier=2 in this session.
    row = await db_session.get(ConceptProblem, problem_id)
    assert row.tier == 2


# --------------------------------------------------------------------------- #
# T-PR7 — promote RE-HOMES the Tier-1 row from the provisional concept onto the
# mint plan's REAL tagged concept (so list_problems_for_concept can select it).
# --------------------------------------------------------------------------- #
async def test_promote_rehomes_row_to_tagged_concept(db_session):
    """The scraped Tier-1 row is written under the provisional-inventory concept
    (scrape.py); stage-4 tag_and_mint resolves the REAL tagged concept. On promote
    the row must be RE-HOMED to ``mint_plan.concept_id`` AND flipped tier=2 — else
    the student selector (concept_id == session concept AND tier == 2) can never
    reach it. Dropping the re-home assignment REDs this test."""
    from apollo.overseer.problem_selector import list_problems_for_concept

    space = SearchSpace(name="Course pr7", slug="pr7", subject_name="Physics")
    db_session.add(space)
    await db_session.flush()
    subj = Subject(slug="s-pr7", display_name="Sub", search_space_id=space.id)
    db_session.add(subj)
    await db_session.flush()

    # The PROVISIONAL inventory concept the scraped row hangs off (never teachable).
    provisional = Concept(
        subject_id=subj.id,
        slug="provisional.inventory",
        display_name="Provisional inventory",
        canonical_symbols={"symbols": []},
        normalization_map={},
    )
    db_session.add(provisional)
    # The REAL tagged concept (authored canonical_symbols) tag_and_mint resolved.
    tagged = Concept(
        subject_id=subj.id,
        slug="concept-pr7",
        display_name="Concept",
        canonical_symbols={"symbols": list(_AUTHORED_SYMBOLS), "description": {}},
        normalization_map=dict(_NORMALIZATION),
    )
    db_session.add(tagged)
    await db_session.flush()

    # The Tier-1 row is homed on the PROVISIONAL concept (not the tagged one).
    row = ConceptProblem(
        concept_id=provisional.id,
        problem_code="scrape.rehome",
        difficulty="intro",
        payload=_bernoulli_problem(),
        tier=1,
        solution_source=None,
        provenance={},
        search_space_id=space.id,
    )
    db_session.add(row)
    await db_session.flush()
    assert row.concept_id == provisional.id  # precondition: homed on provisional

    result = await promote(
        db_session,
        AsyncMock(),
        problem=_bernoulli_problem(),
        mint_plan=_mint_plan(tagged.id),  # the REAL tagged concept
        search_space_id=space.id,
        concept_problem_id=row.id,
        existing_problem_hashes=set(),
    )
    assert result.promoted is True

    refreshed = await db_session.get(ConceptProblem, row.id)
    assert refreshed.tier == 2
    # RE-HOMED: the promoted row now lives under the tagged concept, NOT provisional.
    assert refreshed.concept_id == tagged.id

    # The student selector (tier==2 + concept filter) can now reach it.
    teachable = await list_problems_for_concept(db_session, concept_id=tagged.id)
    assert any(p.id == _bernoulli_problem()["id"] for p in teachable)
    # ...and it is NOT reachable under the provisional concept.
    provisional_pool = await list_problems_for_concept(
        db_session, concept_id=provisional.id
    )
    assert provisional_pool == []
