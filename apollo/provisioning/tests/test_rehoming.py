"""Direct, DB-backed unit tests for ``rehoming.py``'s low-level durable-queue
functions (``enqueue``/``claim``/``complete``/``fail``/``run``), independent of
the confirm/API path exercised end-to-end in ``test_typed_confirmation.py``.

Covers the guard/edge branches Phase C flagged as unexercised at the unit
level: the tier-2 preconditions, the claim-nothing-available and
job-not-found no-ops, ``fail_rehoming_job``'s retry-cap -> terminal
transition, ``enqueue_rehoming``'s open-job reuse-vs-mint branch, and
``run_rehoming``'s two failure exits (missing/never-promoted row, and the row
vanishing between the failed transaction's rollback and the re-fetch).
"""

from __future__ import annotations

import pytest

from apollo.persistence.models import Concept, ConceptProblem, RehomingJob, Subject
from apollo.provisioning.authored_sets import rehoming as rehoming_mod
from apollo.provisioning.authored_sets.rehoming import (
    claim_rehoming_job,
    complete_rehoming_job,
    enqueue_rehoming,
    fail_rehoming_job,
    run_rehoming,
)
from apollo.provisioning.cost_constants import MAX_ATTEMPTS
from database.models import SearchSpace

pytestmark = pytest.mark.asyncio


async def _seed_concept_problem(db, *, slug: str, tier: int = 2) -> tuple[int, int, int]:
    """Seed SearchSpace -> Subject -> Concept -> a ConceptProblem at ``tier``.

    Returns (search_space_id, concept_id, problem_id). Callers that need the
    row to survive an internal ``run_rehoming`` rollback must ``db.commit()``
    afterward themselves.
    """
    space = SearchSpace(name=f"Course {slug}", slug=slug, subject_name="Physics")
    db.add(space)
    await db.flush()
    subject = Subject(slug=f"s-{slug}", display_name="Sub", search_space_id=space.id)
    db.add(subject)
    await db.flush()
    concept = Concept(
        subject_id=subject.id,
        slug=f"concept-{slug}",
        display_name="Concept",
        canonical_symbols={},
        normalization_map={},
    )
    db.add(concept)
    await db.flush()
    row = ConceptProblem(
        concept_id=concept.id,
        problem_code=f"authored.{slug}",
        difficulty="intro",
        payload=_prose_problem_payload(slug),
        tier=tier,
        solution_source="authored",
        provenance={},
        search_space_id=space.id,
    )
    db.add(row)
    await db.flush()
    return int(space.id), int(concept.id), int(row.id)


def _prose_problem_payload(slug: str) -> dict:
    """A schema-valid, equation-free ``Problem`` payload (the specific content
    is irrelevant to these tests; only its schema-validity matters)."""
    return {
        "id": f"authored.{slug}",
        "concept_id": "provisional.inventory",
        "difficulty": "intro",
        "given_values": {},
        "problem_text": "Argue whether federalism strengthens accountability.",
        "target_unknown": "",
        "reference_solution": [
            {
                "id": "federalism_meaning",
                "step": 1,
                "entry_type": "definition",
                "content": {"concept": "federalism", "meaning": "divided sovereignty"},
                "depends_on": [],
            },
        ],
    }


# --------------------------------------------------------------------------- #
# enqueue_rehoming
# --------------------------------------------------------------------------- #


async def test_enqueue_rehoming_requires_tier_2(db_session):
    _space_id, _concept_id, problem_id = await _seed_concept_problem(
        db_session, slug="enqueue-tier1", tier=1
    )
    row = await db_session.get(ConceptProblem, problem_id)
    with pytest.raises(ValueError, match="already promoted Tier-2"):
        await enqueue_rehoming(db_session, row)


