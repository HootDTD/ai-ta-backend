"""Subject-AGNOSTIC Apollo Stage-5 — end-to-end authored per-candidate pipeline.

ingest (Tier-1 inventory) -> provision_authored_problem (construct -> faithfulness
-> tag/mint -> CONTENT-GATED promote). Real-pgvector ``db_session`` savepoint;
``neo`` is an AsyncMock (project_canon observable, no Neo4j); all LLM/embed are
deterministic stubs. Docker-skips cleanly; the gate requires GREEN-not-skipped.

Proves the acceptance criteria:
  * AC #2 — a polisci argument problem PROMOTES with NO subject profile: its content
    carries no parseable equation, so the symbolic rigor gates {4,6,7}
    self-deactivate and it rides the structural core + faithfulness.
  * AC #3 — an un-constructable / unfaithful candidate is a CLEAN reject (never an
    abort).
  * Back-compat — a fluid worked problem PROMOTES: it carries equations, so the
    symbolic rigor gates self-activate and genuinely run (gate 4/5/7 enforced via
    the seeded canonical_symbols).
"""

from __future__ import annotations

import json
import sys
from unittest.mock import AsyncMock

from sqlalchemy import func, select

from apollo.persistence.models import (
    Concept,
    ConceptProblem,
    IngestRun,
    RejectedProblem,
    Subject,
)
from apollo.provisioning.ingest import ingest_authored_problems, load_authored_problems
from apollo.provisioning.orchestrator import provision_authored_problem
from database.models import SearchSpace

# pytest.ini sets asyncio_mode = auto.

# project_canon (the :Canon Neo4j MERGE) is patched to a no-op in the promote
# tests — this suite proves the PROMOTION decision + tier flip, not the Neo4j
# projection (covered by the knowledge_graph suite). Resolve the promote MODULE
# through sys.modules (the package re-exports the promote FUNCTION, shadowing it).
_promote_mod = sys.modules["apollo.provisioning.promote"]


async def _noop_project_canon(db, neo, *, search_space_id, concept_id):
    return None


def _chat_returning(payload):
    def _chat(*_a, **_k) -> str:
        return payload if isinstance(payload, str) else json.dumps(payload)

    return _chat


def _approving_judge():
    """Two-phase judge: Phase A paired, Phase B all-claims-entailed."""
    state = {"n": 0}

    def _judge(*_a, **_k) -> str:
        state["n"] += 1
        if state["n"] == 1:
            return json.dumps({"paired": True, "confidence": 0.95})
        return json.dumps({"claims": [{"claim": "the argument is sound", "entailed": True}]})

    return _judge


def _embed_distinct(text: str):
    import random

    rng = random.Random(text)
    return [rng.gauss(0.0, 1.0) for _ in range(64)]


def _tag_payload(concept_slug: str, display_name: str):
    return _chat_returning(
        {"concept_slug": concept_slug, "display_name": display_name, "prereqs": []}
    )


def _argument_reference_solution() -> list[dict]:
    return [
        {
            "step": 1,
            "entry_type": "definition",
            "id": "def_fed",
            "content": {"concept": "federalism", "meaning": "divided sovereignty"},
            "depends_on": [],
        },
        {
            "step": 2,
            "entry_type": "condition",
            "id": "premise",
            "content": {"applies_when": "authority split across levels"},
            "depends_on": ["def_fed"],
        },
        {
            "step": 3,
            "entry_type": "procedure_step",
            "id": "veto",
            "content": {"order": 1, "action": "identify veto points", "purpose": "show checks"},
            "depends_on": ["premise"],
        },
        {
            "step": 4,
            "entry_type": "procedure_step",
            "id": "concl",
            "content": {
                "order": 2,
                "action": "weigh checks vs blurred blame",
                "purpose": "verdict",
            },
            "depends_on": ["veto"],
        },
    ]


_POLISCI_RECORD = {
    "statement": "Argue whether federalism strengthens democratic accountability.",
    "solution": "Federalism creates multiple veto points that both check power and blur blame.",
    "worked_procedure": [{"order": 1, "text": "define federalism"}],
    "concept_slug": "federalism",
}


