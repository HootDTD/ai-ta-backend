"""WU-3B2g — the 6-stage per-document auto-provisioning orchestrator.

``run_provisioning`` WIRES the already-built §8B stages (3B2b–3B2f) for one
claimed document. It REDEFINES no stage — every stage function is a FROZEN import
the orchestrator merely sequences. The orchestrator OWNS:

  * the ``apollo_ingest_runs`` lifecycle: ``queued`` -> ``running`` (+``started_at``)
    -> terminal ``succeeded`` | ``failed`` (+``finished_at``). It NEVER leaves the
    run ``running`` — a per-document error always flips it to a TERMINAL status
    (else the partial-unique-index would wedge re-enqueue, §9 OPS-5);
  * the §4b stage-outcome -> observability decision: a per-CANDIDATE rejection
    (pairing ``Rejection`` / lint fail / a ``find_or_generate``
    ``SolutionDraftError``) writes ONE ``apollo_rejected_problems`` row and
    CONTINUES (``n_rejected`` recomputed); a per-DOCUMENT error
    (``TagMintError`` / ``CostBudgetExceeded`` / ``CanonProjectionError`` / any
    unexpected exception) writes ONE ``apollo_ingest_errors`` row and FAILS the
    whole run;
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
from dataclasses import dataclass
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apollo.knowledge_graph.canon_projection import CanonProjectionError
from apollo.persistence.models import (
    ConceptProblem,
    IngestError,
    IngestRun,
    RejectedProblem,
)
from apollo.provisioning.cost_constants import (
    APOLLO_SCRAPE_MAX_SECTIONS,
    APOLLO_SCRAPE_MIN_CANDIDATES,
    structured_scrape_enabled,
)
from apollo.provisioning.metered_chat import CostBudgetExceeded, MeteredChat
from apollo.provisioning.pairing_gate import (
    rejection_from_verdict,
    validate_pair,
)
from apollo.provisioning.problem_hash import problem_dup_hash
from apollo.provisioning.promote import PromoteResult, promote
from apollo.provisioning.provisioning_schema import build_tag_schema
from apollo.provisioning.queue import ClaimedJob
from apollo.provisioning.retrieval_adapter import make_course_retrieve_fn
from apollo.provisioning.scrape import (
    resolve_or_create_provisional_concept,
    scrape_document,
    write_tier1_problems,
)
from apollo.provisioning.solution import (
    GroundingSpan,
    SolutionDraftError,
    build_approved_pair,
    build_authored_approved_pair,
    construct_authored_reference,
    find_or_generate,
)
from apollo.provisioning.tag_mint import TagMintError, tag_and_mint
from apollo.schemas.problem import Problem

__all__ = [
    "run_provisioning",
    "ProvisioningOutcome",
    "provision_authored_problem",
    "AuthoredProvisionResult",
]

_LOG = logging.getLogger(__name__)

_RUN_RUNNING = "running"
_RUN_SUCCEEDED = "succeeded"
_RUN_FAILED = "failed"

_SCRAPE_SYSTEM_PROMPT = (
    "You extract EVERY question a student could be asked to answer from one "
    "SECTION of course material, in ANY subject (textbook prose, worked "
    "examples, exercise sets, exam study guides, and review outlines all count; "
    "a section may contain zero, one, or many questions). Numeric solve-for "
    "exercises, convergence/divergence determinations, show-that/verify tasks, "
    "true/false items, define/explain/compare prompts, and open-ended "
    "study-guide or discussion questions ALL count as problems — for a question "
    'with no numeric answer, "target_unknown" is a short phrase naming what is '
    'asked (e.g. "convergence verdict", "definition of future shock") and '
    '"given_values" is {}.\n'
    "Return ONLY a JSON array - no prose, no explanation, no markdown code fences. "
    "Each array element is an object with EXACTLY these keys:\n"
    '  "problem_text": string - the full, self-contained problem statement.\n'
    '  "given_values": object mapping each stated known quantity\'s short symbol to '
    "its NUMERIC value (numbers only - no units, no strings); use {} if none.\n"
    '  "target_unknown": string - the single quantity or idea the problem asks '
    "to find.\n"
    '  "difficulty": exactly one of "intro", "standard", "hard".\n'
    '  "concept_slug": string - a short dotted/kebab concept id, e.g. '
    '"bernoulli-equation".\n'
    '  "label": the problem\'s printed number/label exactly as shown, e.g. '
    '"Problem 3", "Q3", "3.", or null if none.\n'
    "If the section truly contains no questions, return []."
)

_TRIAGE_SYSTEM_PROMPT = (
    "You triage a document's SECTIONS to find which likely contain questions a "
    "student could be asked to answer — quantitative exercises OR qualitative "
    "review/discussion questions. You receive a JSON array of sections, each with "
    'an "index", "title", "chars", and "has_numeric_imperative" flag.\n'
    "Return ONLY a JSON array - no prose, no markdown fences. Each element is an "
    "object with EXACTLY these keys:\n"
    '  "index": integer - echo the section\'s index.\n'
    '  "is_problem_likely": boolean - true if the section probably contains '
    "questions, practice problems, or worked examples.\n"
    '  "priority": integer 0-10 - higher = scrape sooner.\n'
    '  "concept_slug": string - a short dotted/kebab concept id for the section.\n'
    '  "concept_display": string - a human-readable concept label.\n'
    "Include EVERY index from the input exactly once."
)

_TAG_MINT_SYSTEM_PROMPT = (
    "You tag an already-approved problem (quantitative OR qualitative) with its "
    "canonical concept and the prerequisite edges between its solution entities.\n"
    "Return ONLY a JSON object - no prose, no explanation, no markdown code fences. "
    "The object has EXACTLY these keys:\n"
    '  "concept_slug": string - a short dotted/kebab concept id (e.g. '
    '"bernoulli-equation"). REQUIRED.\n'
    '  "display_name": string - a human-readable concept label; if unknown, repeat '
    "the concept_slug.\n"
    '  "prereqs": array of {"from": <entity-key>, "to": <entity-key>} objects naming '
    "prerequisite edges between the problem's minted entity keys; use [] if none."
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
    run.n_questions_scraped = scraped  # type: ignore[assignment]
    run.n_promoted = promoted  # type: ignore[assignment]
    run.n_rejected = rejected  # type: ignore[assignment]
    run.n_dedup_merged = merged  # type: ignore[assignment]


# --------------------------------------------------------------------------- #
# Per-candidate handlers — each returns ('promoted'|'rejected'|'merged_delta')
# --------------------------------------------------------------------------- #
class _ChunkView:
    """The minimal chunk shape the scrape reads. Now carries the section metadata
    (``id``/``section_path``/``chunk_type``) so ``group_into_sections`` can rebuild
    the document's sections. Selecting only these columns keeps the read cheap (no
    pgvector ``embedding``)."""

    __slots__ = ("id", "content", "document_id", "page_number", "section_path", "chunk_type")

    def __init__(self, id, content, document_id, page_number, section_path, chunk_type):  # noqa: A002
        self.id = id
        self.content = content
        self.document_id = document_id
        self.page_number = page_number
        self.section_path = section_path
        self.chunk_type = chunk_type


async def _load_chunks(db: AsyncSession, *, document_id: int) -> Sequence[_ChunkView]:
    from database.models import AITAChunk

    rows = (
        await db.execute(
            select(
                AITAChunk.id,
                AITAChunk.content,
                AITAChunk.document_id,
                AITAChunk.page_number,
                AITAChunk.section_path,
                AITAChunk.chunk_type,
            )
            .where(AITAChunk.document_id == document_id)
            .order_by(AITAChunk.id.asc())
        )
    ).all()
    return [
        _ChunkView(r.id, r.content, r.document_id, r.page_number, r.section_path, r.chunk_type)
        for r in rows
    ]


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
    if run is None:
        raise RuntimeError(f"run_provisioning: ingest_run {job.ingest_run_id} not found")
    run.status = _RUN_RUNNING  # type: ignore[assignment]
    run.started_at = _now()  # type: ignore[assignment]
    await db.flush()

    if embed_fn is None:
        from indexing.document_embedder import embed_text as embed_fn  # type: ignore
    if retrieve_fn is None:
        retrieve_fn = make_course_retrieve_fn(db, search_space_id=job.search_space_id)

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
            scrape_result = await scrape_document(
                chunks,  # type: ignore[arg-type]  # _ChunkView is the duck-typed shape grouping reads
                chat_fn=metered_chat.scrape_chat_fn(_SCRAPE_SYSTEM_PROMPT),
                triage_chat_fn=metered_chat.scrape_chat_fn(_TRIAGE_SYSTEM_PROMPT),
                max_sections=APOLLO_SCRAPE_MAX_SECTIONS,
                min_candidates=APOLLO_SCRAPE_MIN_CANDIDATES,
                structured=structured_scrape_enabled(),
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
            outcome, merged_delta = await _process_candidate(
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
            # ``merged_delta`` is the count of entity candidates ``tag_and_mint`` (3B2c
            # dedup ladder) RESOLVED to an existing entity instead of minting fresh —
            # ``len(mint_plan.merged_entity_keys)`` for this candidate. 0 when a
            # candidate rejected before tag/mint or merged nothing.
            merged += merged_delta

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


def _tag_mint_chat_fn(metered_chat: MeteredChat) -> Callable[[str], str]:
    """Adapt the keyword-only ``MeteredChat.cheap`` to the POSITIONAL-string
    ``chat_fn`` contract ``tag_and_mint`` invokes (``chat_fn(json.dumps(problem))``
    at ``tag_mint.py:_parse_tag``). Mirrors ``MeteredChat.scrape_chat_fn``: handing
    ``metered_chat.cheap`` directly would raise ``TypeError`` because ``cheap`` is
    ``def cheap(self, *, purpose, messages, ...)`` (keyword-only). The
    ``find_or_generate``/``validate_pair`` stages call their callables WITH keywords,
    so they consume ``.main``/``.cheap`` unchanged — only tag/mint needs this seam."""

    def _chat_fn(prompt: str) -> str:
        return metered_chat.cheap(
            purpose="tag_mint",
            messages=[
                {"role": "system", "content": _TAG_MINT_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            response_format={"type": "json_schema", "json_schema": build_tag_schema()},
        )

    return _chat_fn


async def _concept_dup_hashes(db: AsyncSession, *, concept_id: int) -> set[str]:
    """Compute the concept-scoped gate-8 ``existing_problem_hashes`` set: load every
    already-promoted (``tier=2``) ``apollo_concept_problems`` payload for this
    BIGINT concept and apply the frozen ``problem_dup_hash`` to each. §8B.4 gate 8
    is 'not already present FOR THIS COURSE'S CONCEPT' — the orchestrator (the
    caller) supplies the concept-scoped set; an empty set would make gate 8 vacuous
    (a duplicate problem would always re-promote). A payload that does not validate
    as a ``Problem`` (e.g. a Tier-1 inventory stub) is skipped (it carries no
    promotable content to dedup against)."""
    rows = (
        (
            await db.execute(
                select(ConceptProblem.payload)
                .where(ConceptProblem.concept_id == concept_id)
                .where(ConceptProblem.tier == 2)
            )
        )
        .scalars()
        .all()
    )
    hashes: set[str] = set()
    for payload in rows:
        try:
            problem = Problem.model_validate(payload)
        except (ValidationError, ValueError):
            continue
        hashes.add(problem_dup_hash(problem))
    return hashes


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
) -> tuple[str, int]:
    """Run stages 2-5 for one candidate. Returns ``(outcome, merged_delta)`` where
    ``outcome`` is 'promoted' | 'rejected' and ``merged_delta`` is the number of
    entities ``tag_and_mint`` de-duplicated for this candidate (0 when it rejected
    before tag/mint).

    A per-CANDIDATE rejection (pairing fail / lint fail / a stage-2 SolutionDraftError) writes the rejection row
    and returns 'rejected' (the run continues). A per-DOCUMENT error raises a
    ``_PerDocumentError`` to abort the whole run."""
    # --- stage 2: find_or_generate ---------------------------------------- #
    try:
        draft = await find_or_generate(
            db, candidate, retrieve_fn=retrieve_fn, chat_fn=metered_chat.main
        )
    except SolutionDraftError as exc:
        # A single un-draftable candidate is a per-CANDIDATE rejection (write the
        # row, CONTINUE) — NOT a per-document abort. One bad candidate must not
        # sink a document whose other candidates promote (§4b decision table).
        _record_rejection(
            db,
            run=run,
            rejected_stage="solution_draft",
            failed_gate=None,
            diagnostic=str(exc),
            concept_id=provisional_concept_id,
            payload={"reason": "solution_draft_error"},
        )
        return "rejected", 0
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
        return "rejected", 0

    # --- stage 4: build_approved_pair + tag_and_mint ---------------------- #
    pair = build_approved_pair(candidate, draft, search_space_id=search_space_id)
    try:
        mint_plan = await tag_and_mint(
            db, pair, chat_fn=_tag_mint_chat_fn(metered_chat), embed_fn=embed_fn
        )
    except TagMintError as exc:
        raise _PerDocumentError(stage="tag_mint", error_class="TagMintError") from exc
    except CostBudgetExceeded as exc:
        raise _cost_abort(exc, stage="tag_mint") from exc

    merged_delta = len(mint_plan.merged_entity_keys)

    # --- stage 5: promote (lint + :Canon) --------------------------------- #
    concept_problem_id = await _find_tier1_row_id(
        db,
        concept_id=provisional_concept_id,
        chunk_content_hash=candidate.chunk_content_hash,
    )
    if concept_problem_id is None:
        # Defensive: ``write_tier1_problems`` already wrote this row earlier in the
        # run, so a miss means a corrupted/raced inventory write — fail the run
        # rather than promote a phantom row.
        raise _PerDocumentError(stage="promote", error_class="MissingTier1Row")
    # Gate 8 needs the concept-scoped dup-hash set of ALREADY-promoted problems on
    # the REAL tagged concept (``mint_plan.concept_id``) — NOT an empty set (which
    # makes gate 8 vacuous). Computed AFTER tag/mint resolved the tagged concept.
    existing_problem_hashes = await _concept_dup_hashes(db, concept_id=mint_plan.concept_id)
    try:
        result: PromoteResult = await promote(
            db,
            neo,
            problem=pair.problem,
            mint_plan=mint_plan,
            search_space_id=search_space_id,
            concept_problem_id=concept_problem_id,
            existing_problem_hashes=existing_problem_hashes,
        )
    except CanonProjectionError as exc:
        raise _PerDocumentError(stage="promotion", error_class="CanonProjectionError") from exc

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
        return "rejected", merged_delta
    return "promoted", merged_delta


def _cost_abort(exc: CostBudgetExceeded, *, stage: str) -> _PerDocumentError:
    return _PerDocumentError(
        stage=stage,
        error_class="CostBudgetExceeded",
        context={"tokens": exc.tokens, "ceiling": exc.ceiling},
    )


# --------------------------------------------------------------------------- #
# Subject-AGNOSTIC Apollo — the AUTHORED per-candidate pipeline.
#
# Where ``_process_candidate`` runs the textbook-scrape stages (find_or_generate ->
# pairing -> tag/mint -> promote), this runs the AUTHORED stages for one already-
# ingested ``AuthoredProblem``: construct the reference graph over the UNIVERSAL
# mint-map vocab -> faithfulness against the authored solution -> tag/mint ->
# content-gated promote. ``promote`` derives the applicable gates from the problem's
# OWN content (the symbolic rigor gates self-activate only on parseable equations),
# so a polisci argument promotes while a fluid one is unchanged — no stored subject
# profile. Reuses the FROZEN stages — it redefines none of them. The ClaimedJob /
# ingest_run worker lifecycle stays with ``run_provisioning``; this function is
# driven directly (ingest commits the Tier-1 inventory independently first).
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class AuthoredProvisionResult:
    """The per-authored-problem outcome. ``outcome`` ∈ {'promoted', 'rejected'};
    ``stage`` names where it ended ('ok' | 'construct' | 'pairing_gate' |
    'promotion_lint')."""

    outcome: str
    stage: str
    diagnostic: str = ""
    failed_gate: int | None = None


async def _no_retrieve(_question) -> tuple[GroundingSpan, ...]:
    """A no-op retrieve_fn for the authored path: the draft ALWAYS carries the
    authored solution as its grounding, so ``validate_pair`` never re-grounds."""
    return ()


async def _find_authored_tier1_row_id(
    db: AsyncSession, *, concept_id: int, problem_code: str
) -> int | None:
    """The ingested Tier-1 ``apollo_concept_problems.id`` to flip on promote, keyed
    on the authored ``problem_code`` (``authored.<hash>``)."""
    return (
        await db.execute(
            select(ConceptProblem.id)
            .where(ConceptProblem.concept_id == concept_id)
            .where(ConceptProblem.problem_code == problem_code)
        )
    ).scalar_one_or_none()


async def provision_authored_problem(
    db: AsyncSession,
    neo,
    authored,
    *,
    search_space_id: int,
    ingest_concept_id: int,
    construct_chat_fn: Callable[..., str],
    judge_fn: Callable[..., str],
    tag_chat_fn: Callable[[str], str],
    embed_fn: Callable[[str], Sequence[float]],
    run: IngestRun | None = None,
) -> AuthoredProvisionResult:
    """Construct -> faithfulness -> tag/mint -> content-gated promote for ONE
    already-ingested authored problem. Returns an ``AuthoredProvisionResult``.

    A per-candidate rejection (un-constructable graph, a failed faithfulness
    verdict, or a lint failure) is a CLEAN reject — never a run abort (AC #3). When
    ``run`` is supplied an ``apollo_rejected_problems`` row is written for the
    reject (mirroring ``_process_candidate``); otherwise the rejection is returned
    only. ``promote`` derives the applicable gates from the problem's own content —
    no subject profile."""
    # --- construct (three-completeness, universal vocab, authored grounding) --- #
    try:
        draft = await construct_authored_reference(authored, chat_fn=construct_chat_fn)
    except SolutionDraftError as exc:
        if run is not None:
            _record_rejection(
                db,
                run=run,
                rejected_stage="solution_draft",
                failed_gate=None,
                diagnostic=str(exc),
                concept_id=ingest_concept_id,
                payload={"reason": "authored_construct_error"},
            )
        return AuthoredProvisionResult(outcome="rejected", stage="construct", diagnostic=str(exc))

    # --- faithfulness against the AUTHORED solution (draft.grounding) --------- #
    verdict = await validate_pair(authored, draft, retrieve_fn=_no_retrieve, judge_fn=judge_fn)
    rej = rejection_from_verdict(verdict)
    if rej is not None:
        if run is not None:
            _record_rejection(
                db,
                run=run,
                rejected_stage="pairing_gate",
                failed_gate=None,
                diagnostic=rej.diagnostic,
                concept_id=ingest_concept_id,
                payload={"reason": rej.reason},
            )
        return AuthoredProvisionResult(
            outcome="rejected", stage="pairing_gate", diagnostic=rej.diagnostic
        )

    # --- tag/mint + content-gated promote ----------------------------------- #
    pair = build_authored_approved_pair(authored, draft, search_space_id=search_space_id)
    # A failed tag/mint (the LLM omits a concept_slug) or a blown cost budget is a
    # per-CANDIDATE reject — never a run abort (AC #3). Mirrors _process_candidate's
    # guard, except the authored path treats a TagMintError as a clean reject rather
    # than a per-document abort (one un-taggable authored problem must not sink a
    # batch of others).
    try:
        mint_plan = await tag_and_mint(db, pair, chat_fn=tag_chat_fn, embed_fn=embed_fn)
    except (TagMintError, CostBudgetExceeded) as exc:
        if run is not None:
            _record_rejection(
                db,
                run=run,
                rejected_stage="tag_mint",
                failed_gate=None,
                diagnostic=f"{type(exc).__name__}: {exc}",
                concept_id=ingest_concept_id,
                payload={"reason": "tag_mint_error"},
            )
        return AuthoredProvisionResult(outcome="rejected", stage="tag_mint", diagnostic=str(exc))

    concept_problem_id = await _find_authored_tier1_row_id(
        db, concept_id=ingest_concept_id, problem_code=authored.problem_code
    )
    if concept_problem_id is None:
        raise _PerDocumentError(stage="promote", error_class="MissingTier1Row")

    existing_problem_hashes = await _concept_dup_hashes(db, concept_id=mint_plan.concept_id)
    result: PromoteResult = await promote(
        db,
        neo,
        problem=pair.problem,
        mint_plan=mint_plan,
        search_space_id=search_space_id,
        concept_problem_id=concept_problem_id,
        existing_problem_hashes=existing_problem_hashes,
    )
    if not result.promoted:
        if run is not None:
            _record_rejection(
                db,
                run=run,
                rejected_stage="promotion_lint",
                failed_gate=result.failed_gate,
                diagnostic=result.diagnostic,
                concept_id=mint_plan.concept_id,
                payload={},
            )
        return AuthoredProvisionResult(
            outcome="rejected",
            stage="promotion_lint",
            diagnostic=result.diagnostic,
            failed_gate=result.failed_gate,
        )
    return AuthoredProvisionResult(outcome="promoted", stage="ok")


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
    _recompute_counts(run, scraped=scraped, promoted=promoted, rejected=rejected, merged=merged)
    run.status = status  # type: ignore[assignment]
    run.finished_at = _now()  # type: ignore[assignment]
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
