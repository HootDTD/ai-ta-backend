from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from auth import AuthContext


class _BG:
    def __init__(self):
        self.tasks = []

    def add_task(self, fn, *args, **kwargs):
        self.tasks.append((fn, args, kwargs))


class _FakeRequest:
    pass


async def _fake_require_user(_request):
    return AuthContext(user_id="teacher-1", access_token="token")


async def _fake_require_teacher(**_kwargs):
    return None


async def _seed_course(db, *, slug: str) -> tuple[int, int]:
    from apollo.persistence.models import Concept
    from database.models import Course

    space = Course(name=f"Course {slug}", slug=slug, subject_name="Physics")
    db.add(space)
    await db.flush()
    subject = SimpleNamespace(slug=f"subject-{slug}", display_name="Physics", search_space_id=space.id)
    concept = Concept(
        course_id=subject.search_space_id, subject_slug=subject.slug, subject_display_name=subject.display_name,
        slug=f"concept-{slug}",
        display_name="Concept",
        canonical_symbols=[],
        normalization_map={},
    )
    db.add(concept)
    await db.flush()
    return int(space.id), int(concept.id)


def _draft(*, secret: str | None = None) -> dict:
    return {
        "solution_source": "generated",
        "reference_solution": [
            {
                "step": 1,
                "entry_type": "equation",
                "id": "moment_balance",
                "content": {"symbolic": "M"},
                "depends_on": [],
            }
        ],
        "grounding": [{"text": secret or "private grounding"}],
        "provenance": {"internal": secret or "private provenance"},
    }


async def _seed_problem(
    db,
    *,
    space_id: int,
    concept_id: int,
    generated: bool,
    required: bool = True,
    problem_text: str = "Find M.",
):
    from apollo.persistence.models import Problem as ProblemRecord

    row = ProblemRecord.from_inventory_payload(
        {
            "id": f"problem-{concept_id}-{generated}-{required}-{len(problem_text)}",
            "concept_id": "known_concept_slug",
            "difficulty": "intro",
            "problem_text": problem_text,
            "given_values": {},
            "target_unknown": "M",
        },
        course_id=space_id,
        concept_id=concept_id,
        tier=1 if generated else 2,
        solution_source="generated" if generated else "authored",
        provenance=(
            {
                "source": "generated",
                "aig_seed_id": 17,
                "variation_operator": "context_reskin",
                "model": "gpt-4o",
                "round_trip": {"verdict": "verified", "diagnostic": "symbolically equal"},
                "qualitative_rubric": {"ceiling": "faithfulness_only"},
                "authored_review": {
                    "required": required,
                    "reason": "generated_variant",
                    "ocr_draft": _draft(secret="do-not-leak"),
                },
            }
            if generated
            else {}
        ),
    )
    db.add(row)
    await db.flush()
    return row


@pytest.mark.asyncio
async def test_post_flag_off_403_but_get_list_still_serves(db_session, monkeypatch):
    from fastapi import HTTPException

    import apollo.provisioning.problem_generation.api as gapi

    space_id, concept_id = await _seed_course(db_session, slug="gen4-flag")
    monkeypatch.delenv("APOLLO_PROBLEM_GENERATION", raising=False)
    with pytest.raises(HTTPException) as exc:
        await gapi.create_generation_run(
            concept_id=concept_id,
            body=gapi.GenerateVariantsBody(seed_problem_ids=[1], count=1),
            request=_FakeRequest(),
            background=_BG(),
            db=db_session,
        )
    assert exc.value.status_code == 403
    assert exc.value.detail == "problem generation is disabled"

    monkeypatch.setattr(gapi, "require_user", _fake_require_user)
    monkeypatch.setattr(gapi, "require_course_teacher", _fake_require_teacher)
    assert await gapi.list_generation_runs(
        request=_FakeRequest(), search_space_id=space_id, db=db_session
    ) == {"runs": []}