async def _seed_subject(db, *, slug: str):
    space = SearchSpace(name=f"Course {slug}", slug=slug, subject_name="X")
    db.add(space)
    await db.flush()
    subj = Subject(slug=f"s-{slug}", display_name="Sub", search_space_id=space.id)
    db.add(subj)
    await db.flush()
    prov = Concept(subject_id=subj.id, slug="provisional.inventory", display_name="Prov")
    db.add(prov)
    await db.flush()
    return space.id, subj.id, prov.id


async def _seed_ingest_run(db, *, search_space_id: int) -> IngestRun:
    """A real running ingest_run so provision_authored_problem can write an
    apollo_rejected_problems audit row for a per-candidate reject (run is not None)."""
    run = IngestRun(search_space_id=search_space_id, document_id=1, status="running")
    db.add(run)
    await db.flush()
    return run


async def _count_rejections(db, *, run: IngestRun) -> int:
    return (
        await db.execute(
            select(func.count())
            .select_from(RejectedProblem)
            .where(RejectedProblem.ingest_run_id == run.id)
        )
    ).scalar_one()


# --------------------------------------------------------------------------- #
# AC #2 — polisci argument promotes under qualitative
# --------------------------------------------------------------------------- #
async def test_polisci_authored_problem_promotes_under_qualitative(db_session, monkeypatch):
    monkeypatch.setattr(_promote_mod, "project_canon", _noop_project_canon)
    space, subj_id, prov_id = await _seed_subject(db_session, slug="ap-poli")
    # ingest: writes the Tier-1 inventory (commit=False keeps the test's outer
    # savepoint). No subject profile is detected — gates are content-derived at promote.
    await ingest_authored_problems(
        db_session,
        [_POLISCI_RECORD],
        subject_id=subj_id,
        concept_id=prov_id,
        search_space_id=space,
        commit=False,
    )


    authored = load_authored_problems([_POLISCI_RECORD], default_concept_slug="prov")[0][0]
    result = await provision_authored_problem(
        db_session,
        AsyncMock(),
        authored,
        search_space_id=space,
        ingest_concept_id=prov_id,
        construct_chat_fn=_chat_returning({"reference_solution": _argument_reference_solution()}),
        judge_fn=_approving_judge(),
        tag_chat_fn=_tag_payload("federalism", "Federalism"),
        embed_fn=_embed_distinct,
    )
    assert result.outcome == "promoted", result.diagnostic

    # the ingested Tier-1 row is now teachable (tier=2), re-homed to the federalism concept
    fed = (
        await db_session.execute(Concept.__table__.select().where(Concept.slug == "federalism"))
    ).fetchone()
    row = (
        await db_session.execute(
            ConceptProblem.__table__.select().where(
                ConceptProblem.problem_code == authored.problem_code
            )
        )
    ).fetchone()
    assert row.tier == 2
    assert row.concept_id == fed.id
    assert "reference_solution" in row.payload
    assert row.payload["reference_solution"][0].get("entity_key")  # annotated


# --------------------------------------------------------------------------- #
# AC #3 — clean rejects (never an abort)
# --------------------------------------------------------------------------- #
async def test_unconstructable_candidate_is_clean_reject(db_session):
    space, subj_id, prov_id = await _seed_subject(db_session, slug="ap-bad")
    await ingest_authored_problems(
        db_session,
        [_POLISCI_RECORD],
        subject_id=subj_id,
        concept_id=prov_id,
        search_space_id=space,
        commit=False,
    )
    authored = load_authored_problems([_POLISCI_RECORD], default_concept_slug="prov")[0][0]
    run = await _seed_ingest_run(db_session, search_space_id=space)
    result = await provision_authored_problem(
        db_session,
        AsyncMock(),
        authored,
        search_space_id=space,
        ingest_concept_id=prov_id,
        construct_chat_fn=_chat_returning("not json at all"),  # construction fails
        judge_fn=_approving_judge(),
        tag_chat_fn=_tag_payload("federalism", "Federalism"),
        embed_fn=_embed_distinct,
        run=run,
    )
    assert result.outcome == "rejected"
    assert result.stage == "construct"
    # run is not None -> an apollo_rejected_problems audit row is written
    assert await _count_rejections(db_session, run=run) == 1
    rej = (
        await db_session.execute(
            select(RejectedProblem).where(RejectedProblem.ingest_run_id == run.id)
        )
    ).scalar_one()
    assert rej.rejected_stage == "solution_draft"


