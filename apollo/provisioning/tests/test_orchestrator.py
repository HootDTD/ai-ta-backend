"""WU-3B2g Step 3 — orchestrator stage-sequencing + error-mapping unit tests.

``run_provisioning`` drives the six per-document stages and OWNS the
``apollo_ingest_runs`` lifecycle + the §4b stage-outcome -> observability decision
(``apollo_rejected_problems`` for a per-candidate reject, ``apollo_ingest_errors``
+ a failed run for a per-document error). The stages themselves are FROZEN and
mocked here at the orchestrator module surface; ``metered_chat``/``retrieve_fn``/
``embed_fn``/``neo`` are deterministic stubs. NO network, NO LLM, NO Neo4j.

The savepoint ``db_session`` is real pgvector. Tests Docker-skip cleanly but the
WU-3B2g gate requires GREEN-not-skipped.
"""

from __future__ import annotations

import sys
from unittest.mock import AsyncMock

import pytest

from apollo.persistence.models import (
    Concept,
    ConceptProblem,
    IngestError,
    IngestRun,
    ProvisioningJob,
    RejectedProblem,
    Subject,
)
from apollo.provisioning import run_provisioning
from apollo.provisioning.metered_chat import CostBudgetExceeded
from apollo.provisioning.orchestrator import ProvisioningOutcome
from apollo.provisioning.pairing_gate import PairingVerdict, Rejection
from apollo.provisioning.promote import PromoteResult
from apollo.provisioning.queue import ClaimedJob
from apollo.provisioning.scrape import CandidateQuestion, ScrapeResult
from apollo.provisioning.solution import ReferenceSolutionDraft
from apollo.provisioning.tag_mint import MintPlan
from database.models import AITAChunk, AITADocument, SearchSpace

orch = sys.modules["apollo.provisioning.orchestrator"]

# pytest.ini sets asyncio_mode = auto.


# --------------------------------------------------------------------------- #
# Fake metered chat — deterministic, raising CostBudgetExceeded on demand.
# --------------------------------------------------------------------------- #
class _FakeMeteredChat:
    def __init__(self, *, raise_on_scrape: bool = False) -> None:
        self._raise_on_scrape = raise_on_scrape

    def scrape_chat_fn(self, system_prompt):  # noqa: ANN001
        def _fn(_chunk_content):  # noqa: ANN001
            if self._raise_on_scrape:
                raise CostBudgetExceeded(tokens=10, ceiling=5, document_id=1)
            return "[]"

        return _fn

    def cheap(self, **_k):
        return "{}"

    def main(self, **_k):
        return "{}"


# --------------------------------------------------------------------------- #
# Seed helpers
# --------------------------------------------------------------------------- #
async def _seed(db, *, slug: str, document_id: int = 1, n_chunks: int = 1):
    space = SearchSpace(name=f"Course {slug}", slug=slug, subject_name="Physics")
    db.add(space)
    await db.flush()
    doc = AITADocument(
        id=document_id,
        title="Doc",
        content="x",
        content_hash=f"hash-{slug}-{document_id}",
        search_space_id=space.id,
    )
    db.add(doc)
    await db.flush()
    for i in range(n_chunks):
        db.add(
            AITAChunk(
                document_id=document_id,
                content=f"chunk {i} content",
                page_number=i + 1,
            )
        )
    run = IngestRun(
        search_space_id=space.id,
        document_id=document_id,
        content_hash=f"hash-{slug}-{document_id}",
        status="queued",
    )
    db.add(run)
    await db.flush()
    job = ProvisioningJob(
        search_space_id=space.id,
        document_id=document_id,
        state="running",
        ingest_run_id=run.id,
        attempt_count=1,
    )
    db.add(job)
    await db.flush()
    claimed = ClaimedJob(
        job_id=job.id,
        search_space_id=space.id,
        document_id=document_id,
        ingest_run_id=run.id,
        attempt_count=1,
    )
    return space.id, run.id, claimed