@pytest.mark.asyncio
async def test_post_persists_pending_run_and_schedules_one_task(db_session, monkeypatch):
    import apollo.provisioning.problem_generation.api as gapi
    from apollo.persistence.models import ProvisioningRun

    space_id, concept_id = await _seed_course(db_session, slug="gen4-create")
    monkeypatch.setenv("APOLLO_PROBLEM_GENERATION", "1")
    monkeypatch.setattr(gapi, "require_user", _fake_require_user)
    monkeypatch.setattr(gapi, "require_course_teacher", _fake_require_teacher)
    bg = _BG()

    response = await gapi.create_generation_run(
        concept_id=concept_id,
        body=gapi.GenerateVariantsBody(seed_problem_ids=[7, 8], count=3),
        request=_FakeRequest(),
        background=bg,
        db=db_session,
    )

    assert response == {"run_id": response["run_id"], "status": "pending"}
    row = await db_session.get(ProvisioningRun, response["run_id"])
    assert (row.search_space_id, row.concept_id, row.status) == (
        space_id,
        concept_id,
        "pending",
    )
    assert len(bg.tasks) == 1
    fn, args, kwargs = bg.tasks[0]
    assert fn is gapi._run_generation_background
    assert args == (response["run_id"], concept_id, space_id, [7, 8], 3)
    assert kwargs == {}


@pytest.mark.asyncio
async def test_post_bad_concept_404_and_teacher_403_bubbles(db_session, monkeypatch):
    from fastapi import HTTPException

    import apollo.provisioning.problem_generation.api as gapi

    _space_id, concept_id = await _seed_course(db_session, slug="gen4-auth")
    monkeypatch.setenv("APOLLO_PROBLEM_GENERATION", "1")
    monkeypatch.setattr(gapi, "require_user", _fake_require_user)
    with pytest.raises(HTTPException) as missing:
        await gapi.create_generation_run(
            concept_id=999999,
            body=gapi.GenerateVariantsBody(seed_problem_ids=[1], count=1),
            request=_FakeRequest(),
            background=_BG(),
            db=db_session,
        )
    assert missing.value.status_code == 404

    async def _deny(**_kwargs):
        raise HTTPException(status_code=403, detail="teacher required")

    monkeypatch.setattr(gapi, "require_course_teacher", _deny)
    with pytest.raises(HTTPException) as denied:
        await gapi.create_generation_run(
            concept_id=concept_id,
            body=gapi.GenerateVariantsBody(seed_problem_ids=[1], count=1),
            request=_FakeRequest(),
            background=_BG(),
            db=db_session,
        )
    assert denied.value.status_code == 403


@pytest.mark.asyncio
async def test_background_succeeds_serializes_records_and_stamps_ingest(db_session, monkeypatch):
    import apollo.provisioning.problem_generation.api as gapi
    from apollo.persistence.models import IngestRun, ProvisioningRun
    from apollo.provisioning.problem_generation.generator import (
        GenerationRecord,
        GenerationRunResult,
    )

    space_id, concept_id = await _seed_course(db_session, slug="gen4-bg-ok")
    run = ProvisioningRun.generation(search_space_id=space_id, concept_id=concept_id)
    db_session.add(run)
    await db_session.flush()

    @asynccontextmanager
    async def _session_cm():
        yield db_session

    async def _generate(*_args, **_kwargs):
        return GenerationRunResult(
            requested=2,
            written=[101],
            dropped={"duplicate": 1},
            records=[
                GenerationRecord(
                    seed_id=7,
                    operator="context_reskin",
                    outcome="duplicate",
                    reasons=("same normalized text",),
                )
            ],
        )

    monkeypatch.setattr(gapi, "get_async_session", _session_cm)
    monkeypatch.setattr(gapi, "generate_problem_variants", _generate)
    monkeypatch.setattr(gapi, "MeteredChat", lambda **kwargs: kwargs)
    await gapi._run_generation_background(int(run.id), concept_id, space_id, [7], 2)

    refreshed = await db_session.get(ProvisioningRun, int(run.id))
    assert refreshed.status == "succeeded"
    assert refreshed.result_summary == {
        "requested": 2,
        "written": [101],
        "dropped": {"duplicate": 1},
        "records": [
            {
                "seed_id": 7,
                "operator": "context_reskin",
                "outcome": "duplicate",
                "reasons": ["same normalized text"],
                "concept_problem_id": None,
            }
        ],
    }
    assert refreshed.ingest_run_id is not None
    ingest = await db_session.get(IngestRun, int(refreshed.ingest_run_id))
    assert ingest.status == "succeeded"


