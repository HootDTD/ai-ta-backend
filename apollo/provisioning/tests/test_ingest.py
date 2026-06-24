"""Subject-fluid Apollo Stage-1 — authored-problem ingest tests.

Classification + loader fail-soft are PURE (no DB). The Tier-1 write, independent
commit, and profile detect+persist use the real-pgvector ``db_session`` savepoint
fixture (Docker-skips cleanly; the gate requires GREEN-not-skipped).
"""

from __future__ import annotations

from apollo.persistence.models import ConceptProblem, Subject
from apollo.provisioning.ingest import (
    AuthoredProblem,
    authored_problem_code,
    classify_completeness,
    ingest_authored_problems,
    load_authored_problems,
    write_authored_tier1_problems,
)
from apollo.provisioning.subject_profile import (
    PROFILE_QUALITATIVE_ARGUMENTATIVE,
    PROFILE_QUANTITATIVE_SYMBOLIC,
)
from database.models import SearchSpace

# pytest.ini sets asyncio_mode = auto.


# --------------------------------------------------------------------------- #
# Completeness classification (pure)
# --------------------------------------------------------------------------- #


def test_classify_worked():
    assert classify_completeness("ans", [{"step": 1}]) == "worked"


def test_classify_answer_only():
    assert classify_completeness("the answer is 42", None) == "answer_only"
    assert classify_completeness("ans", []) == "answer_only"  # empty procedure


def test_classify_none():
    assert classify_completeness(None, None) == "none"
    assert classify_completeness("   ", None) == "none"  # blank solution


# --------------------------------------------------------------------------- #
# Loader — fail-soft, content-derived code, all three flavors
# --------------------------------------------------------------------------- #


def _mixed_records() -> list[dict]:
    return [
        {  # worked
            "statement": "Find the pressure P2 in a horizontal pipe.",
            "solution": "P2 = 197 kPa",
            "worked_procedure": [{"order": 1, "action": "apply continuity"}],
            "given_values": {"v1": 2.0},
            "target_unknown": "P2",
            "concept_slug": "bernoulli",
        },
        {  # answer-only
            "statement": "Compute the exit velocity of the nozzle.",
            "solution": "v2 = 4.0 m/s",
            "concept_slug": "continuity",
        },
        {  # none
            "statement": "Derive Bernoulli's equation from energy conservation.",
            "concept_slug": "bernoulli",
        },
        {"problem_text": ""},  # malformed -> dropped
        "not a mapping",  # malformed -> dropped
    ]


def test_loader_classifies_and_drops_malformed():
    problems, dropped = load_authored_problems(_mixed_records(), default_concept_slug="prov")
    assert dropped == 2
    assert [p.completeness for p in problems] == ["worked", "answer_only", "none"]
    # content-derived code is stable + statement-derived
    assert problems[0].problem_code == authored_problem_code(problems[0].statement)


def test_loader_defaults_concept_slug_and_difficulty():
    problems, _ = load_authored_problems(
        [{"statement": "x", "solution": "y"}], default_concept_slug="prov.fallback"
    )
    assert problems[0].concept_slug == "prov.fallback"
    assert problems[0].difficulty == "standard"


def test_authored_problem_may_omit_givens_and_target():
    # An argument problem carries neither numeric givens nor a symbolic target.
    p = AuthoredProblem(
        problem_code="authored.x",
        concept_slug="federalism",
        statement="Argue whether federalism strengthens accountability.",
        completeness="none",
    )
    assert p.given_values == {}
    assert p.target_unknown == ""


# --------------------------------------------------------------------------- #
# Tier-1 write — explicit tier=1, idempotent, authored solution_source
# --------------------------------------------------------------------------- #


async def _seed_subject_concept(db, *, slug: str):
    from apollo.persistence.models import Concept

    space = SearchSpace(name=f"Course {slug}", slug=slug, subject_name="X")
    db.add(space)
    await db.flush()
    subj = Subject(slug=f"s-{slug}", display_name="Sub", search_space_id=space.id)
    db.add(subj)
    await db.flush()
    concept = Concept(subject_id=subj.id, slug=f"prov-{slug}", display_name="Prov")
    db.add(concept)
    await db.flush()
    return space.id, subj.id, concept.id


