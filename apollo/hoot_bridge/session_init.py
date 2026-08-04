"""Apollo session initialization: Hoot handoff + standalone entry.

init_session_from_hoot — the original Hoot→Apollo handoff. The transcript is
used for exactly one thing: infer_concept_id picks the concept. Everything
else is shared with the standalone path.

init_session_direct — WU-E2E standalone entry (2026-07-07 spec). The student
explicitly picks concept_id (validated against the course's teachable set) and
optionally a specific problem_id (validated against the concept's teachable
pool). No LLM call, no transcript.

Both raise NoMatchingConceptError / PoolExhaustedError (409) —
init_session_direct additionally raises ProblemNotFoundError (404).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from apollo.errors import NoMatchingConceptError, ProblemNotFoundError
from apollo.overseer.concept_inference import infer_concept_id
from apollo.overseer.problem_selector import (
    list_problems_for_concept,
    select_problem_personalized,
)
from apollo.persistence.models import (
    ProblemAttempt,
    SessionPhase,
    SessionStatus,
    TutoringSession,
)
from apollo.schemas.problem import Problem
from apollo.subjects.curriculum_db import ConceptRow, list_course_concepts
from config.contracts import BundleSnippet
from config.settings import (
    get_citation_label,
    interaction1_enabled,
    interaction_allowed_for_concept,
)
from database.session import _get_session_factory
from retrieval import retrieve_for_question

_ALLOWED_DIFFICULTIES = {"intro", "standard", "hard"}
_GROUNDING_TOP_K = 8
_GROUNDING_TOKEN_BUDGET = 2500
_GROUNDING_RETRIEVAL_VERSION = "retrieve_for_question:v1"
_SOLUTION_DOC_KINDS = frozenset(
    {
        "answer_key",
        "authored_solution",
        "solution",
        "solution_manual",
        "solutions",
    }
)
_LOG = logging.getLogger(__name__)
_GROUNDING_TASKS: set[asyncio.Task[None]] = set()


def _concept_grounding_terms(
    candidates: list[ConceptRow],
    concept_id: int,
    problem: Problem,
) -> tuple[str, list[str]]:
    concept = next((item for item in candidates if item.concept_id == concept_id), None)
    if concept is None:
        return problem.concept_id, []

    title = concept.display_name.strip() or concept.slug
    aliases = [
        alias for alias in (concept.slug.strip(),) if alias and alias.casefold() != title.casefold()
    ]
    return title, aliases


def _is_solution_bearing(snippet: BundleSnippet) -> bool:
    metadata = snippet.metadata or {}
    authored_role = str(metadata.get("authored_role") or "").strip().casefold()
    if authored_role == "solution":
        return True

    return any(
        str(metadata.get(key) or "").strip().casefold() in _SOLUTION_DOC_KINDS
        for key in ("doc_kind", "document_kind", "document_role", "kind", "material_kind")
    )


async def _build_grounding_bundle(
    db: AsyncSession,
    *,
    session: TutoringSession,
    search_space_id: int,
    concept_title: str,
    concept_aliases: list[str],
    problem: Problem,
) -> None:
    """Best-effort retrieval and persistence after the session transaction."""
    try:
        snippets, diag = await retrieve_for_question(
            query=f"{concept_title}. {problem.problem_text}",
            keywords=[concept_title, *concept_aliases],
            search_space_id=search_space_id,
            db_session=db,
            top_k=_GROUNDING_TOP_K,
            token_budget=_GROUNDING_TOKEN_BUDGET,
            citation_label=get_citation_label(),
        )
        student_safe = [snippet for snippet in snippets if not _is_solution_bearing(snippet)]
        dropped = len(snippets) - len(student_safe)
        if dropped:
            _LOG.warning(
                "Dropped %d solution-bearing snippets from Apollo session %s grounding",
                dropped,
                session.id,
            )
        if not student_safe:
            return

        session.grounding_bundle = {
            "snippets": [asdict(snippet) for snippet in student_safe],
            "diag": {
                **diag,
                "solution_snippets_dropped": dropped,
                "student_safe_snippets": len(student_safe),
            },
            "built_at": datetime.now(UTC).isoformat(),
            "retrieval_version": _GROUNDING_RETRIEVAL_VERSION,
        }
        await db.commit()
    except Exception:
        _LOG.exception(
            "Apollo grounding retrieval failed for session %s; continuing without bundle",
            session.id,
        )
        try:
            await db.rollback()
        except Exception:  # pragma: no cover - defensive cleanup must not break session creation
            _LOG.exception("Failed to roll back Apollo grounding transaction")


async def _build_grounding_bundle_in_background(
    *,
    session_id: int,
    search_space_id: int,
    concept_title: str,
    concept_aliases: list[str],
    problem: Problem,
) -> None:
    """Build grounding with a session that outlives the request dependency."""
    try:
        session_factory = _get_session_factory()
        async with session_factory() as db:
            session = await db.get(TutoringSession, session_id)
            if session is None:
                _LOG.error(
                    "Apollo grounding session %s disappeared before background retrieval",
                    session_id,
                )
                return
            await _build_grounding_bundle(
                db,
                session=session,
                search_space_id=search_space_id,
                concept_title=concept_title,
                concept_aliases=concept_aliases,
                problem=problem,
            )
    except Exception:
        _LOG.exception(
            "Apollo grounding background task failed for session %s; continuing without bundle",
            session_id,
        )


def _schedule_grounding_bundle(
    *,
    session_id: int,
    search_space_id: int,
    concept_title: str,
    concept_aliases: list[str],
    problem: Problem,
) -> None:
    task = asyncio.create_task(
        _build_grounding_bundle_in_background(
            session_id=session_id,
            search_space_id=search_space_id,
            concept_title=concept_title,
            concept_aliases=concept_aliases,
            problem=problem,
        )
    )
    _GROUNDING_TASKS.add(task)
    task.add_done_callback(_GROUNDING_TASKS.discard)


async def _create_session_with_problem(
    db: AsyncSession,
    *,
    user_id: str,
    search_space_id: int,
    concept_id: int,
    difficulty: str,
    problem: Problem,
    concept_title: str,
    concept_aliases: list[str],
) -> dict[str, Any]:
    """Shared tail of both entries: end any active session, create the
    TEACHING session + first attempt, commit, return the FE payload.
    Moved verbatim from init_session_from_hoot (WU-3D shape unchanged)."""
    await db.execute(
        update(TutoringSession)
        .where(
            TutoringSession.user_id == user_id,
            TutoringSession.search_space_id == search_space_id,
            TutoringSession.status == SessionStatus.active.value,
        )
        .values(status=SessionStatus.ended.value)
    )
    await db.flush()

    session = TutoringSession(
        user_id=user_id,
        search_space_id=search_space_id,
        concept_id=concept_id,
        status=SessionStatus.active.value,
        phase=SessionPhase.TEACHING.value,
        current_problem_id=problem.database_id,
    )
    db.add(session)
    await db.flush()

    attempt = ProblemAttempt(
        session_id=session.id,
        course_id=search_space_id,
        user_id=user_id,
        problem_id=problem.database_id,
        difficulty=difficulty,
    )
    db.add(attempt)
    await db.flush()
    attempt_id = attempt.id
    await db.commit()

    if interaction1_enabled() and interaction_allowed_for_concept(problem.concept_id):
        _schedule_grounding_bundle(
            session_id=session.id,
            search_space_id=search_space_id,
            concept_title=concept_title,
            concept_aliases=concept_aliases,
            problem=problem,
        )

    return {
        "session_id": session.id,
        "attempt_id": attempt_id,
        "problem": {
            "id": problem.id,
            "concept_id": problem.concept_id,
            "difficulty": problem.difficulty,
            "problem_text": problem.problem_text,
            "given_values": problem.given_values,
            "target_unknown": problem.target_unknown,
        },
    }


async def init_session_from_hoot(
    *,
    db: AsyncSession,
    user_id: str,
    search_space_id: int,
    hoot_transcript: str,
    difficulty: str,
) -> dict[str, Any]:
    if difficulty not in _ALLOWED_DIFFICULTIES:
        raise ValueError(
            f"unknown difficulty {difficulty!r}; expected one of {sorted(_ALLOWED_DIFFICULTIES)}"
        )

    candidates = await list_course_concepts(db, search_space_id=search_space_id)
    concept_id = await asyncio.to_thread(
        infer_concept_id,
        transcript=hoot_transcript,
        candidates=candidates,
    )

    problem = await select_problem_personalized(
        db,
        user_id=user_id,
        search_space_id=search_space_id,
        concept_id=concept_id,
        difficulty=difficulty,
        attempted_ids=[],
    )
    concept_title, concept_aliases = _concept_grounding_terms(candidates, concept_id, problem)
    return await _create_session_with_problem(
        db,
        user_id=user_id,
        search_space_id=search_space_id,
        concept_id=concept_id,
        difficulty=difficulty,
        problem=problem,
        concept_title=concept_title,
        concept_aliases=concept_aliases,
    )


async def init_session_direct(
    *,
    db: AsyncSession,
    user_id: str,
    search_space_id: int,
    concept_id: int,
    difficulty: str,
    problem_id: str | None = None,
) -> dict[str, Any]:
    if difficulty not in _ALLOWED_DIFFICULTIES:
        raise ValueError(
            f"unknown difficulty {difficulty!r}; expected one of {sorted(_ALLOWED_DIFFICULTIES)}"
        )

    candidates = await list_course_concepts(db, search_space_id=search_space_id)
    if concept_id not in {c.concept_id for c in candidates}:
        raise NoMatchingConceptError(
            f"concept_id={concept_id} is not teachable in course {search_space_id}"
        )

    if problem_id is not None:
        pool = await list_problems_for_concept(
            db, concept_id=concept_id, search_space_id=search_space_id
        )
        problem = next((p for p in pool if p.id == problem_id), None)
        if problem is None:
            raise ProblemNotFoundError(problem_id=problem_id, concept_id=concept_id)
    else:
        problem = await select_problem_personalized(
            db,
            user_id=user_id,
            search_space_id=search_space_id,
            concept_id=concept_id,
            difficulty=difficulty,
            attempted_ids=[],
        )

    concept_title, concept_aliases = _concept_grounding_terms(candidates, concept_id, problem)
    return await _create_session_with_problem(
        db,
        user_id=user_id,
        search_space_id=search_space_id,
        concept_id=concept_id,
        difficulty=difficulty,
        problem=problem,
        concept_title=concept_title,
        concept_aliases=concept_aliases,
    )
