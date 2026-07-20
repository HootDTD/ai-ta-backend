"""Manual typed construction persistence and orchestration regressions."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import func, select

from apollo.persistence.models import Concept, ConceptProblem, IngestRun, RejectedProblem, Subject
from apollo.provisioning.ingest import ingest_authored_problems, load_authored_problems
from apollo.provisioning.orchestrator import _PerDocumentError, provision_authored_problem
from apollo.provisioning.solution import ReferenceSolutionDraft
from database.models import SearchSpace


def _chat_returning(payload):
    def _chat(*_args, **_kwargs) -> str:
        return payload if isinstance(payload, str) else json.dumps(payload)

    return _chat


def _argument_steps() -> list[dict]:
    return [
        {
            "entry_type": "definition",
            "id": "federalism_meaning",
            "content": {"concept": "federalism", "meaning": "divided sovereignty"},
        },
        {
            "entry_type": "condition",
            "id": "divided_authority",
            "content": {"applies_when": "authority is split across levels"},
        },
        {
            "entry_type": "procedure_step",
            "id": "identify_veto_points",
            "content": {"action": "identify veto points", "purpose": "show checks"},
        },
        {
            "entry_type": "procedure_step",
            "id": "weigh_accountability",
            "content": {
                "action": "weigh checks against blurred responsibility",
                "purpose": "reach the verdict",
            },
        },
    ]


_POLISCI_RECORD = {
    "statement": "Argue whether federalism strengthens democratic accountability.",
    "solution": "Federalism creates veto points that check power and can blur blame.",
    "worked_procedure": [{"order": 1, "text": "define federalism"}],
    "concept_slug": "provisional.inventory",
}


async def _seed_subject(db, *, slug: str) -> tuple[int, int, int]:
    space = SearchSpace(name=f"Course {slug}", slug=slug, subject_name="X")
    db.add(space)
    await db.flush()
    subject = Subject(slug=f"s-{slug}", display_name="Sub", search_space_id=space.id)
    db.add(subject)
    await db.flush()
    provisional = Concept(
        subject_id=subject.id,
        slug="provisional.inventory",
        display_name="Provisional inventory",
    )
    db.add(provisional)
    await db.flush()
    return int(space.id), int(subject.id), int(provisional.id)


async def _ingest_one(db, *, slug: str):
    space_id, subject_id, provisional_id = await _seed_subject(db, slug=slug)
    await ingest_authored_problems(
        db,
        [_POLISCI_RECORD],
        subject_id=subject_id,
        concept_id=provisional_id,
        search_space_id=space_id,
        commit=False,
    )
    authored = load_authored_problems(
        [_POLISCI_RECORD], default_concept_slug="provisional.inventory"
    )[0][0]
    return space_id, provisional_id, authored


def _must_not_run(*_args, **_kwargs):
    raise AssertionError("typed construction must stop before this stage")


async def test_typed_orchestration_stops_for_confirmation(db_session, monkeypatch):
    import apollo.provisioning.orchestrator as orchestrator

    space_id, provisional_id, authored = await _ingest_one(db_session, slug="typed-await")
    monkeypatch.setattr(orchestrator, "validate_pair", _must_not_run)
    monkeypatch.setattr(orchestrator, "tag_and_mint", _must_not_run)

    result = await provision_authored_problem(
        db_session,
        object(),
        authored,
        search_space_id=space_id,
        ingest_concept_id=provisional_id,
        construct_chat_fn=_chat_returning({"steps": _argument_steps()}),
        judge_fn=_must_not_run,
        tag_chat_fn=_must_not_run,
        embed_fn=_must_not_run,
    )

    assert result.outcome == "awaiting_teacher_confirmation"
    assert result.stage == "confirmation"
    row = (
        await db_session.execute(
            select(ConceptProblem).where(ConceptProblem.problem_code == authored.problem_code)
        )
    ).scalar_one()
    assert row.tier == 1
    assert row.concept_id == provisional_id
    confirmation = row.provenance["typed_confirmation"]
    assert confirmation["status"] == "awaiting_teacher_confirmation"
    assert confirmation["confirmed_by"] is None
    assert confirmation["confirmed_at"] is None
    assert confirmation["draft"]["steps"] == row.payload["reference_solution"]
    assert confirmation["draft"]["solution"] == _POLISCI_RECORD["solution"]
    assert {edge["edge_type"] for edge in confirmation["draft"]["edges"]} >= {
        "DEPENDS_ON",
        "PRECEDES",
    }


async def test_typed_fingerprint_replay_does_not_reconstruct_or_reset(db_session):
    space_id, provisional_id, authored = await _ingest_one(db_session, slug="typed-replay")
    calls = 0

    def _construct(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return json.dumps({"steps": _argument_steps()})

    kwargs = {
        "search_space_id": space_id,
        "ingest_concept_id": provisional_id,
        "construct_chat_fn": _construct,
        "judge_fn": _must_not_run,
        "tag_chat_fn": _must_not_run,
        "embed_fn": _must_not_run,
    }
    first = await provision_authored_problem(db_session, object(), authored, **kwargs)
    row = (
        await db_session.execute(
            select(ConceptProblem).where(ConceptProblem.problem_code == authored.problem_code)
        )
    ).scalar_one()
    constructed_at = row.provenance["typed_confirmation"]["constructed_at"]
    second = await provision_authored_problem(db_session, object(), authored, **kwargs)

    assert first.outcome == second.outcome == "awaiting_teacher_confirmation"
    assert calls == 1
    assert row.provenance["typed_confirmation"]["constructed_at"] == constructed_at


async def test_unconstructable_typed_candidate_is_clean_audited_reject(db_session):
    space_id, provisional_id, authored = await _ingest_one(db_session, slug="typed-bad")
    run = IngestRun(search_space_id=space_id, document_id=None, status="running")
    db_session.add(run)
    await db_session.flush()

    result = await provision_authored_problem(
        db_session,
        object(),
        authored,
        search_space_id=space_id,
        ingest_concept_id=provisional_id,
        construct_chat_fn=_chat_returning("not json"),
        judge_fn=_must_not_run,
        tag_chat_fn=_must_not_run,
        embed_fn=_must_not_run,
        run=run,
    )

    assert result.outcome == "rejected"
    assert result.stage == "construct"
    assert "attempt 1:" in result.diagnostic
    assert "attempt 2:" in result.diagnostic
    assert "attempt 3:" in result.diagnostic
    assert (
        await db_session.execute(
            select(func.count())
            .select_from(RejectedProblem)
            .where(RejectedProblem.ingest_run_id == run.id)
        )
    ).scalar_one() == 1


async def test_construct_reject_without_run_writes_no_audit_row(db_session):
    space_id, provisional_id, authored = await _ingest_one(db_session, slug="typed-no-run")
    result = await provision_authored_problem(
        db_session,
        object(),
        authored,
        search_space_id=space_id,
        ingest_concept_id=provisional_id,
        construct_chat_fn=_chat_returning("not json"),
        judge_fn=_must_not_run,
        tag_chat_fn=_must_not_run,
        embed_fn=_must_not_run,
    )
    assert result.outcome == "rejected"
    assert (
        await db_session.execute(
            select(func.count())
            .select_from(RejectedProblem)
            .where(RejectedProblem.search_space_id == space_id)
        )
    ).scalar_one() == 0


async def test_missing_tier1_row_raises_per_document_error(db_session):
    """No Tier-1 row exists for this ``problem_code`` (it was never ingested) —
    a defensive ``_PerDocumentError`` fail-closed, not a silent construct."""
    space_id, _subject_id, provisional_id = await _seed_subject(db_session, slug="typed-missing")
    authored = load_authored_problems(
        [_POLISCI_RECORD], default_concept_slug="provisional.inventory"
    )[0][0]

    with pytest.raises(_PerDocumentError) as exc_info:
        await provision_authored_problem(
            db_session,
            object(),
            authored,
            search_space_id=space_id,
            ingest_concept_id=provisional_id,
            construct_chat_fn=_must_not_run,
            judge_fn=_must_not_run,
            tag_chat_fn=_must_not_run,
            embed_fn=_must_not_run,
        )
    assert exc_info.value.stage == "construct"
    assert exc_info.value.error_class == "MissingTier1Row"


async def test_typed_replay_teacher_confirmed_not_promoted_returns_held(db_session):
    """Replay on a row already stamped ``teacher_confirmed_not_promoted`` (the
    teacher approved, but duplicate/solve-and-check held it) returns
    ``held_for_review`` WITHOUT touching construction again."""
    space_id, provisional_id, authored = await _ingest_one(db_session, slug="typed-held-replay")
    first = await provision_authored_problem(
        db_session,
        object(),
        authored,
        search_space_id=space_id,
        ingest_concept_id=provisional_id,
        construct_chat_fn=_chat_returning({"steps": _argument_steps()}),
        judge_fn=_must_not_run,
        tag_chat_fn=_must_not_run,
        embed_fn=_must_not_run,
    )
    assert first.outcome == "awaiting_teacher_confirmation"

    row = (
        await db_session.execute(
            select(ConceptProblem).where(ConceptProblem.problem_code == authored.problem_code)
        )
    ).scalar_one()
    row.provenance = {
        **row.provenance,
        "typed_confirmation": {
            **row.provenance["typed_confirmation"],
            "status": "teacher_confirmed_not_promoted",
            "diagnostic": "gate 9: refuted",
        },
    }
    await db_session.flush()

    second = await provision_authored_problem(
        db_session,
        object(),
        authored,
        search_space_id=space_id,
        ingest_concept_id=provisional_id,
        construct_chat_fn=_must_not_run,
        judge_fn=_must_not_run,
        tag_chat_fn=_must_not_run,
        embed_fn=_must_not_run,
    )
    assert second.outcome == "held_for_review"
    assert second.stage == "confirmation"
    assert second.diagnostic == "gate 9: refuted"


async def test_typed_replay_promoted_tier2_returns_promoted(db_session):
    """Replay on a row already flipped Tier 2 (teacher-confirmed and promoted)
    returns ``promoted``/``ok`` WITHOUT re-constructing or re-promoting."""
    space_id, provisional_id, authored = await _ingest_one(db_session, slug="typed-tier2-replay")
    first = await provision_authored_problem(
        db_session,
        object(),
        authored,
        search_space_id=space_id,
        ingest_concept_id=provisional_id,
        construct_chat_fn=_chat_returning({"steps": _argument_steps()}),
        judge_fn=_must_not_run,
        tag_chat_fn=_must_not_run,
        embed_fn=_must_not_run,
    )
    assert first.outcome == "awaiting_teacher_confirmation"

    row = (
        await db_session.execute(
            select(ConceptProblem).where(ConceptProblem.problem_code == authored.problem_code)
        )
    ).scalar_one()
    row.tier = 2
    await db_session.flush()

    second = await provision_authored_problem(
        db_session,
        object(),
        authored,
        search_space_id=space_id,
        ingest_concept_id=provisional_id,
        construct_chat_fn=_must_not_run,
        judge_fn=_must_not_run,
        tag_chat_fn=_must_not_run,
        embed_fn=_must_not_run,
    )
    assert second.outcome == "promoted"
    assert second.stage == "ok"
    assert second.problem == row.payload


async def test_construct_success_with_no_constructed_problem_raises_per_document_error(
    db_session, monkeypatch
):
    """Defensive belt-and-suspenders: if ``construct_authored_reference`` ever
    returned successfully without stamping ``constructed_problem`` provenance,
    orchestration fails closed with ``_PerDocumentError`` rather than persisting
    a half-built confirmation row."""
    import apollo.provisioning.orchestrator as orchestrator

    space_id, provisional_id, authored = await _ingest_one(db_session, slug="typed-no-constructed")

    async def _empty_draft(*_args, **_kwargs):
        return ReferenceSolutionDraft(solution_source="authored", reference_solution=[])

    monkeypatch.setattr(orchestrator, "construct_authored_reference", _empty_draft)

    with pytest.raises(_PerDocumentError) as exc_info:
        await provision_authored_problem(
            db_session,
            object(),
            authored,
            search_space_id=space_id,
            ingest_concept_id=provisional_id,
            construct_chat_fn=_must_not_run,
            judge_fn=_must_not_run,
            tag_chat_fn=_must_not_run,
            embed_fn=_must_not_run,
        )
    assert exc_info.value.stage == "construct"
    assert exc_info.value.error_class == "MissingConstructedProblem"