async def test_enqueue_rehoming_reuses_open_pending_job(db_session):
    _space_id, concept_id, problem_id = await _seed_concept_problem(
        db_session, slug="enqueue-reuse", tier=2
    )
    row = await db_session.get(ConceptProblem, problem_id)
    first_job_id = await enqueue_rehoming(db_session, row, requested_concept_id=None)

    second_job_id = await enqueue_rehoming(db_session, row, requested_concept_id=concept_id)
    assert second_job_id == first_job_id  # same open (pending) job reused, not re-minted

    job = await db_session.get(RehomingJob, first_job_id)
    assert job.requested_concept_id == concept_id  # reuse path updates the request in place

    # Exactly one durable row exists for this problem (no duplicate minted).
    from sqlalchemy import select

    rows = (
        (
            await db_session.execute(
                select(RehomingJob).where(RehomingJob.concept_problem_id == problem_id)
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1


async def test_enqueue_rehoming_mints_fresh_job_once_prior_is_terminal(db_session):
    _space_id, _concept_id, problem_id = await _seed_concept_problem(
        db_session, slug="enqueue-mint", tier=2
    )
    row = await db_session.get(ConceptProblem, problem_id)
    first_job_id = await enqueue_rehoming(db_session, row)

    first_job = await db_session.get(RehomingJob, first_job_id)
    first_job.state = "completed"
    await db_session.flush()

    second_job_id = await enqueue_rehoming(db_session, row)
    assert second_job_id != first_job_id  # prior job is terminal -> a fresh one is minted


# --------------------------------------------------------------------------- #
# claim_rehoming_job / complete_rehoming_job / fail_rehoming_job
# --------------------------------------------------------------------------- #


async def test_claim_rehoming_job_returns_none_when_nothing_claimable(db_session):
    claimed = await claim_rehoming_job(db_session, lease_owner="worker-1", lease_seconds=60)
    assert claimed is None


async def test_complete_rehoming_job_is_a_noop_when_job_missing(db_session):
    # Must not raise even though no such job exists.
    await complete_rehoming_job(db_session, job_id=999999)


async def test_fail_rehoming_job_returns_failed_sentinel_when_job_missing(db_session):
    outcome = await fail_rehoming_job(db_session, job_id=999999, error="boom")
    assert outcome == "failed"


async def test_fail_rehoming_job_retry_cap_transitions_to_terminal(db_session):
    _space_id, _concept_id, problem_id = await _seed_concept_problem(
        db_session, slug="fail-cap", tier=2
    )
    row = await db_session.get(ConceptProblem, problem_id)
    job_id = await enqueue_rehoming(db_session, row)
    job = await db_session.get(RehomingJob, job_id)
    job.attempt_count = MAX_ATTEMPTS  # already at the cap
    await db_session.flush()

    outcome = await fail_rehoming_job(db_session, job_id=job_id, error="tag mint exploded")
    assert outcome == "failed"
    terminal = await db_session.get(RehomingJob, job_id)
    assert terminal.state == "failed"
    refreshed = await db_session.get(ConceptProblem, problem_id)
    state = refreshed.provenance["typed_rehoming"]
    assert state["job_state"] == "failed"
    assert state["retryable"] is False
    assert "tag mint exploded" in state["diagnostic"]


async def test_fail_rehoming_job_below_cap_stays_retryable(db_session):
    _space_id, _concept_id, problem_id = await _seed_concept_problem(
        db_session, slug="fail-retry", tier=2
    )
    row = await db_session.get(ConceptProblem, problem_id)
    job_id = await enqueue_rehoming(db_session, row)
    job = await db_session.get(RehomingJob, job_id)
    job.attempt_count = MAX_ATTEMPTS - 1  # one below the cap
    await db_session.flush()

    outcome = await fail_rehoming_job(db_session, job_id=job_id, error="transient")
    assert outcome == "pending"
    released = await db_session.get(RehomingJob, job_id)
    assert released.state == "pending"
    refreshed = await db_session.get(ConceptProblem, problem_id)
    assert refreshed.provenance["typed_rehoming"]["retryable"] is True


# --------------------------------------------------------------------------- #
# run_rehoming
# --------------------------------------------------------------------------- #


async def test_run_rehoming_returns_false_when_problem_missing(db_session):
    ok = await run_rehoming(
        db_session,
        object(),
        problem_id=999999,
        chat_fn=lambda **_kwargs: "",
        embed_fn=lambda _text: [0.0],
    )
    assert ok is False


async def test_run_rehoming_requires_tier_2_and_records_failure(db_session):
    _space_id, _concept_id, problem_id = await _seed_concept_problem(
        db_session, slug="run-tier1", tier=1
    )
    await db_session.commit()  # durable, so it survives run_rehoming's internal rollback

    ok = await run_rehoming(
        db_session,
        object(),
        problem_id=problem_id,
        chat_fn=lambda **_kwargs: "",
        embed_fn=lambda _text: [0.0],
    )
    assert ok is False
    refreshed = await db_session.get(ConceptProblem, problem_id)
    assert refreshed.tier == 1  # never promoted by re-homing
    state = refreshed.provenance["typed_rehoming"]
    assert state["status"] == "rehoming_failed"
    assert "already promoted Tier-2" in state["diagnostic"]


async def test_run_rehoming_except_branch_returns_false_when_row_vanishes(db_session, monkeypatch):
    """When the failing transaction rolls back and undoes the very row being
    re-homed (the concurrent-delete race this code defends against), the
    except branch's own re-fetch finds nothing and must return False rather
    than raise. Modeled here by leaving the ``ConceptProblem`` insert
    UNCOMMITTED: ``run_rehoming``'s internal ``db.rollback()`` on failure then
    undoes that insert, so the second ``db.get`` genuinely returns None."""
    _space_id, _concept_id, problem_id = await _seed_concept_problem(
        db_session, slug="run-vanish", tier=2
    )
    # Deliberately NOT committed: only flushed, so it lives in the same
    # savepoint that run_rehoming's own rollback will unwind.

    async def _boom(*_args, **_kwargs):
        raise RuntimeError("tag_and_mint exploded")

    monkeypatch.setattr(rehoming_mod, "tag_and_mint", _boom)

    ok = await run_rehoming(
        db_session,
        object(),
        problem_id=problem_id,
        chat_fn=lambda **_kwargs: "",
        embed_fn=lambda _text: [0.0],
    )
    assert ok is False
    assert await db_session.get(ConceptProblem, problem_id) is None