def _candidate(*, document_id: int = 1, chash: str = "c1") -> CandidateQuestion:
    return CandidateQuestion(
        problem_text="Find P2.",
        given_values={"P1": 1.0},
        target_unknown="P2",
        difficulty="intro",
        document_id=document_id,
        page=1,
        chunk_content_hash=chash,
        concept_slug="provisional.inventory",
    )


def _draft() -> ReferenceSolutionDraft:
    return ReferenceSolutionDraft(
        solution_source="generated",
        reference_solution=[{"id": "s1", "entry_type": "equation", "content": {}}],
        grounding=(),
        provenance={},
    )


def _mint_plan(concept_id: int) -> MintPlan:
    return MintPlan(
        concept_id=concept_id,
        concept_slug="c",
        authored_symbols=["P"],
        minted_entity_ids={},
        merged_entity_keys=[],
        prereq_pairs=[],
        misconception_keys=[],
    )


def _patch_stages(
    monkeypatch,
    *,
    scrape_candidates=(),
    find_or_generate=None,
    validate_pair=None,
    promote=None,
    tag_and_mint=None,
    concept_id: int,
):
    """Patch the frozen stage callables on the orchestrator module surface."""
    async def _scrape(chunks, *, chat_fn):  # noqa: ANN001
        # exercise the injected chat_fn so a cost-abort scrape can raise.
        for ch in chunks:
            chat_fn(ch.content)
        return ScrapeResult(
            candidates=tuple(scrape_candidates),
            scraped_count=1 if scrape_candidates else 0,
            parse_failures=0,
        )

    async def _write_tier1(db, candidates, *, concept_id, search_space_id):  # noqa: ANN001
        return len(candidates)

    async def _resolve_prov(db, *, search_space_id):  # noqa: ANN001
        return concept_id

    monkeypatch.setattr(orch, "scrape_questions", _scrape)
    monkeypatch.setattr(orch, "write_tier1_problems", _write_tier1)
    monkeypatch.setattr(orch, "resolve_or_create_provisional_concept", _resolve_prov)

    if find_or_generate is not None:
        monkeypatch.setattr(orch, "find_or_generate", find_or_generate)
    if validate_pair is not None:
        monkeypatch.setattr(orch, "validate_pair", validate_pair)
    if tag_and_mint is not None:
        monkeypatch.setattr(orch, "tag_and_mint", tag_and_mint)
    if promote is not None:
        monkeypatch.setattr(orch, "promote", promote)


async def _seed_tier1_row(db, *, concept_id, chash, search_space_id):
    """A Tier-1 ConceptProblem the orchestrator promotes."""
    row = ConceptProblem(
        concept_id=concept_id,
        problem_code=f"scrape.{chash}",
        difficulty="intro",
        payload={"id": f"scrape.{chash}"},
        tier=1,
        solution_source=None,
        provenance={"chunk_content_hash": chash},
        search_space_id=search_space_id,
    )
    db.add(row)
    await db.flush()
    return row.id


async def _seed_concept(db, *, search_space_id, slug="c"):
    subj = Subject(slug=f"s-{slug}", display_name="S", search_space_id=search_space_id)
    db.add(subj)
    await db.flush()
    c = Concept(
        subject_id=subj.id,
        slug=slug,
        display_name="C",
        canonical_symbols={"symbols": ["P"]},
        normalization_map={},
    )
    db.add(c)
    await db.flush()
    return c.id


async def _run(db, claimed, *, metered=None):
    return await run_provisioning(
        db,
        AsyncMock(),
        job=claimed,
        metered_chat=metered or _FakeMeteredChat(),
        retrieve_fn=AsyncMock(return_value=()),
        embed_fn=lambda _t: [0.0],
    )