async def test_tier1_write_is_explicit_tier1_and_authored(db_session):
    space, subj_id, concept_id = await _seed_subject_concept(db_session, slug="ing1")
    problems, _ = load_authored_problems(_mixed_records(), default_concept_slug="prov")
    n = await write_authored_tier1_problems(
        db_session, problems, concept_id=concept_id, search_space_id=space
    )
    assert n == 3
    rows = (
        (await db_session.execute(ConceptProblem.__table__.select().where(
            ConceptProblem.concept_id == concept_id
        ))).fetchall()
    )
    assert len(rows) == 3
    for row in rows:
        assert row.tier == 1  # NEVER the teachable default
        assert row.solution_source == "authored"
        assert row.payload["authored"]["completeness"] in {"worked", "answer_only", "none"}


async def test_tier1_write_is_idempotent(db_session):
    space, subj_id, concept_id = await _seed_subject_concept(db_session, slug="ing2")
    problems, _ = load_authored_problems(_mixed_records(), default_concept_slug="prov")
    first = await write_authored_tier1_problems(
        db_session, problems, concept_id=concept_id, search_space_id=space
    )
    second = await write_authored_tier1_problems(
        db_session, problems, concept_id=concept_id, search_space_id=space
    )
    assert first == 3
    assert second == 0  # re-ingest inserts ZERO rows


# --------------------------------------------------------------------------- #
# ingest_authored_problems — independent commit + profile detect/persist
# --------------------------------------------------------------------------- #


async def test_ingest_detects_and_persists_quantitative_profile(db_session):
    space, subj_id, concept_id = await _seed_subject_concept(db_session, slug="ing3")
    result = await ingest_authored_problems(
        db_session,
        _mixed_records(),
        subject_id=subj_id,
        concept_id=concept_id,
        search_space_id=space,
        commit=False,  # keep the test's outer savepoint intact
    )
    assert result.n_written == 3
    assert result.n_dropped == 2
    assert result.completeness_counts == {"worked": 1, "answer_only": 1, "none": 1}
    # The fluid-flavored set (numeric givens, m/s, kPa) detects quantitative.
    assert result.profile.kind == PROFILE_QUANTITATIVE_SYMBOLIC
    subj = await db_session.get(Subject, subj_id)
    assert subj.profile_kind == PROFILE_QUANTITATIVE_SYMBOLIC


async def test_ingest_detects_polisci_qualitative_profile(db_session):
    space, subj_id, concept_id = await _seed_subject_concept(db_session, slug="ing4")
    polisci = [
        {
            "statement": "Explain why federalism disperses power across levels of government.",
            "solution": "Federalism divides sovereignty so multiple veto points check each other.",
            "worked_procedure": [{"order": 1, "action": "define federalism"}],
            "concept_slug": "federalism",
        },
        {
            "statement": "Assess whether separation of powers is the primary safeguard against tyranny.",
            "solution": "It distributes authority so ambition counteracts ambition, though partisanship erodes it.",
            "concept_slug": "separation_of_powers",
        },
    ]
    result = await ingest_authored_problems(
        db_session, polisci, subject_id=subj_id, concept_id=concept_id,
        search_space_id=space, commit=False,
    )
    assert result.profile.kind == PROFILE_QUALITATIVE_ARGUMENTATIVE
    subj = await db_session.get(Subject, subj_id)
    assert subj.profile_kind == PROFILE_QUALITATIVE_ARGUMENTATIVE
    assert subj.profile_evidence["n_prose"] == 2


async def test_ingest_independent_commit_persists_inventory(db_session):
    """The load-bearing write-then-rollback fix: with commit=True the Tier-1
    inventory is durable BEFORE any downstream stage. We emulate a downstream
    failure by rolling the session back AFTER ingest and asserting the rows
    survive (a commit to the test savepoint outlives a later rollback)."""
    space, subj_id, concept_id = await _seed_subject_concept(db_session, slug="ing5")
    result = await ingest_authored_problems(
        db_session, _mixed_records(), subject_id=subj_id, concept_id=concept_id,
        search_space_id=space, commit=True,
    )
    assert result.n_written == 3
    # Simulate a downstream abort.
    await db_session.rollback()
    # Inventory + profile survived the rollback (committed independently).
    surviving = (
        await db_session.execute(ConceptProblem.__table__.select().where(
            ConceptProblem.concept_id == concept_id
        ))
    ).fetchall()
    assert len(surviving) == 3
    subj = await db_session.get(Subject, subj_id)
    assert subj.profile_kind == PROFILE_QUANTITATIVE_SYMBOLIC
