"""DB-backed curriculum loader (WU-3D §8A runtime cutover).

The SELECTION path reads concepts from the ``apollo_concepts`` rows, scoped to a
course via ``apollo_subjects.search_space_id``, instead of globbing the
filesystem registry. The filesystem layout under ``apollo/subjects/<s>/concepts``
remains the AUTHORING source only — ``scripts/seed_apollo_concept_registry.py``
converts it to rows; this module is the runtime mirror of that read.

Async by design: every caller (session_init, the chat/done/next/lifecycle
handlers) already holds the request-scoped ``AsyncSession``, mirroring
``apollo/knowledge_graph/canon_projection.load_entity_specs(db, ...)``.

Immutable: a NEW ``ConceptDefinition`` is built from each row; the ORM row is
never mutated.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from apollo.persistence.models import Concept, ConceptProblem, Subject
from apollo.subjects import (
    CanonicalSymbols,
    ConceptDefinition,
    ForbiddenNamedLaws,
    SolverHints,
)

# A concept's problems come from list_problems_for_concept (DB), never the
# filesystem. ConceptDefinition.problems_dir is kept on the model for shape
# compatibility but points at this sentinel non-existent path so the only
# contract is `.exists() is False` — the runtime never globs it.
_NO_FILESYSTEM_PROBLEMS_DIR = Path("/__apollo_db_concept_no_fs__")


@dataclass(frozen=True)
class ConceptRow:
    """A candidate concept for a course (id + display name). The minimal shape
    concept_inference needs to pick among a course's concepts."""

    concept_id: int
    slug: str
    display_name: str


class ConceptNotFoundError(LookupError):
    """Raised when a concept_id has no apollo_concepts row (e.g. deleted course).

    Internal error — deliberately NOT registered as an HTTP handler. It only
    fires when a session's concept_id points at a deleted concept (which the
    ON DELETE RESTRICT FK should make impossible), so it surfaces loudly.
    """


async def list_course_concepts(db: AsyncSession, *, search_space_id: int) -> list[ConceptRow]:
    """The course's TEACHABLE concepts, scoped via ``apollo_subjects.search_space_id``.

    JOINs ``apollo_concepts`` to ``apollo_subjects`` on ``subject_id`` and filters
    by the course's ``search_space_id``, ordered by ``apollo_concepts.id``
    (deterministic). Returns ``[]`` when the course has no curriculum.

    A correlated ``EXISTS`` additionally drops any concept with NO teachable
    problem, using the EXACT predicate the downstream pool query
    (``overseer.problem_selector.list_problems_for_concept``) applies —
    ``ConceptProblem.tier == 2 AND quarantined_at IS NULL``. This keeps the
    inference candidate set and the selectable pool in lockstep: without it an
    autoprovisioned decoy concept (an empty ``provisional.inventory`` /
    tier-1-only / fully-quarantined concept) would be a candidate that
    ``infer_concept_id`` could lexically pick, only for ``select_problem`` to
    then raise ``PoolExhaustedError`` on its empty pool (the G6 fluids-grading
    409 block).
    """
    result = await db.execute(
        select(Concept.id, Concept.slug, Concept.display_name)
        .join(Subject, Concept.subject_id == Subject.id)
        .where(
            Subject.search_space_id == search_space_id,
            exists().where(
                ConceptProblem.concept_id == Concept.id,
                ConceptProblem.tier == 2,
                ConceptProblem.quarantined_at.is_(None),
            ),
        )
        .order_by(Concept.id)
    )
    return [
        ConceptRow(concept_id=row.id, slug=row.slug, display_name=row.display_name)
        for row in result.all()
    ]


@dataclass(frozen=True)
class RegisteredConcept:
    """One registered concept of a course's premade list (reversed provisioning).

    Unlike ``ConceptRow``/``list_course_concepts`` this is NOT filtered to
    teachable concepts — a fresh reversed-provisioning course has registered
    concepts and zero problems. The reserved provisional-inventory concept is
    excluded (it is a scrape seam, never a match target). ``description`` is
    the migration-038 column the closed-list matcher prompt renders.
    """

    concept_id: int
    slug: str
    display_name: str
    description: str


async def list_registered_concepts(
    db: AsyncSession, *, search_space_id: int
) -> list[RegisteredConcept]:
    """EVERY registered concept of a course (the concept matcher's closed list)."""
    from apollo.provisioning.scrape import PROVISIONAL_CONCEPT_SLUG

    result = await db.execute(
        select(Concept.id, Concept.slug, Concept.display_name, Concept.description)
        .join(Subject, Concept.subject_id == Subject.id)
        .where(
            Subject.search_space_id == search_space_id,
            Concept.slug != PROVISIONAL_CONCEPT_SLUG,
        )
        .order_by(Concept.id)
    )
    return [
        RegisteredConcept(
            concept_id=int(row.id),
            slug=str(row.slug),
            display_name=str(row.display_name),
            description=str(row.description or ""),
        )
        for row in result.all()
    ]


async def load_concept_definition(db: AsyncSession, *, concept_id: int) -> ConceptDefinition:
    """Build a ``ConceptDefinition`` from the ``apollo_concepts`` row's columns.

    Re-validates the JSONB/TEXT columns through the same pydantic models
    ``load_concept`` used. ``subject_id``/``concept_id`` fields are set to the
    row's slug values for display continuity; ``problems_dir`` is a sentinel
    non-existent path (problems come from ``list_problems_for_concept``). Raises
    ``ConceptNotFoundError`` if the row is absent.
    """
    concept = (
        await db.execute(select(Concept).where(Concept.id == concept_id))
    ).scalar_one_or_none()
    if concept is None:
        raise ConceptNotFoundError(f"no apollo_concepts row for concept_id={concept_id}")

    subject = (
        await db.execute(select(Subject).where(Subject.id == concept.subject_id))
    ).scalar_one_or_none()
    subject_slug = str(subject.slug) if subject is not None else str(concept.subject_id)

    return ConceptDefinition(
        subject_id=subject_slug,
        concept_id=str(concept.slug),
        canonical_symbols=CanonicalSymbols.model_validate(concept.canonical_symbols),
        normalization_map=dict(concept.normalization_map),
        parser_prompt_template=str(concept.parser_prompt_template),
        solver_hints=SolverHints.model_validate(concept.solver_hints),
        forbidden_named_laws=ForbiddenNamedLaws.model_validate(concept.forbidden_named_laws),
        problems_dir=_NO_FILESYSTEM_PROBLEMS_DIR,
    )


__all__ = [
    "ConceptRow",
    "ConceptNotFoundError",
    "list_course_concepts",
    "load_concept_definition",
]