# --------------------------------------------------------------------------- #
# T-OR1 — happy path promotes (through the package surface)
# --------------------------------------------------------------------------- #
async def test_run_provisioning_happy_path_promotes(db_session, monkeypatch):
    space, run_id, claimed = await _seed(db_session, slug="or1")
    concept_id = await _seed_concept(db_session, search_space_id=space)
    chash = "c1"
    pid = await _seed_tier1_row(
        db_session, concept_id=concept_id, chash=chash, search_space_id=space
    )

    async def _fog(db, q, *, retrieve_fn, chat_fn):  # noqa: ANN001
        return _draft()

    async def _vp(q, draft, *, retrieve_fn, judge_fn):  # noqa: ANN001
        return PairingVerdict(paired=True, faithful=True, confidence=1.0)

    async def _tm(db, pair, *, chat_fn, embed_fn):  # noqa: ANN001
        return _mint_plan(concept_id)

    async def _promote(db, neo, **kwargs):  # noqa: ANN001
        row = await db.get(ConceptProblem, kwargs["concept_problem_id"])
        row.tier = 2
        await db.flush()
        return PromoteResult(promoted=True)

    _patch_stages(
        monkeypatch,
        scrape_candidates=(_candidate(chash=chash),),
        find_or_generate=_fog,
        validate_pair=_vp,
        tag_and_mint=_tm,
        promote=_promote,
        concept_id=concept_id,
    )

    outcome = await _run(db_session, claimed)

    assert isinstance(outcome, ProvisioningOutcome)
    assert outcome.status == "succeeded"
    assert outcome.n_questions_scraped >= 1
    assert outcome.n_promoted == 1
    assert outcome.n_rejected == 0

    run = await db_session.get(IngestRun, run_id)
    assert run.status == "succeeded"
    assert run.n_promoted == 1
    row = await db_session.get(ConceptProblem, pid)
    assert row.tier == 2


# --------------------------------------------------------------------------- #
# T-OR2 — pairing rejection continues, run still succeeds
# --------------------------------------------------------------------------- #
async def test_run_provisioning_pairing_rejection_continues(db_session, monkeypatch):
    space, run_id, claimed = await _seed(db_session, slug="or2")
    concept_id = await _seed_concept(db_session, search_space_id=space)
    chash = "c1"
    await _seed_tier1_row(
        db_session, concept_id=concept_id, chash=chash, search_space_id=space
    )

    async def _fog(db, q, *, retrieve_fn, chat_fn):  # noqa: ANN001
        return _draft()

    async def _vp(q, draft, *, retrieve_fn, judge_fn):  # noqa: ANN001
        return PairingVerdict(paired=False, faithful=False, confidence=0.2)

    _patch_stages(
        monkeypatch,
        scrape_candidates=(_candidate(chash=chash),),
        find_or_generate=_fog,
        validate_pair=_vp,
        concept_id=concept_id,
    )

    outcome = await _run(db_session, claimed)

    assert outcome.status == "succeeded"
    assert outcome.n_rejected == 1
    assert outcome.n_promoted == 0
    rejects = (
        await db_session.execute(
            RejectedProblem.__table__.select().where(
                RejectedProblem.ingest_run_id == run_id
            )
        )
    ).all()
    assert len(rejects) == 1
    assert rejects[0].rejected_stage == "pairing_gate"
    assert rejects[0].failed_gate is None