async def test_unfaithful_candidate_is_clean_reject(db_session):
    space, subj_id, prov_id = await _seed_subject(db_session, slug="ap-unfaith")
    await ingest_authored_problems(
        db_session,
        [_POLISCI_RECORD],
        subject_id=subj_id,
        concept_id=prov_id,
        search_space_id=space,
        commit=False,
    )
    authored = load_authored_problems([_POLISCI_RECORD], default_concept_slug="prov")[0][0]

    def _rejecting_judge():
        state = {"n": 0}

        def _judge(*_a, **_k) -> str:
            state["n"] += 1
            if state["n"] == 1:
                return json.dumps({"paired": True, "confidence": 0.9})
            return json.dumps({"claims": [{"claim": "fabricated", "entailed": False}]})

        return _judge

    run = await _seed_ingest_run(db_session, search_space_id=space)
    result = await provision_authored_problem(
        db_session,
        AsyncMock(),
        authored,
        search_space_id=space,
        ingest_concept_id=prov_id,
        construct_chat_fn=_chat_returning({"reference_solution": _argument_reference_solution()}),
        judge_fn=_rejecting_judge(),
        tag_chat_fn=_tag_payload("federalism", "Federalism"),
        embed_fn=_embed_distinct,
        run=run,
    )
    assert result.outcome == "rejected"
    assert result.stage == "pairing_gate"
    # run is not None -> the pairing-gate reject is audited
    assert await _count_rejections(db_session, run=run) == 1
    rej = (
        await db_session.execute(
            select(RejectedProblem).where(RejectedProblem.ingest_run_id == run.id)
        )
    ).scalar_one()
    assert rej.rejected_stage == "pairing_gate"


async def test_tag_mint_failure_is_clean_reject(db_session):
    """A tag/mint failure (the LLM omits concept_slug -> TagMintError) is a CLEAN
    per-candidate reject, NOT a run abort (AC #3). DISCRIMINATING: removing the
    try/except around tag_and_mint lets the TagMintError propagate."""
    space, subj_id, prov_id = await _seed_subject(db_session, slug="ap-tagfail")
    await ingest_authored_problems(
        db_session,
        [_POLISCI_RECORD],
        subject_id=subj_id,
        concept_id=prov_id,
        search_space_id=space,
        commit=False,
    )
    authored = load_authored_problems([_POLISCI_RECORD], default_concept_slug="prov")[0][0]
    run = await _seed_ingest_run(db_session, search_space_id=space)
    result = await provision_authored_problem(
        db_session,
        AsyncMock(),
        authored,
        search_space_id=space,
        ingest_concept_id=prov_id,
        construct_chat_fn=_chat_returning({"reference_solution": _argument_reference_solution()}),
        judge_fn=_approving_judge(),
        # tag payload missing concept_slug -> tag_and_mint raises TagMintError
        tag_chat_fn=_chat_returning({"display_name": "X", "prereqs": []}),
        embed_fn=_embed_distinct,
        run=run,
    )
    assert result.outcome == "rejected"
    assert result.stage == "tag_mint"
    # run is not None -> the tag/mint reject is audited
    assert await _count_rejections(db_session, run=run) == 1
    rej = (
        await db_session.execute(
            select(RejectedProblem).where(RejectedProblem.ingest_run_id == run.id)
        )
    ).scalar_one()
    assert rej.rejected_stage == "tag_mint"


async def test_reject_without_run_writes_no_audit_row(db_session):
    """The guard side of the audit branch: when ``run`` is omitted (the default, the
    direct-driven path) a reject is still CLEAN but writes NO apollo_rejected_problems
    row — the ``if run is not None`` guard is what keeps _record_rejection off the
    no-run path. DISCRIMINATING: dropping the guard would NameError/crash here."""
    space, subj_id, prov_id = await _seed_subject(db_session, slug="ap-norun")
    await ingest_authored_problems(
        db_session,
        [_POLISCI_RECORD],
        subject_id=subj_id,
        concept_id=prov_id,
        search_space_id=space,
        commit=False,
    )
    authored = load_authored_problems([_POLISCI_RECORD], default_concept_slug="prov")[0][0]
    result = await provision_authored_problem(
        db_session,
        AsyncMock(),
        authored,
        search_space_id=space,
        ingest_concept_id=prov_id,
        construct_chat_fn=_chat_returning("not json at all"),  # construction fails
        judge_fn=_approving_judge(),
        tag_chat_fn=_tag_payload("federalism", "Federalism"),
        embed_fn=_embed_distinct,
        # run omitted -> default None -> no audit row
    )
    assert result.outcome == "rejected"
    assert result.stage == "construct"
    n_rows = (
        await db_session.execute(
            select(func.count())
            .select_from(RejectedProblem)
            .where(RejectedProblem.search_space_id == space)
        )
    ).scalar_one()
    assert n_rows == 0