@pytest.mark.asyncio
async def test_background_exception_is_swallowed_and_persisted(db_session, monkeypatch):
    import apollo.provisioning.problem_generation.api as gapi
    from apollo.persistence.models import IngestRun, ProvisioningRun

    space_id, concept_id = await _seed_course(db_session, slug="gen4-bg-fail")
    run = ProvisioningRun.generation(search_space_id=space_id, concept_id=concept_id)
    db_session.add(run)
    await db_session.flush()

    @asynccontextmanager
    async def _session_cm():
        yield db_session

    async def _explode(*_args, **_kwargs):
        raise RuntimeError("generation boom")

    monkeypatch.setattr(gapi, "get_async_session", _session_cm)
    monkeypatch.setattr(gapi, "generate_problem_variants", _explode)
    monkeypatch.setattr(gapi, "MeteredChat", lambda **kwargs: kwargs)
    await gapi._run_generation_background(int(run.id), concept_id, space_id, [7], 1)

    refreshed = await db_session.get(ProvisioningRun, int(run.id))
    assert refreshed.status == "failed"
    assert refreshed.result_summary["error"] == "generation boom"
    ingest = await db_session.get(IngestRun, int(refreshed.ingest_run_id))
    assert ingest.status == "failed"


@pytest.mark.asyncio
async def test_background_recovery_failure_is_also_swallowed(monkeypatch):
    import apollo.provisioning.problem_generation.api as gapi

    @asynccontextmanager
    async def _unavailable_session():
        raise RuntimeError("database unavailable")
        yield  # pragma: no cover - makes this an async context manager

    monkeypatch.setattr(gapi, "get_async_session", _unavailable_session)

    await gapi._run_generation_background(1, 2, 3, [4], 1)


@pytest.mark.asyncio
async def test_get_detail_projects_review_without_provenance_leak(db_session, monkeypatch):
    import apollo.provisioning.problem_generation.api as gapi
    from apollo.persistence.models import IngestRun, ProvisioningRun

    space_id, concept_id = await _seed_course(db_session, slug="gen4-detail")
    problem = await _seed_problem(
        db_session,
        space_id=space_id,
        concept_id=concept_id,
        generated=True,
        problem_text="x" * 2100,
    )
    ingest = IngestRun(
        search_space_id=space_id,
        document_id=None,
        status="succeeded",
        llm_calls=3,
        llm_tokens_in=120,
        llm_tokens_out=45,
        llm_cost_usd="0.012345",
    )
    db_session.add(ingest)
    await db_session.flush()
    run = ProvisioningRun.generation(
        search_space_id=space_id,
        concept_id=concept_id,
        status="succeeded",
        ingest_run_id=int(ingest.id),
        result_summary={"requested": 1, "written": [int(problem.id)], "dropped": {}},
    )
    db_session.add(run)
    await db_session.flush()
    monkeypatch.setattr(gapi, "require_user", _fake_require_user)
    monkeypatch.setattr(gapi, "require_course_teacher", _fake_require_teacher)

    detail = await gapi.get_generation_run(
        run_id=int(run.id), request=_FakeRequest(), db=db_session
    )

    projected = detail["problems"][0]
    assert detail["ingest_run"] == {
        "llm_calls": 3,
        "llm_tokens_in": 120,
        "llm_tokens_out": 45,
        "llm_cost_usd": "0.012345",
    }
    assert len(projected["problem_text"]) == 2000
    assert projected["problem_text_truncated"] is True
    assert projected["review"]["variation_operator"] == "context_reskin"
    assert projected["review"]["round_trip"] == {
        "verdict": "verified",
        "diagnostic": "symbolically equal",
    }
    assert projected["review"]["authored_review"] == {"required": True}
    assert projected["review"]["ocr_draft"] == {
        "solution_source": "generated",
        "reference_solution": _draft()["reference_solution"],
    }
    assert "grounding" not in projected["review"]["ocr_draft"]
    assert "provenance" not in projected["review"]["ocr_draft"]
    assert "do-not-leak" not in str(projected)

    listed = await gapi.list_generation_runs(
        request=_FakeRequest(), search_space_id=space_id, db=db_session
    )
    assert listed["runs"] == [
        {
            "run_id": int(run.id),
            "concept_id": concept_id,
            "status": "succeeded",
            "created_at": run.created_at.isoformat(),
            "requested": 1,
            "written_count": 1,
            "dropped": {},
        }
    ]