# --------------------------------------------------------------------------- #
# T-OR3 — lint rejection continues
# --------------------------------------------------------------------------- #
async def test_run_provisioning_lint_rejection_continues(db_session, monkeypatch):
    space, run_id, claimed = await _seed(db_session, slug="or3")
    concept_id = await _seed_concept(db_session, search_space_id=space)
    chash = "c1"
    pid = await _seed_tier1_row(
        db_session, concept_id=concept_id, chash=chash, search_space_id=space
    )

    async def _fog(db, q, *, retrieve_fn, chat_fn):  # noqa: ANN001
        return _draft()

    async def _vp(q, draft, *, retrieve_fn, judge_fn):  # noqa: ANN001
        return PairingVerdict(paired=True, faithful=True, confidence=1.0)

    async def _tm(db, pair, *, chat_fn, embed_fn):  # noqa: ANN001
        return _mint_plan(concept_id)

    async def _promote(db, neo, **kwargs):  # noqa: ANN001
        return PromoteResult(promoted=False, failed_gate=4, diagnostic="foreign")

    _patch_stages(
        monkeypatch,
        scrape_candidates=(_candidate(chash=chash),),
        find_or_generate=_fog,
        validate_pair=_vp,
        tag_and_mint=_tm,
        promote=_promote,
        concept_id=concept_id,
    )

    outcome = await _run(db_session, claimed)

    assert outcome.status == "succeeded"
    assert outcome.n_rejected == 1
    assert outcome.n_promoted == 0
    rejects = (
        await db_session.execute(
            RejectedProblem.__table__.select().where(
                RejectedProblem.ingest_run_id == run_id
            )
        )
    ).all()
    assert len(rejects) == 1
    assert rejects[0].rejected_stage == "promotion_lint"
    assert rejects[0].failed_gate == 4
    row = await db_session.get(ConceptProblem, pid)
    assert row.tier == 1  # stays Tier-1


# --------------------------------------------------------------------------- #
# T-OR4 — solution error fails the run
# --------------------------------------------------------------------------- #
async def test_run_provisioning_solution_error_fails_run(db_session, monkeypatch):
    from apollo.provisioning.solution import SolutionDraftError

    space, run_id, claimed = await _seed(db_session, slug="or4")
    concept_id = await _seed_concept(db_session, search_space_id=space)
    chash = "c1"
    await _seed_tier1_row(
        db_session, concept_id=concept_id, chash=chash, search_space_id=space
    )

    async def _fog(db, q, *, retrieve_fn, chat_fn):  # noqa: ANN001
        raise SolutionDraftError("no solution")

    _patch_stages(
        monkeypatch,
        scrape_candidates=(_candidate(chash=chash),),
        find_or_generate=_fog,
        concept_id=concept_id,
    )

    outcome = await _run(db_session, claimed)

    assert outcome.status == "failed"
    run = await db_session.get(IngestRun, run_id)
    assert run.status == "failed"
    errors = (
        await db_session.execute(
            IngestError.__table__.select().where(IngestError.ingest_run_id == run_id)
        )
    ).all()
    assert len(errors) == 1
    assert errors[0].stage == "find_or_generate"
    assert errors[0].error_class == "SolutionDraftError"


# --------------------------------------------------------------------------- #
# T-OR5 — tag/mint error fails the run
# --------------------------------------------------------------------------- #
async def test_run_provisioning_tagmint_error_fails_run(db_session, monkeypatch):
    from apollo.provisioning.tag_mint import TagMintError

    space, run_id, claimed = await _seed(db_session, slug="or5")
    concept_id = await _seed_concept(db_session, search_space_id=space)
    chash = "c1"
    await _seed_tier1_row(
        db_session, concept_id=concept_id, chash=chash, search_space_id=space
    )

    async def _fog(db, q, *, retrieve_fn, chat_fn):  # noqa: ANN001
        return _draft()

    async def _vp(q, draft, *, retrieve_fn, judge_fn):  # noqa: ANN001
        return PairingVerdict(paired=True, faithful=True, confidence=1.0)

    async def _tm(db, pair, *, chat_fn, embed_fn):  # noqa: ANN001
        raise TagMintError("bad tag")

    _patch_stages(
        monkeypatch,
        scrape_candidates=(_candidate(chash=chash),),
        find_or_generate=_fog,
        validate_pair=_vp,
        tag_and_mint=_tm,
        concept_id=concept_id,
    )

    outcome = await _run(db_session, claimed)

    assert outcome.status == "failed"
    errors = (
        await db_session.execute(
            IngestError.__table__.select().where(IngestError.ingest_run_id == run_id)
        )
    ).all()
    assert len(errors) == 1
    assert errors[0].stage == "tag_mint"
    assert errors[0].error_class == "TagMintError"


