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
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from apollo.knowledge_graph.canon_projection import CanonProjectionError
from apollo.persistence.models import Concept
from apollo.persistence.models import Problem as ProblemRecord
from apollo.provisioning.promote import PromoteResult, promote
from apollo.provisioning.tag_mint import MintPlan
from database.models import Course

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
        "problem_text": ("Water flows through a horizontal pipe. Find the pressure P2."),
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


def _argument_problem() -> dict:
    """A prose ARGUMENT problem (definition / condition / procedure_step; NO
    equations, empty givens, PROSE target). NO entity_key / declared_paths —
    ``promote`` annotates those. Mirrors test_promotion_lint._argument_graph."""
    return {
        "id": "polisci_federalism_disperses_power",
        "concept_id": "federalism",
        "difficulty": "standard",
        "given_values": {},
        "problem_text": (
            "Argue whether a federal system strengthens or weakens democratic accountability."
        ),
        "target_unknown": "whether federalism strengthens accountability",
        "reference_solution": [
            {
                "id": "def_federalism",
                "step": 1,
                "entry_type": "definition",
                "content": {
                    "concept": "federalism",
                    "meaning": "Sovereignty divided between national and subnational units.",
                },
                "depends_on": [],
            },
            {
                "id": "premise_dispersed_power",
                "step": 2,
                "entry_type": "condition",
                "content": {"applies_when": "authority is constitutionally split across levels"},
                "depends_on": ["def_federalism"],
            },
            {
                "id": "step_veto_points",
                "step": 3,
                "entry_type": "procedure_step",
                "content": {
                    "order": 1,
                    "action": "identify the multiple veto points federalism creates",
                    "purpose": "establish that power is checked at several levels",
                },
                "depends_on": ["premise_dispersed_power"],
            },
            {
                "id": "step_conclusion",
                "step": 4,
                "entry_type": "procedure_step",
                "content": {
                    "order": 2,
                    "action": "weigh dispersed checks against blurred responsibility",
                    "purpose": "reach a reasoned verdict on accountability",
                },
                "depends_on": ["step_veto_points"],
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
    """Seed Course -> Subject -> Concept (with authored canonical_symbols) ->
    a Tier-1 ProblemRecord. Returns (search_space_id, concept_id, problem_id)."""
    space = Course(name=f"Course {slug}", slug=slug, subject_name="Physics")
    db.add(space)
    await db.flush()
    subj = SimpleNamespace(slug=f"s-{slug}", display_name="Sub", search_space_id=space.id)
    syms = _AUTHORED_SYMBOLS if canonical_symbols is None else canonical_symbols
    norm = _NORMALIZATION if normalization is None else normalization
    concept = Concept(
        course_id=subj.search_space_id, subject_slug=subj.slug, subject_display_name=subj.display_name,
        slug=f"concept-{slug}",
        display_name="Concept",
        canonical_symbols=list(syms),
        symbol_metadata={"description": {}},
        normalization_map=dict(norm),
    )
    db.add(concept)
    await db.flush()
    problem = ProblemRecord.from_inventory_payload(
        {"id": "scrape.abc", "difficulty": "intro", "problem_text": ""},
        course_id=space.id,
        concept_id=concept.id,
        tier=1,
        solution_source=None,
        provenance={},
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


def _valid_bernoulli_strategy_paths() -> list[dict]:
    """Two distinct routes that jointly cover all seven steps and share the sink."""
    return [
        {
            "strategy_id": "continuity_route",
            "nodes": [
                "continuity",
                "incompressibility",
                "plan_apply_continuity",
                "plan_solve_bernoulli_for_p2",
            ],
            "milestones": ["plan_solve_bernoulli_for_p2"],
        },
        {
            "strategy_id": "bernoulli_route",
            "nodes": [
                "bernoulli",
                "horizontal_simplification",
                "plan_apply_horizontal_simplification",
                "plan_solve_bernoulli_for_p2",
            ],
            "milestones": ["plan_solve_bernoulli_for_p2"],
        },
    ]


def test_enumerated_paths_helper_is_inert_when_flag_off(monkeypatch):
    monkeypatch.delenv("APOLLO_MULTI_PATH", raising=False)
    annotated = promote_mod._annotate(_bernoulli_problem(), _mint_plan(1))

    def enumerator(_problem):
        raise AssertionError("flag-off enumerator must not run")

    assert (
        promote_mod._with_enumerated_paths(
            annotated,
            enumerator,
            concept_problem_id=1,
        )
        is annotated
    )


def test_enumerated_paths_helper_falls_back_on_error_and_invalid_combination(monkeypatch, caplog):
    monkeypatch.setenv("APOLLO_MULTI_PATH", "1")
    problem = _bernoulli_problem()
    annotated = promote_mod._annotate(problem, _mint_plan(1))

    def raises(_problem):
        raise RuntimeError("stubbed outage")

    assert promote_mod._with_enumerated_paths(annotated, raises, concept_problem_id=7) is annotated
    assert "provisioning_path_enumeration_fallback" in caplog.text

    def strict_subset(_problem):
        return [{"strategy_id": "short", "nodes": ["continuity"], "milestones": ["continuity"]}]

    assert (
        promote_mod._with_enumerated_paths(
            annotated,
            strict_subset,
            concept_problem_id=8,
        )
        is annotated
    )

    incomplete = _valid_bernoulli_strategy_paths()
    incomplete[0] = {
        **incomplete[0],
        "nodes": ["continuity", "plan_apply_continuity", "plan_solve_bernoulli_for_p2"],
    }
    assert (
        promote_mod._with_enumerated_paths(
            annotated,
            lambda _problem: incomplete,
            concept_problem_id=9,
        )
        is annotated
    )


def test_enumerated_paths_helper_replaces_legacy_path_with_valid_object_set(monkeypatch):
    monkeypatch.setenv("APOLLO_MULTI_PATH", "1")
    annotated = promote_mod._annotate(_bernoulli_problem(), _mint_plan(1))
    enumerated = _valid_bernoulli_strategy_paths()

    candidate = promote_mod._with_enumerated_paths(
        annotated,
        lambda _problem: enumerated,
        concept_problem_id=10,
    )

    assert candidate is not annotated
    assert candidate["declared_paths"] == enumerated
    assert all(isinstance(path, dict) for path in candidate["declared_paths"])
    assert promote_mod.validate_reference_graph(candidate).ok
    assert isinstance(annotated["declared_paths"][0], list)


# --------------------------------------------------------------------------- #
# T-PR1 — pass flips tier + payload + calls project_canon
# --------------------------------------------------------------------------- #
async def test_promote_pass_flips_tier_and_payload(db_session):
    space, concept_id, problem_id = await _seed_concept_with_problem(db_session, slug="pr1")
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

    row = await db_session.get(ProblemRecord, problem_id)
    assert row.tier == 2
    assert row.solution_source is not None
    # The annotated reference solution is stored (entity_key per step + paths).
    first_step = row.reference_solution["steps"][0]
    assert first_step.get("entity_key")
    assert row.payload_extra.get("declared_paths")


# --------------------------------------------------------------------------- #
# T-PR1b — project_canon called with the mint plan's concept_id
# --------------------------------------------------------------------------- #
async def test_promote_pass_calls_project_canon_with_concept_id(db_session, monkeypatch):
    space, concept_id, problem_id = await _seed_concept_with_problem(db_session, slug="pr1b")
    calls: list[dict] = []

    async def _fake_project_canon(db, neo, *, search_space_id, concept_id):
        calls.append({"search_space_id": search_space_id, "concept_id": concept_id})

    monkeypatch.setattr(promote_mod, "project_canon", _fake_project_canon)
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


async def test_multi_path_flag_off_never_calls_enumerator_and_preserves_legacy_payload(
    db_session, monkeypatch
):
    space, concept_id, problem_id = await _seed_concept_with_problem(db_session, slug="mp-off")
    monkeypatch.delenv("APOLLO_MULTI_PATH", raising=False)
    original_problem = _bernoulli_problem()

    def enumerator(_problem):
        raise AssertionError("flag-off enumerator must not be called")

    result = await promote(
        db_session,
        AsyncMock(),
        problem=original_problem,
        mint_plan=_mint_plan(concept_id),
        search_space_id=space,
        concept_problem_id=problem_id,
        existing_problem_hashes=set(),
        path_enumerator=enumerator,
    )
    assert result.promoted
    row = await db_session.get(ProblemRecord, problem_id)
    assert row.payload_extra["declared_paths"] == [
        [step["id"] for step in original_problem["reference_solution"]]
    ]


async def test_multi_path_enumerator_failure_falls_back_and_promotion_succeeds(
    db_session, monkeypatch
):
    space, concept_id, problem_id = await _seed_concept_with_problem(db_session, slug="mp-fail")
    monkeypatch.setenv("APOLLO_MULTI_PATH", "1")

    def enumerator(_problem):
        raise RuntimeError("stubbed enumeration failure")

    result = await promote(
        db_session,
        AsyncMock(),
        problem=_bernoulli_problem(),
        mint_plan=_mint_plan(concept_id),
        search_space_id=space,
        concept_problem_id=problem_id,
        existing_problem_hashes=set(),
        path_enumerator=enumerator,
    )
    assert result.promoted
    row = await db_session.get(ProblemRecord, problem_id)
    assert len(row.payload_extra["declared_paths"]) == 1
    assert isinstance(row.payload_extra["declared_paths"][0], list)


async def test_multi_path_valid_replacement_writes_only_object_paths(db_session, monkeypatch):
    space, concept_id, problem_id = await _seed_concept_with_problem(db_session, slug="mp-valid")
    monkeypatch.setenv("APOLLO_MULTI_PATH", "1")
    enumerated = _valid_bernoulli_strategy_paths()

    result = await promote(
        db_session,
        AsyncMock(),
        problem=_bernoulli_problem(),
        mint_plan=_mint_plan(concept_id),
        search_space_id=space,
        concept_problem_id=problem_id,
        existing_problem_hashes=set(),
        path_enumerator=lambda _problem: enumerated,
    )

    assert result.promoted
    row = await db_session.get(ProblemRecord, problem_id)
    assert row.payload_extra["declared_paths"] == enumerated
    assert all(isinstance(path, dict) for path in row.payload_extra["declared_paths"])
    assert promote_mod.validate_reference_graph(
        row.to_pydantic_payload(concept_slug="concept-x")
    ).ok


async def test_multi_path_joint_cover_failure_falls_back_to_legacy_path(db_session, monkeypatch):
    space, concept_id, problem_id = await _seed_concept_with_problem(db_session, slug="mp-cover")
    monkeypatch.setenv("APOLLO_MULTI_PATH", "1")
    incomplete = _valid_bernoulli_strategy_paths()
    incomplete[0] = {
        **incomplete[0],
        "nodes": ["continuity", "plan_apply_continuity", "plan_solve_bernoulli_for_p2"],
    }

    result = await promote(
        db_session,
        AsyncMock(),
        problem=_bernoulli_problem(),
        mint_plan=_mint_plan(concept_id),
        search_space_id=space,
        concept_problem_id=problem_id,
        existing_problem_hashes=set(),
        path_enumerator=lambda _problem: incomplete,
    )

    assert result.promoted
    row = await db_session.get(ProblemRecord, problem_id)
    assert row.payload_extra["declared_paths"] == [
        [step["id"] for step in _bernoulli_problem()["reference_solution"]]
    ]


# --------------------------------------------------------------------------- #
# T-PR1c — a threaded solution_source is persisted (WU-AAS audit bug #1)
# --------------------------------------------------------------------------- #
async def test_promote_persists_threaded_solution_source(db_session):
    """A caller that knows the true per-problem source (e.g. an authored set's
    paired-EXTRACTED solution) can thread ``solution_source`` into ``promote`` and
    it lands on the row, instead of the row always defaulting to ``"generated"``."""
    space, concept_id, problem_id = await _seed_concept_with_problem(db_session, slug="pr1c")
    result = await promote(
        db_session,
        AsyncMock(),
        problem=_bernoulli_problem(),
        mint_plan=_mint_plan(concept_id),
        search_space_id=space,
        concept_problem_id=problem_id,
        existing_problem_hashes=set(),
        solution_source="extracted",
    )

    assert result.promoted is True
    row = await db_session.get(ProblemRecord, problem_id)
    assert row.solution_source == "extracted"


# --------------------------------------------------------------------------- #
# T-PR1d — omitting solution_source still defaults to "generated" (regression)
# --------------------------------------------------------------------------- #
async def test_promote_defaults_solution_source_when_omitted(db_session):
    space, concept_id, problem_id = await _seed_concept_with_problem(db_session, slug="pr1d")
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
    row = await db_session.get(ProblemRecord, problem_id)
    assert row.solution_source == "generated"


# --------------------------------------------------------------------------- #
# T-PR1e — a pre-stamped Tier-1 source is preserved over the threaded value
# --------------------------------------------------------------------------- #
async def test_promote_preserves_prestamped_solution_source(db_session):
    """The default-only-if-falsy guard keeps ``promote`` idempotent: a Tier-1 row
    already stamped (e.g. single-authored ingest's ``"authored"``) is NOT
    overwritten by a threaded value on (re-)promote."""
    space, concept_id, problem_id = await _seed_concept_with_problem(db_session, slug="pr1e")
    row = await db_session.get(ProblemRecord, problem_id)
    row.solution_source = "authored"
    await db_session.flush()

    result = await promote(
        db_session,
        AsyncMock(),
        problem=_bernoulli_problem(),
        mint_plan=_mint_plan(concept_id),
        search_space_id=space,
        concept_problem_id=problem_id,
        existing_problem_hashes=set(),
        solution_source="extracted",
    )

    assert result.promoted is True
    row = await db_session.get(ProblemRecord, problem_id)
    assert row.solution_source == "authored"


# --------------------------------------------------------------------------- #
# T-PR2 — fail returns failed_gate, no flip, no project_canon
# --------------------------------------------------------------------------- #
async def test_promote_fail_returns_failed_gate_no_flip(db_session, monkeypatch):
    space, concept_id, problem_id = await _seed_concept_with_problem(db_session, slug="pr2")
    # Inject a foreign symbol so gate 4 fails (the sole foreign-symbol guard). It
    # must be UNGROUNDED — appearing only inside an equation, not given/defined —
    # since a given/defined symbol is now correctly author-grounded (WU-AAS G4.2).
    problem = _bernoulli_problem()
    for step in problem["reference_solution"]:
        if step["id"] == "continuity":
            step["content"]["symbolic"] = "rho*A1*v1 - rho*A2*v2 + zzz_foreign"

    called = []

    async def _fake_project_canon(*a, **k):
        called.append(1)

    monkeypatch.setattr(promote_mod, "project_canon", _fake_project_canon)
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
    row = await db_session.get(ProblemRecord, problem_id)
    assert row.tier == 1  # STAYS Tier-1
    assert not called  # project_canon NOT called on a lint failure


# --------------------------------------------------------------------------- #
# T-PR3 — a TABLE-LESS concept promotes via internal grounding (Option 2)
# --------------------------------------------------------------------------- #
async def test_promote_table_less_concept_promotes_via_internal_grounding(db_session):
    """A fresh auto-minted concept (EMPTY canonical_symbols) no longer auto-fails
    gate 4: the same self-grounded bernoulli PROMOTES via internal symbol grounding
    (spec §4.2 fixes the fresh-concept bootstrap that rejected the AAE 333 case). The
    symbolic rigor gates fire+pass on it, so it is stamped mechanically_verified. Old
    code rejected it at gate 4 (every symbol foreign vs the empty table)."""
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
    assert result.promoted is True, result.diagnostic
    row = await db_session.get(ProblemRecord, problem_id)
    assert row.payload_extra["verification"] == "mechanically_verified"


# --------------------------------------------------------------------------- #
# T-PR3b — content-derived active_gates + the "how it cleared" provenance stamp
# --------------------------------------------------------------------------- #
async def test_promote_argument_graph_promotes_with_faithfulness_only_stamp(db_session):
    """A prose argument graph (no equations) PROMOTES with NO subject profile:
    content-derived active_gates drop the symbolic gates, gate 5 rides its structural
    half, and NO mechanical oracle fires -> the payload is stamped
    ``faithfulness_only`` (rode structure + the LLM faithfulness judge only)."""
    space, concept_id, problem_id = await _seed_concept_with_problem(
        db_session, slug="argp", canonical_symbols=[], normalization={}
    )
    result = await promote(
        db_session,
        AsyncMock(),
        problem=_argument_problem(),
        mint_plan=_mint_plan(concept_id),
        search_space_id=space,
        concept_problem_id=problem_id,
        existing_problem_hashes=set(),
    )
    assert result.promoted is True, result.diagnostic
    row = await db_session.get(ProblemRecord, problem_id)
    assert row.payload_extra["verification"] == "faithfulness_only"


async def test_promote_symbolic_graph_stamps_mechanically_verified(db_session):
    """A symbolic graph with a seeded table promotes and is stamped
    ``mechanically_verified`` — the symbolic rigor layer (gates 4/6/7) was applicable
    and passed."""
    space, concept_id, problem_id = await _seed_concept_with_problem(db_session, slug="symv")
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
    row = await db_session.get(ProblemRecord, problem_id)
    assert row.payload_extra["verification"] == "mechanically_verified"


async def test_promote_rejects_malformed_problem_cleanly(db_session):
    """REGRESSION: _annotate runs BEFORE run_promotion_lint's gate-1 validation,
    so a step whose entry_type is outside the frozen mint map would KeyError in
    _annotate and surface as a per-DOCUMENT abort. promote must convert it to a
    clean gate-1 rejection (one bad candidate must not sink the document).
    DISCRIMINATING: removing the guard REDs with KeyError."""
    problem = {
        "id": "scrape.bad",
        "concept_id": "bernoulli_principle",
        "difficulty": "intro",
        "problem_text": "x",
        "given_values": {},
        "target_unknown": "P2",
        "reference_solution": [
            {"step": 1, "entry_type": "NOT_A_REAL_TYPE", "id": "x", "content": {}},
        ],
    }
    mint_plan = MintPlan(
        concept_id=1,
        concept_slug="bernoulli_principle",
        authored_symbols=[],
        minted_entity_ids={},
        merged_entity_keys=[],
        prereq_pairs=[],
        misconception_keys=[],
    )
    result = await promote(
        db_session,
        None,
        problem=problem,
        mint_plan=mint_plan,
        search_space_id=1,
        concept_problem_id=1,
        existing_problem_hashes=set(),
    )
    assert result.promoted is False
    assert result.failed_gate == 1


# --------------------------------------------------------------------------- #
# T-PR4 — promote passes existing hashes to gate 8
# --------------------------------------------------------------------------- #
async def test_promote_passes_existing_hashes_to_gate8(db_session):
    from apollo.provisioning import problem_dup_hash
    from apollo.schemas.problem import Problem

    space, concept_id, problem_id = await _seed_concept_with_problem(db_session, slug="pr4")
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
    space, concept_id, problem_id = await _seed_concept_with_problem(db_session, slug="pr5")
    n_canon = []

    async def _fake_project_canon(*a, **k):
        n_canon.append(1)

    monkeypatch.setattr(promote_mod, "project_canon", _fake_project_canon)

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
    row = await db_session.get(ProblemRecord, problem_id)
    assert row.tier == 2  # still 2 (no-op flip)
    assert len(n_canon) == 2  # re-MERGEd idempotently both times


# --------------------------------------------------------------------------- #
# T-PR6 — canon error propagates; tier flip not rolled back inside promote
# --------------------------------------------------------------------------- #
async def test_promote_canon_error_propagates(db_session, monkeypatch):
    space, concept_id, problem_id = await _seed_concept_with_problem(db_session, slug="pr6")

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
    row = await db_session.get(ProblemRecord, problem_id)
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

    space = Course(name="Course pr7", slug="pr7", subject_name="Physics")
    db_session.add(space)
    await db_session.flush()
    subj = SimpleNamespace(slug="s-pr7", display_name="Sub", search_space_id=space.id)

    # The PROVISIONAL inventory concept the scraped row hangs off (never teachable).
    provisional = Concept(
        course_id=subj.search_space_id, subject_slug=subj.slug, subject_display_name=subj.display_name,
        slug="provisional.inventory",
        display_name="Provisional inventory",
        canonical_symbols=[],
        normalization_map={},
    )
    db_session.add(provisional)
    # The REAL tagged concept (authored canonical_symbols) tag_and_mint resolved.
    tagged = Concept(
        course_id=subj.search_space_id, subject_slug=subj.slug, subject_display_name=subj.display_name,
        slug="concept-pr7",
        display_name="Concept",
        canonical_symbols=list(_AUTHORED_SYMBOLS),
        symbol_metadata={"description": {}},
        normalization_map=dict(_NORMALIZATION),
    )
    db_session.add(tagged)
    await db_session.flush()

    # The Tier-1 row is homed on the PROVISIONAL concept (not the tagged one).
    row = ProblemRecord.from_inventory_payload(
        _bernoulli_problem(),
        course_id=space.id,
        concept_id=provisional.id,
        tier=1,
        solution_source=None,
        provenance={},
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

    refreshed = await db_session.get(ProblemRecord, row.id)
    assert refreshed.tier == 2
    # RE-HOMED: the promoted row now lives under the tagged concept, NOT provisional.
    assert refreshed.concept_id == tagged.id

    # The student selector (tier==2 + concept filter) can now reach it.
    teachable = await list_problems_for_concept(
        db_session, concept_id=tagged.id, search_space_id=space.id
    )
    assert any(p.id == _bernoulli_problem()["id"] for p in teachable)
    # ...and it is NOT reachable under the provisional concept.
    provisional_pool = await list_problems_for_concept(
        db_session, concept_id=provisional.id, search_space_id=space.id
    )
    assert provisional_pool == []