# --------------------------------------------------------------------------- #
# Back-compat — a fluid worked problem promotes under quantitative (all 8 gates)
# --------------------------------------------------------------------------- #
def _bernoulli_reference_solution() -> list[dict]:
    return [
        {
            "id": "continuity",
            "step": 1,
            "entry_type": "equation",
            "content": {
                "label": "Continuity",
                "symbolic": "rho*A1*v1 - rho*A2*v2",
                "variables": ["rho", "A1", "v1", "A2", "v2"],
            },
            "depends_on": [],
        },
        {
            "id": "incompressibility",
            "step": 2,
            "entry_type": "condition",
            "content": {"label": "Incompressibility", "applies_when": "density is constant"},
            "depends_on": [],
        },
        {
            "id": "bernoulli",
            "step": 3,
            "entry_type": "equation",
            "content": {
                "label": "Bernoulli",
                "symbolic": "P1 + Rational(1,2)*rho*v1**2 + rho*g*h1 - (P2 + Rational(1,2)*rho*v2**2 + rho*g*h2)",
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
                "action": "use continuity to solve for v2",
                "purpose": "obtain v2",
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
                "action": "set h1 == h2",
                "purpose": "cancel gravity terms",
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
                "action": "substitute and solve for P2",
                "purpose": "final answer",
                "uses_equations": ["bernoulli"],
            },
            "depends_on": ["plan_apply_continuity", "plan_apply_horizontal_simplification"],
        },
    ]


async def test_fluid_authored_problem_promotes_under_quantitative(db_session, monkeypatch):
    monkeypatch.setattr(_promote_mod, "project_canon", _noop_project_canon)
    space, subj_id, prov_id = await _seed_subject(db_session, slug="ap-fluid")
    # Pre-seed the REAL tagged concept WITH canonical_symbols so gate 4/7 pass
    # (tag_and_mint reuses a concept by (search_space, slug)).
    bern = Concept(
        subject_id=subj_id,
        slug="bernoulli_principle",
        display_name="Bernoulli",
        canonical_symbols={"symbols": ["A", "P", "Q", "g", "h", "rho", "v"], "description": {}},
        normalization_map={
            "pressure": "P",
            "density": "rho",
            "velocity": "v",
            "area": "A",
            "height": "h",
            "gravity": "g",
            "flow rate": "Q",
        },
    )
    db_session.add(bern)
    await db_session.flush()

    fluid_record = {
        "statement": "Water flows through a horizontal pipe at 2.0 m/s; find the pressure P2 in kPa.",
        "solution": "P2 = 197 kPa",
        "worked_procedure": [{"order": 1, "text": "apply continuity then bernoulli"}],
        "given_values": {"A1": 0.01, "A2": 0.005, "P1": 200000.0, "v1": 2.0, "rho": 1000.0},
        "target_unknown": "P2",
        "concept_slug": "bernoulli_principle",
    }
    await ingest_authored_problems(
        db_session,
        [fluid_record],
        subject_id=subj_id,
        concept_id=prov_id,
        search_space_id=space,
        commit=False,
    )


    authored = load_authored_problems([fluid_record], default_concept_slug="prov")[0][0]
    result = await provision_authored_problem(
        db_session,
        AsyncMock(),
        authored,
        search_space_id=space,
        ingest_concept_id=prov_id,
        construct_chat_fn=_chat_returning({"reference_solution": _bernoulli_reference_solution()}),
        judge_fn=_approving_judge(),
        tag_chat_fn=_tag_payload("bernoulli_principle", "Bernoulli"),
        embed_fn=_embed_distinct,
    )
    assert result.outcome == "promoted", f"{result.stage}: {result.diagnostic}"

    row = (
        await db_session.execute(
            ConceptProblem.__table__.select().where(
                ConceptProblem.problem_code == authored.problem_code
            )
        )
    ).fetchone()
    assert row.tier == 2
    assert row.concept_id == bern.id