# --------------------------------------------------------------------------- #
# T-OR6 — cost abort fails the run
# --------------------------------------------------------------------------- #
async def test_run_provisioning_cost_abort_fails_run(db_session, monkeypatch):
    space, run_id, claimed = await _seed(db_session, slug="or6")
    concept_id = await _seed_concept(db_session, search_space_id=space)

    # The cost abort fires inside scrape's injected chat_fn (the first LLM call).
    _patch_stages(
        monkeypatch,
        scrape_candidates=(),
        concept_id=concept_id,
    )

    outcome = await _run(
        db_session, claimed, metered=_FakeMeteredChat(raise_on_scrape=True)
    )

    assert outcome.status == "failed"
    errors = (
        await db_session.execute(
            IngestError.__table__.select().where(IngestError.ingest_run_id == run_id)
        )
    ).all()
    assert len(errors) == 1
    assert errors[0].error_class == "CostBudgetExceeded"
    assert "tokens" in (errors[0].context or {})
    assert "ceiling" in (errors[0].context or {})


# --------------------------------------------------------------------------- #
# T-OR7 — counts re-assigned (not doubled) on replay
# --------------------------------------------------------------------------- #
async def test_run_provisioning_recomputes_counts_on_replay(db_session, monkeypatch):
    space, run_id, claimed = await _seed(db_session, slug="or7")
    concept_id = await _seed_concept(db_session, search_space_id=space)
    chash = "c1"
    await _seed_tier1_row(
        db_session, concept_id=concept_id, chash=chash, search_space_id=space
    )

    async def _fog(db, q, *, retrieve_fn, chat_fn):  # noqa: ANN001
        return _draft()

    async def _vp(q, draft, *, retrieve_fn, judge_fn):  # noqa: ANN001
        return PairingVerdict(paired=True, faithful=True, confidence=1.0)

    async def _tm(db, pair, *, chat_fn, embed_fn):  # noqa: ANN001
        return _mint_plan(concept_id)

    async def _promote(db, neo, **kwargs):  # noqa: ANN001
        return PromoteResult(promoted=True)

    _patch_stages(
        monkeypatch,
        scrape_candidates=(_candidate(chash=chash),),
        find_or_generate=_fog,
        validate_pair=_vp,
        tag_and_mint=_tm,
        promote=_promote,
        concept_id=concept_id,
    )

    o1 = await _run(db_session, claimed)
    o2 = await _run(db_session, claimed)

    # Counts are RE-ASSIGNED each run, never accumulated across replays.
    assert o1.n_questions_scraped == o2.n_questions_scraped
    assert o1.n_promoted == o2.n_promoted == 1
    run = await db_session.get(IngestRun, run_id)
    assert run.n_promoted == 1  # NOT 2 (a `+=` would double -> REDs)


# --------------------------------------------------------------------------- #
# T-OR8 — started_at / finished_at lifecycle
# --------------------------------------------------------------------------- #
async def test_run_provisioning_sets_started_and_finished(db_session, monkeypatch):
    space, run_id, claimed = await _seed(db_session, slug="or8")
    concept_id = await _seed_concept(db_session, search_space_id=space)
    _patch_stages(monkeypatch, scrape_candidates=(), concept_id=concept_id)

    outcome = await _run(db_session, claimed)

    assert outcome.status == "succeeded"
    run = await db_session.get(IngestRun, run_id)
    assert run.started_at is not None
    assert run.finished_at is not None
    assert run.status == "succeeded"  # never stranded at 'running'


