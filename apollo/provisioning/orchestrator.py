"""WU-3B2g — the 6-stage per-document auto-provisioning orchestrator.

``run_provisioning`` WIRES the already-built §8B stages (3B2b–3B2f) for one
claimed document. It REDEFINES no stage — every stage function is a FROZEN import
the orchestrator merely sequences. The orchestrator OWNS:

  * the ``apollo_ingest_runs`` lifecycle: ``queued`` -> ``running`` (+``started_at``)
    -> terminal ``succeeded`` | ``failed`` (+``finished_at``). It NEVER leaves the
    run ``running`` — a per-document error always flips it to a TERMINAL status
    (else the partial-unique-index would wedge re-enqueue, §9 OPS-5);
  * the §4b stage-outcome -> observability decision: a per-CANDIDATE rejection
    (pairing ``Rejection`` / lint fail) writes ONE ``apollo_rejected_problems`` row
    and CONTINUES (``n_rejected`` recomputed); a per-DOCUMENT error
    (``SolutionDraftError`` / ``TagMintError`` / ``CostBudgetExceeded`` /
    ``CanonProjectionError`` / any unexpected exception) writes ONE
    ``apollo_ingest_errors`` row and FAILS the whole run;
  * the per-run counters: ``n_*`` are ASSIGNED from freshly-computed values (never
    ``+=``) so a re-claimed job's replay does not inflate them (§2c).

The six stages, per candidate:
  1. scrape_questions + write_tier1_problems (3B2d) — once per document
  2. find_or_generate (3B2e)
  3. validate_pair + rejection_from_verdict (3B2e)
  4. build_approved_pair + tag_and_mint (3B2e->3B2d) [dedup resolve_candidate
     runs INSIDE tag_and_mint, 3B2c]
  5. promote (run_promotion_lint 3B2b + project_canon 3C1, promote.py)

The orchestrator does NOT call ``complete_job``/``fail_job`` — that terminal job
decision is the WORKER's (so a test can drive ``run_provisioning`` in isolation).
``metered_chat`` is the FROZEN cost-aggregating client (3B2f); its ``.cheap`` /
``.main`` / ``.scrape_chat_fn`` are the injected stage callables, metering onto the
run row. NO new LLM call of its own; all LLM is mocked in Tier-1.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apollo.knowledge_graph.canon_projection import CanonProjectionError
from apollo.persistence.models import (
    ConceptProblem,
    IngestError,
    IngestRun,
    RejectedProblem,
)
from apollo.provisioning.metered_chat import CostBudgetExceeded, MeteredChat
from apollo.provisioning.pairing_gate import (
    Rejection,
    rejection_from_verdict,
    validate_pair,
)
from apollo.provisioning.promote import PromoteResult, promote
from apollo.provisioning.queue import ClaimedJob
from apollo.provisioning.scrape import (
    resolve_or_create_provisional_concept,
    scrape_questions,
    write_tier1_problems,
)
from apollo.provisioning.solution import (
    GroundingSpan,
    SolutionDraftError,
    build_approved_pair,
    find_or_generate,
)
from apollo.provisioning.tag_mint import TagMintError, tag_and_mint

__all__ = ["run_provisioning", "ProvisioningOutcome"]

_LOG = logging.getLogger(__name__)

_RUN_RUNNING = "running"
_RUN_SUCCEEDED = "succeeded"
_RUN_FAILED = "failed"

_SCRAPE_SYSTEM_PROMPT = (
    "Extract candidate practice questions from the course passage as a JSON "
    "array of objects."
)


class ProvisioningOutcome(BaseModel):
    """The immutable per-run outcome the worker turns into complete_job/fail_job."""

    model_config = ConfigDict(frozen=True)

    run_id: int
    status: str  # 'succeeded' | 'failed'
    n_questions_scraped: int
    n_promoted: int
    n_rejected: int
    n_dedup_merged: int


class _PerDocumentError(Exception):
    """Internal carrier mapping a per-document stage failure to its
    ``apollo_ingest_errors(stage, error_class, context)`` row. Caught in
    ``run_provisioning`` to fail the run terminally (never left 'running')."""

    def __init__(self, *, stage: str, error_class: str, context: dict | None = None):
        self.stage = stage
        self.error_class = error_class
        self.context = context or {}
        super().__init__(f"{stage}:{error_class}")


def _now() -> datetime:
    return datetime.now(UTC)


# --------------------------------------------------------------------------- #
# Observability writers (single owners of each row type)
# --------------------------------------------------------------------------- #
def _record_stage_error(
    db: AsyncSession, *, run: IngestRun, stage: str, error_class: str, context: dict
) -> None:
    """Write ONE ``apollo_ingest_errors`` row for a per-document terminal error."""
    db.add(
        IngestError(
            ingest_run_id=run.id,
            search_space_id=run.search_space_id,
            stage=stage,
            error_class=error_class,
            context=dict(context),
        )
    )


def _record_rejection(
    db: AsyncSession,
    *,
    run: IngestRun,
    rejected_stage: str,
    failed_gate: int | None,
    diagnostic: str,
    concept_id: int | None,
    payload: dict,
) -> None:
    """Write ONE ``apollo_rejected_problems`` row for a per-candidate rejection."""
    db.add(
        RejectedProblem(
            ingest_run_id=run.id,
            search_space_id=run.search_space_id,
            concept_id=concept_id,
            failed_gate=failed_gate,
            rejected_stage=rejected_stage,
            diagnostic=diagnostic,
            payload=dict(payload),
        )
    )


def _recompute_counts(
    run: IngestRun,
    *,
    scraped: int,
    promoted: int,
    rejected: int,
    merged: int,
) -> None:
    """ASSIGN (never ``+=``) the per-run aggregates so a replay re-computes rather
    than inflates them (§2c — a ``+=`` would double on a re-claimed job)."""
    run.n_questions_scraped = scraped
    run.n_promoted = promoted
    run.n_rejected = rejected
    run.n_dedup_merged = merged


# --------------------------------------------------------------------------- #
# Per-candidate handlers — each returns ('promoted'|'rejected'|'merged_delta')
# --------------------------------------------------------------------------- #
class _ChunkView:
    """The minimal chunk shape ``scrape_questions`` reads (``content`` /
    ``document_id`` / ``page_number``). Selecting only these columns avoids
    coupling the orchestrator to the full ``AITAChunk`` ORM (e.g. the pgvector
    ``embedding`` column) and keeps the read cheap."""

    __slots__ = ("content", "document_id", "page_number")

    def __init__(self, content: str, document_id: int, page_number: int | None):
        self.content = content
        self.document_id = document_id
        self.page_number = page_number


async def _load_chunks(db: AsyncSession, *, document_id: int) -> Sequence[_ChunkView]:
    from database.models import AITAChunk

    rows = (
        await db.execute(
            select(
                AITAChunk.content,
                AITAChunk.document_id,
                AITAChunk.page_number,
            )
            .where(AITAChunk.document_id == document_id)
            .order_by(AITAChunk.id.asc())
        )
    ).all()
    return [_ChunkView(r.content, r.document_id, r.page_number) for r in rows]


async def _find_tier1_row_id(
    db: AsyncSession, *, concept_id: int, chunk_content_hash: str
) -> int | None:
    """The Tier-1 ``apollo_concept_problems.id`` to flip on promote, keyed on the
    content-derived ``problem_code`` (``scrape.<chunk_content_hash>``)."""
    return (
        await db.execute(
            select(ConceptProblem.id)
            .where(ConceptProblem.concept_id == concept_id)
            .where(ConceptProblem.problem_code == f"scrape.{chunk_content_hash}")
        )
    ).scalar_one_or_none()


async def run_provisioning(
    db: AsyncSession,
    neo,
    *,
    job: ClaimedJob,
    metered_chat: MeteredChat,
    embed_fn: Callable[[str], Sequence[float]] | None = None,
    retrieve_fn: Callable[..., Awaitable[Sequence[GroundingSpan]]] | None = None,
) -> ProvisioningOutcome:
    """Run the 6 stages for one claimed document. See the module docstring.

    Loads the run row by ``job.ingest_run_id``, flips it ``running`` (+started_at),
    drives scrape -> per-candidate (find_or_generate -> validate_pair ->
    build_approved_pair -> tag_and_mint -> promote) with the §4b decision table,
    re-assigns the per-run counts, sets the terminal status (+finished_at), and
    commits. Returns the outcome; the worker calls complete_job/fail_job on it."""
    run = await db.get(IngestRun, job.ingest_run_id)
    run.status = _RUN_RUNNING
    run.started_at = _now()
    await db.flush()

    if embed_fn is None:  # pragma: no cover - default wiring exercised at runtime
        from indexing.document_embedder import embed_text as embed_fn  # type: ignore
    if retrieve_fn is None:  # pragma: no cover - default course-corpus adapter
        retrieve_fn = _default_retrieve_fn

    scraped = 0
    promoted = 0
    rejected = 0
    merged = 0

    try:
        provisional_concept_id = await resolve_or_create_provisional_concept(
            db, search_space_id=job.search_space_id
        )
        chunks = await _load_chunks(db, document_id=job.document_id)
        try:
            scrape_result = await scrape_questions(
                chunks, chat_fn=metered_chat.scrape_chat_fn(_SCRAPE_SYSTEM_PROMPT)
            )
        except CostBudgetExceeded as exc:
            raise _cost_abort(exc, stage="scrape") from exc
        scraped = scrape_result.scraped_count
        await write_tier1_problems(
            db,
            scrape_result.candidates,
            concept_id=provisional_concept_id,
            search_space_id=job.search_space_id,
        )

        for candidate in scrape_result.candidates:
            outcome = await _process_candidate(
                db,
                neo,
                candidate=candidate,
                provisional_concept_id=provisional_concept_id,
                search_space_id=job.search_space_id,
                run=run,
                metered_chat=metered_chat,
                embed_fn=embed_fn,
                retrieve_fn=retrieve_fn,
            )
            if outcome == "promoted":
                promoted += 1
            elif outcome == "rejected":
                rejected += 1
            merged += 0  # dedup merges accrue inside tag_and_mint (3B2c); v1: 0

    except _PerDocumentError as exc:
        _record_stage_error(
            db,
            run=run,
            stage=exc.stage,
            error_class=exc.error_class,
            context=exc.context,
        )
        return await _finalize(
            db,
            run,
            status=_RUN_FAILED,
            scraped=scraped,
            promoted=promoted,
            rejected=rejected,
            merged=merged,
        )
    except Exception as exc:  # noqa: BLE001 - any unexpected stage error fails the
        # run TERMINALLY (never left 'running' — §4c wedge-prevention). The error
        # is recorded with its class so ops can triage; the run is then failed.
        _record_stage_error(
            db,
            run=run,
            stage="orchestrator",
            error_class=type(exc).__name__,
            context={},
        )
        return await _finalize(
            db,
            run,
            status=_RUN_FAILED,
            scraped=scraped,
            promoted=promoted,
            rejected=rejected,
            merged=merged,
        )

    return await _finalize(
        db,
        run,
        status=_RUN_SUCCEEDED,
        scraped=scraped,
        promoted=promoted,
        rejected=rejected,
        merged=merged,
    )


async def _process_candidate(
    db: AsyncSession,
    neo,
    *,
    candidate,
    provisional_concept_id: int,
    search_space_id: int,
    run: IngestRun,
    metered_chat: MeteredChat,
    embed_fn,
    retrieve_fn,
) -> str:
    """Run stages 2-5 for one candidate. Returns 'promoted' | 'rejected'.

    A per-CANDIDATE rejection (pairing fail / lint fail) writes the rejection row
    and returns 'rejected' (the run continues). A per-DOCUMENT error raises a
    ``_PerDocumentError`` to abort the whole run."""
    # --- stage 2: find_or_generate ---------------------------------------- #
    try:
        draft = await find_or_generate(
            db, candidate, retrieve_fn=retrieve_fn, chat_fn=metered_chat.main
        )
    except SolutionDraftError as exc:
        raise _PerDocumentError(
            stage="find_or_generate", error_class="SolutionDraftError"
        ) from exc
    except CostBudgetExceeded as exc:
        raise _cost_abort(exc, stage="find_or_generate") from exc

    # --- stage 3: validate_pair + rejection mapping ----------------------- #
    try:
        verdict = await validate_pair(
            candidate, draft, retrieve_fn=retrieve_fn, judge_fn=metered_chat.cheap
        )
    except CostBudgetExceeded as exc:
        raise _cost_abort(exc, stage="validate_pair") from exc

    rej = rejection_from_verdict(verdict)
    if rej is not None:
        _record_rejection(
            db,
            run=run,
            rejected_stage="pairing_gate",
            failed_gate=None,
            diagnostic=rej.diagnostic,
            concept_id=provisional_concept_id,
            payload={"reason": rej.reason},
        )
        return "rejected"

    # --- stage 4: build_approved_pair + tag_and_mint ---------------------- #
    pair = build_approved_pair(candidate, draft, search_space_id=search_space_id)
    try:
        mint_plan = await tag_and_mint(
            db, pair, chat_fn=metered_chat.cheap, embed_fn=embed_fn
        )
    except TagMintError as exc:
        raise _PerDocumentError(
            stage="tag_mint", error_class="TagMintError"
        ) from exc
    except CostBudgetExceeded as exc:
        raise _cost_abort(exc, stage="tag_mint") from exc

    # --- stage 5: promote (lint + :Canon) --------------------------------- #
    concept_problem_id = await _find_tier1_row_id(
        db,
        concept_id=provisional_concept_id,
        chunk_content_hash=candidate.chunk_content_hash,
    )
    if concept_problem_id is None:  # pragma: no cover - write_tier1 always wrote it
        raise _PerDocumentError(
            stage="promote", error_class="MissingTier1Row"
        )
    try:
        result: PromoteResult = await promote(
            db,
            neo,
            problem=pair.problem,
            mint_plan=mint_plan,
            search_space_id=search_space_id,
            concept_problem_id=concept_problem_id,
            existing_problem_hashes=set(),
        )
    except CanonProjectionError as exc:
        raise _PerDocumentError(
            stage="promotion", error_class="CanonProjectionError"
        ) from exc

    if not result.promoted:
        _record_rejection(
            db,
            run=run,
            rejected_stage="promotion_lint",
            failed_gate=result.failed_gate,
            diagnostic=result.diagnostic,
            concept_id=mint_plan.concept_id,
            payload={},
        )
        return "rejected"
    return "promoted"


def _cost_abort(exc: CostBudgetExceeded, *, stage: str) -> _PerDocumentError:
    return _PerDocumentError(
        stage=stage,
        error_class="CostBudgetExceeded",
        context={"tokens": exc.tokens, "ceiling": exc.ceiling},
    )


async def _finalize(
    db: AsyncSession,
    run: IngestRun,
    *,
    status: str,
    scraped: int,
    promoted: int,
    rejected: int,
    merged: int,
) -> ProvisioningOutcome:
    """ASSIGN the recomputed counts, set the terminal status + finished_at, commit,
    and return the immutable outcome. The run is NEVER left 'running'."""
    _recompute_counts(
        run, scraped=scraped, promoted=promoted, rejected=rejected, merged=merged
    )
    run.status = status
    run.finished_at = _now()
    await db.commit()
    _LOG.info(
        "provisioning_run_finalized",
        extra={
            "event": "provisioning_run_finalized",
            "ingest_run_id": int(run.id),
            "status": status,
            "n_promoted": promoted,
            "n_rejected": rejected,
        },
    )
    return ProvisioningOutcome(
        run_id=int(run.id),
        status=status,
        n_questions_scraped=scraped,
        n_promoted=promoted,
        n_rejected=rejected,
        n_dedup_merged=merged,
    )


async def _default_retrieve_fn(question) -> Sequence[GroundingSpan]:  # pragma: no cover
    """The default course-corpus retrieval adapter for ``find_or_generate`` /
    ``validate_pair``. v1 returns no spans (the generate branch grounds on the
    question alone); the real hybrid-retrieval adapter is a Tier-2 nightly
    concern. Kept <20 lines per the plan §16 deviation note."""
    return ()