@pytest.mark.asyncio
async def test_get_detail_caps_by_default_and_full_text_skips_cap(db_session, monkeypatch):
    import apollo.provisioning.problem_generation.api as gapi
    from apollo.persistence.models import ProvisioningRun

    space_id, concept_id = await _seed_course(db_session, slug="gen4-detail-full-text")
    long_text = "full question " * (gapi._PROBLEM_TEXT_CAP // 4)
    problem = await _seed_problem(
        db_session,
        space_id=space_id,
        concept_id=concept_id,
        generated=True,
        problem_text=long_text,
    )
    run = ProvisioningRun.generation(
        search_space_id=space_id,
        concept_id=concept_id,
        status="succeeded",
        result_summary={"requested": 1, "written": [int(problem.id)], "dropped": {}},
    )
    db_session.add(run)
    await db_session.flush()
    monkeypatch.setattr(gapi, "require_user", _fake_require_user)
    monkeypatch.setattr(gapi, "require_course_teacher", _fake_require_teacher)

    capped_detail = await gapi.get_generation_run(
        run_id=int(run.id), request=_FakeRequest(), db=db_session
    )
    full_detail = await gapi.get_generation_run(
        run_id=int(run.id),
        request=_FakeRequest(),
        full_text=True,
        db=db_session,
    )

    capped_problem = capped_detail["problems"][0]
    assert capped_problem["problem_text"] == long_text[: gapi._PROBLEM_TEXT_CAP]
    assert capped_problem["problem_text_truncated"] is True
    full_problem = full_detail["problems"][0]
    assert full_problem["problem_text"] == long_text
    assert full_problem["problem_text_truncated"] is False


def test_review_projection_omits_optional_rubric_and_trims_invalid_draft():
    from types import SimpleNamespace

    import apollo.provisioning.problem_generation.api as gapi

    review = gapi._generation_review(
        SimpleNamespace(
            provenance={
                "source": "generated",
                "authored_review": {"required": True, "ocr_draft": "not-a-dict"},
            }
        )
    )
    assert "qualitative_rubric" not in review
    assert review["round_trip"] is None
    assert review["ocr_draft"] is None


@pytest.mark.asyncio
async def test_approve_generated_problem_promotes_with_known_concept(db_session, monkeypatch):
    import apollo.provisioning.authored_sets.api as aapi
    import apollo.provisioning.problem_generation.api as gapi
    from apollo.persistence.models import Problem as ProblemRecord
    from apollo.provisioning.promote import PromoteResult
    from apollo.provisioning.tag_mint import MintPlan

    space_id, concept_id = await _seed_course(db_session, slug="gen4-approve")
    problem = await _seed_problem(
        db_session, space_id=space_id, concept_id=concept_id, generated=True
    )
    monkeypatch.setattr(gapi, "require_user", _fake_require_user)
    monkeypatch.setattr(gapi, "require_course_teacher", _fake_require_teacher)
    captured = {}

    async def _tag_and_mint(db, pair, *, chat_fn, embed_fn, resolved_concept=None):
        captured["resolved"] = resolved_concept
        return MintPlan(
            concept_id=concept_id,
            concept_slug=resolved_concept.slug,
            authored_symbols=[],
            minted_entity_ids={},
            merged_entity_keys=[],
            prereq_pairs=[],
            misconception_keys=[],
        )

    async def _promote(db, neo, **kwargs):
        row = await db.get(ProblemRecord, kwargs["concept_problem_id"])
        row.tier = 2
        return PromoteResult(promoted=True)

    async def _dup_hashes(*_args, **_kwargs):
        return set()

    monkeypatch.setattr(aapi, "tag_and_mint", _tag_and_mint)
    monkeypatch.setattr(aapi, "promote", _promote)
    monkeypatch.setattr(aapi, "_authored_concept_dup_hashes", _dup_hashes)
    monkeypatch.setattr(aapi, "get_neo4j_client", lambda: "neo")
    monkeypatch.setattr(aapi, "_make_metered_chat", lambda **_kwargs: object())

    response = await gapi.approve_generated_problem(
        problem_id=int(problem.id),
        body=aapi.ApproveBody(reference="ocr"),
        request=_FakeRequest(),
        db=db_session,
    )

    assert response["promoted"] is True
    assert captured["resolved"] == aapi.ResolvedConcept(
        concept_id=concept_id, slug="concept-gen4-approve"
    )
    refreshed = await db_session.get(ProblemRecord, int(problem.id))
    assert refreshed.tier == 2
    assert refreshed.provenance["authored_review"]["required"] is False


@pytest.mark.asyncio
async def test_approve_generated_problem_404_and_conflict_cases(db_session, monkeypatch):
    from fastapi import HTTPException

    import apollo.provisioning.authored_sets.api as aapi
    import apollo.provisioning.problem_generation.api as gapi

    space_id, concept_id = await _seed_course(db_session, slug="gen4-approve-errors")
    authored = await _seed_problem(
        db_session, space_id=space_id, concept_id=concept_id, generated=False
    )
    approved = await _seed_problem(
        db_session,
        space_id=space_id,
        concept_id=concept_id,
        generated=True,
        required=False,
        problem_text="approved",
    )
    held = await _seed_problem(
        db_session,
        space_id=space_id,
        concept_id=concept_id,
        generated=True,
        problem_text="held",
    )
    monkeypatch.setattr(gapi, "require_user", _fake_require_user)
    monkeypatch.setattr(gapi, "require_course_teacher", _fake_require_teacher)

    with pytest.raises(HTTPException) as not_generated:
        await gapi.approve_generated_problem(
            problem_id=int(authored.id),
            body=aapi.ApproveBody(),
            request=_FakeRequest(),
            db=db_session,
        )
    assert not_generated.value.status_code == 404

    with pytest.raises(HTTPException) as already_approved:
        await gapi.approve_generated_problem(
            problem_id=int(approved.id),
            body=aapi.ApproveBody(),
            request=_FakeRequest(),
            db=db_session,
        )
    assert already_approved.value.status_code == 409

    with pytest.raises(HTTPException) as missing_alt:
        await gapi.approve_generated_problem(
            problem_id=int(held.id),
            body=aapi.ApproveBody(reference="generated"),
            request=_FakeRequest(),
            db=db_session,
        )
    assert missing_alt.value.status_code == 409


@pytest.mark.asyncio
async def test_generation_run_defaults_and_jsonb_round_trip(db_session):
    from apollo.persistence.models import ProvisioningRun

    space_id, concept_id = await _seed_course(db_session, slug="gen4-model")
    run = ProvisioningRun.generation(search_space_id=space_id, concept_id=concept_id)
    db_session.add(run)
    await db_session.flush()
    await db_session.refresh(run)
    assert run.status == "pending"
    assert run.result_summary == {}

    run.result_summary = {"requested": 2, "written": [1], "dropped": {"duplicate": 1}}
    await db_session.flush()
    await db_session.refresh(run)
    assert run.result_summary["dropped"] == {"duplicate": 1}


@pytest.mark.asyncio
async def test_seeds_lists_teachable_problems_only(db_session, monkeypatch):
    """Tier-2 non-quarantined rows only, ordered by id, text capped — and the
    route serves with the generation flag unset (deliberately not gated)."""
    from datetime import UTC, datetime

    import apollo.provisioning.problem_generation.api as gapi
    from apollo.persistence.models import Problem as ProblemRecord

    space_id, concept_id = await _seed_course(db_session, slug="gen4-seeds")
    monkeypatch.setattr(gapi, "require_user", _fake_require_user)
    monkeypatch.setattr(gapi, "require_course_teacher", _fake_require_teacher)
    monkeypatch.delenv("APOLLO_PROBLEM_GENERATION", raising=False)

    first = await _seed_problem(
        db_session, space_id=space_id, concept_id=concept_id, generated=False
    )
    long_text = "x" * 2500
    second = await _seed_problem(
        db_session,
        space_id=space_id,
        concept_id=concept_id,
        generated=False,
        problem_text=long_text,
    )
    # Excluded: tier-1 (generated inventory) and quarantined tier-2.
    await _seed_problem(db_session, space_id=space_id, concept_id=concept_id, generated=True)
    quarantined = await _seed_problem(
        db_session,
        space_id=space_id,
        concept_id=concept_id,
        generated=False,
        problem_text="quarantined",
    )
    quarantined.quarantined_at = datetime.now(UTC)
    await db_session.flush()

    resp = await gapi.list_generation_seeds(
        concept_id=concept_id, request=_FakeRequest(), db=db_session
    )
    seeds = resp["seeds"]
    assert [s["concept_problem_id"] for s in seeds] == [int(first.id), int(second.id)]
    assert seeds[0]["problem_text"] == "Find M."
    assert seeds[0]["difficulty"] == "intro"
    assert len(seeds[1]["problem_text"]) == 2000

    # Quarantined row exists but is excluded (guard the fixture, not the dust).
    assert await db_session.get(ProblemRecord, int(quarantined.id)) is not None


@pytest.mark.asyncio
async def test_seeds_unknown_concept_404(db_session, monkeypatch):
    from fastapi import HTTPException

    import apollo.provisioning.problem_generation.api as gapi

    monkeypatch.setattr(gapi, "require_user", _fake_require_user)
    monkeypatch.setattr(gapi, "require_course_teacher", _fake_require_teacher)
    with pytest.raises(HTTPException) as missing:
        await gapi.list_generation_seeds(concept_id=999_999, request=_FakeRequest(), db=db_session)
    assert missing.value.status_code == 404