# --------------------------------------------------------------------------- #
# T-OR9 — empty scrape succeeds
# --------------------------------------------------------------------------- #
async def test_run_provisioning_empty_scrape_succeeds(db_session, monkeypatch):
    space, run_id, claimed = await _seed(db_session, slug="or9")
    concept_id = await _seed_concept(db_session, search_space_id=space)
    _patch_stages(monkeypatch, scrape_candidates=(), concept_id=concept_id)

    outcome = await _run(db_session, claimed)

    assert outcome.status == "succeeded"
    assert outcome.n_questions_scraped == 0
    assert outcome.n_promoted == 0
    assert outcome.n_rejected == 0
    errors = (
        await db_session.execute(
            IngestError.__table__.select().where(IngestError.ingest_run_id == run_id)
        )
    ).all()
    assert not errors


# --------------------------------------------------------------------------- #
# T-OR10 — an unexpected stage exception fails the run terminally (never running)
# --------------------------------------------------------------------------- #
async def test_run_provisioning_unexpected_error_fails_run_terminally(
    db_session, monkeypatch
):
    space, run_id, claimed = await _seed(db_session, slug="or10")
    concept_id = await _seed_concept(db_session, search_space_id=space)

    async def _boom_scrape(chunks, *, chat_fn):  # noqa: ANN001
        raise ValueError("totally unexpected")

    async def _resolve_prov(db, *, search_space_id):  # noqa: ANN001
        return concept_id

    monkeypatch.setattr(orch, "scrape_questions", _boom_scrape)
    monkeypatch.setattr(orch, "resolve_or_create_provisional_concept", _resolve_prov)

    outcome = await _run(db_session, claimed)

    assert outcome.status == "failed"
    run = await db_session.get(IngestRun, run_id)
    assert run.status == "failed"  # NEVER stranded at 'running'
    assert run.finished_at is not None
    errors = (
        await db_session.execute(
            IngestError.__table__.select().where(IngestError.ingest_run_id == run_id)
        )
    ).all()
    assert len(errors) == 1
    assert errors[0].stage == "orchestrator"
    assert errors[0].error_class == "ValueError"


# --------------------------------------------------------------------------- #
# T-OR11 — cost abort DURING validate_pair fails the run (per-stage cost branch)
# --------------------------------------------------------------------------- #
async def test_run_provisioning_cost_abort_in_validate_pair(db_session, monkeypatch):
    space, run_id, claimed = await _seed(db_session, slug="or11")
    concept_id = await _seed_concept(db_session, search_space_id=space)
    chash = "c1"
    await _seed_tier1_row(
        db_session, concept_id=concept_id, chash=chash, search_space_id=space
    )

    async def _fog(db, q, *, retrieve_fn, chat_fn):  # noqa: ANN001
        return _draft()

    async def _vp(q, draft, *, retrieve_fn, judge_fn):  # noqa: ANN001
        raise CostBudgetExceeded(tokens=99, ceiling=5, document_id=1)

    _patch_stages(
        monkeypatch,
        scrape_candidates=(_candidate(chash=chash),),
        find_or_generate=_fog,
        validate_pair=_vp,
        concept_id=concept_id,
    )

    outcome = await _run(db_session, claimed)

    assert outcome.status == "failed"
    errors = (
        await db_session.execute(
            IngestError.__table__.select().where(IngestError.ingest_run_id == run_id)
        )
    ).all()
    assert len(errors) == 1
    assert errors[0].stage == "validate_pair"
    assert errors[0].error_class == "CostBudgetExceeded"


# --------------------------------------------------------------------------- #
# T-OR12 — cost abort DURING tag_and_mint fails the run (per-stage cost branch)
# --------------------------------------------------------------------------- #
async def test_run_provisioning_cost_abort_in_tag_mint(db_session, monkeypatch):
    space, run_id, claimed = await _seed(db_session, slug="or12")
    concept_id = await _seed_concept(db_session, search_space_id=space)
    chash = "c1"
    await _seed_tier1_row(
        db_session, concept_id=concept_id, chash=chash, search_space_id=space
    )

    async def _fog(db, q, *, retrieve_fn, chat_fn):  # noqa: ANN001
        return _draft()

    async def _vp(q, draft, *, retrieve_fn, judge_fn):  # noqa: ANN001
        return PairingVerdict(paired=True, faithful=True, confidence=1.0)

    async def _tm(db, pair, *, chat_fn, embed_fn):  # noqa: ANN001
        raise CostBudgetExceeded(tokens=99, ceiling=5, document_id=1)

    _patch_stages(
        monkeypatch,
        scrape_candidates=(_candidate(chash=chash),),
        find_or_generate=_fog,
        validate_pair=_vp,
        tag_and_mint=_tm,
        concept_id=concept_id,
    )

    outcome = await _run(db_session, claimed)

    assert outcome.status == "failed"
    errors = (
        await db_session.execute(
            IngestError.__table__.select().where(IngestError.ingest_run_id == run_id)
        )
    ).all()
    assert len(errors) == 1
    assert errors[0].stage == "tag_mint"
    assert errors[0].error_class == "CostBudgetExceeded"


# --------------------------------------------------------------------------- #
# T-OR13 — find_or_generate cost abort fails the run (per-stage cost branch)
# --------------------------------------------------------------------------- #
async def test_run_provisioning_cost_abort_in_find_or_generate(
    db_session, monkeypatch
):
    space, run_id, claimed = await _seed(db_session, slug="or13")
    concept_id = await _seed_concept(db_session, search_space_id=space)
    chash = "c1"
    await _seed_tier1_row(
        db_session, concept_id=concept_id, chash=chash, search_space_id=space
    )

    async def _fog(db, q, *, retrieve_fn, chat_fn):  # noqa: ANN001
        raise CostBudgetExceeded(tokens=99, ceiling=5, document_id=1)

    _patch_stages(
        monkeypatch,
        scrape_candidates=(_candidate(chash=chash),),
        find_or_generate=_fog,
        concept_id=concept_id,
    )

    outcome = await _run(db_session, claimed)

    assert outcome.status == "failed"
    errors = (
        await db_session.execute(
            IngestError.__table__.select().where(IngestError.ingest_run_id == run_id)
        )
    ).all()
    assert len(errors) == 1
    assert errors[0].stage == "find_or_generate"
    assert errors[0].error_class == "CostBudgetExceeded"


# --------------------------------------------------------------------------- #
# T-OR14 — a CanonProjectionError from promote fails the run (promotion stage)
# --------------------------------------------------------------------------- #
async def test_run_provisioning_canon_error_fails_run(db_session, monkeypatch):
    from apollo.knowledge_graph.canon_projection import CanonProjectionError

    space, run_id, claimed = await _seed(db_session, slug="or14")
    concept_id = await _seed_concept(db_session, search_space_id=space)
    chash = "c1"
    await _seed_tier1_row(
        db_session, concept_id=concept_id, chash=chash, search_space_id=space
    )

    async def _fog(db, q, *, retrieve_fn, chat_fn):  # noqa: ANN001
        return _draft()

    async def _vp(q, draft, *, retrieve_fn, judge_fn):  # noqa: ANN001
        return PairingVerdict(paired=True, faithful=True, confidence=1.0)

    async def _tm(db, pair, *, chat_fn, embed_fn):  # noqa: ANN001
        return _mint_plan(concept_id)

    async def _promote(db, neo, **kwargs):  # noqa: ANN001
        raise CanonProjectionError(stage="merge_canon", last_error="neo down")

    _patch_stages(
        monkeypatch,
        scrape_candidates=(_candidate(chash=chash),),
        find_or_generate=_fog,
        validate_pair=_vp,
        tag_and_mint=_tm,
        promote=_promote,
        concept_id=concept_id,
    )

    outcome = await _run(db_session, claimed)

    assert outcome.status == "failed"
    errors = (
        await db_session.execute(
            IngestError.__table__.select().where(IngestError.ingest_run_id == run_id)
        )
    ).all()
    assert len(errors) == 1
    assert errors[0].stage == "promotion"
    assert errors[0].error_class == "CanonProjectionError"
