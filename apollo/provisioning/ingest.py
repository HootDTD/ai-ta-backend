"""Subject-fluid Apollo Stage-1 — load a PROFESSOR-AUTHORED problem set into
Tier-1 inventory (REPLACES the textbook scrape as the Stage-1 entry).

Where ``scrape.py`` ran an LLM over a textbook's chunks (deleted from scope — see
the design spec §1), this unit ingests a STRUCTURED authored problem set: one
problem per record, every field explicit, never re-introducing extraction loss.
Each record becomes an ``AuthoredProblem`` (statement + optional solution +
optional worked procedure), classified by **completeness** into one of three
cases:

  * ``worked``      — statement + solution + worked procedure (structure it).
  * ``answer_only`` — statement + final answer, no procedure (reconstruct it,
                      anchored to the known answer — Stage 5).
  * ``none``        — statement only (generate it; flagged lower-confidence).

It then:
  1. writes each authored problem as a **Tier-1** ``apollo_concept_problems`` row
     (``tier=1`` EXPLICIT — the §8B safety trap: the ORM default is 2/teachable);
  2. **commits independently** of the downstream find/generate/promote stages.

The independent commit is the load-bearing fix for the write-then-rollback hazard
(design spec §1/§5/§8): the legacy orchestrator committed ONCE at run end, so an
interrupted run lost every ingested problem. Here the durable inventory + the
detected profile persist the instant ingest finishes, regardless of what a later
candidate does.

Idempotency mirrors ``scrape.write_tier1_problems``: a deterministic,
content-derived ``problem_code`` (``authored.<statement_hash>``) keyed on the
existing ``(concept_id, problem_code)`` uniqueness, so a re-ingest inserts ZERO
rows. Fail-soft per record: a malformed record (no statement) is DROPPED and
counted — never a half-written row, never a run abort.

NO network, NO LLM in this unit: the detection probe is a pure heuristic; the
caller passes already-structured records.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apollo.persistence.models import ConceptProblem
from apollo.schemas.problem import Difficulty

_LOG = logging.getLogger(__name__)

__all__ = [
    "AuthoredProblem",
    "Completeness",
    "IngestResult",
    "classify_completeness",
    "load_authored_problems",
    "authored_problem_code",
    "write_authored_tier1_problems",
    "ingest_authored_problems",
]

Completeness = Literal["worked", "answer_only", "none"]

# Authored Tier-1 rows are explicitly authored content (not a textbook scrape), so
# they carry solution_source='authored' from the start; promote.py keeps it
# (it only defaults solution_source when the row has none).
_AUTHORED_SOLUTION_SOURCE = "authored"


def _normalize(text: str) -> str:
    """Whitespace-collapsed, stripped, lowercased — so two statements differing
    only by whitespace/case hash IDENTICALLY (idempotency key is content-stable).
    Mirrors ``scrape._normalize``."""
    return re.sub(r"\s+", " ", text).strip().lower()


def authored_problem_code(statement: str) -> str:
    """Deterministic, content-derived ``problem_code`` (``authored.<sha256[:32]>``)
    so a re-ingest of the same statement is a no-op against the existing
    ``(concept_id, problem_code)`` uniqueness — NO new index, NO migration."""
    digest = hashlib.sha256(_normalize(statement).encode("utf-8")).hexdigest()
    return f"authored.{digest[:32]}"


def classify_completeness(solution: Any, worked_procedure: Any) -> Completeness:
    """Three-completeness classification (design spec §4.2):

    worked      = a non-empty solution AND a non-empty worked procedure;
    answer_only = a non-empty solution, no procedure;
    none        = no solution.
    """
    has_solution = bool(solution) and bool(str(solution).strip())
    has_procedure = bool(worked_procedure)
    if has_solution and has_procedure:
        return "worked"
    if has_solution:
        return "answer_only"
    return "none"


class AuthoredProblem(BaseModel):
    """One professor-authored problem, pre-Tier-1-write. ``statement`` is the only
    required field; ``solution`` / ``worked_procedure`` are optional and drive the
    completeness classification. ``given_values`` / ``target_unknown`` are optional
    per subject profile (a prose argument carries neither)."""

    problem_code: str = Field(min_length=1)
    concept_slug: str = Field(min_length=1)
    statement: str = Field(min_length=1)
    difficulty: Difficulty = "standard"
    solution: str | None = None
    worked_procedure: list[dict[str, Any]] | None = None
    given_values: dict[str, float] = Field(default_factory=dict)
    target_unknown: str = ""
    completeness: Completeness

    @property
    def problem_text(self) -> str:
        """Question-shape alias so an ``AuthoredProblem`` can be passed straight to
        the (frozen) ``validate_pair`` faithfulness gate as the ``question``."""
        return self.statement

    def to_problem_dict(self, reference_solution: list[dict]) -> dict[str, Any]:
        """A ``Problem``-validatable dict from this authored problem + a constructed
        ``reference_solution``. The id is the authored ``problem_code`` so the
        promoted payload id matches the Tier-1 inventory row."""
        return {
            "id": self.problem_code,
            "concept_id": self.concept_slug,
            "difficulty": self.difficulty,
            "problem_text": self.statement,
            "given_values": dict(self.given_values),
            "target_unknown": self.target_unknown,
            "reference_solution": reference_solution,
        }

    def probe_view(self) -> dict[str, Any]:
        """The dict the profile-neutral detection probe reads (statement/solution
        text + numeric givens + typed worked-procedure steps)."""
        return {
            "statement": self.statement,
            "solution": self.solution,
            "given_values": dict(self.given_values),
            "worked_procedure": self.worked_procedure or [],
        }

    def tier1_payload(self) -> dict[str, Any]:
        """The Tier-1 inventory payload. Carries the authored ground truth
        (solution + worked procedure + completeness) so Stage-5 construction can
        build the reference graph from the professor's solution rather than RAG."""
        return {
            "id": self.problem_code,
            "concept_id": self.concept_slug,
            "difficulty": self.difficulty,
            "problem_text": self.statement,
            "given_values": dict(self.given_values),
            "target_unknown": self.target_unknown,
            "authored": {
                "completeness": self.completeness,
                "solution": self.solution,
                "worked_procedure": self.worked_procedure,
            },
        }


def _coerce_authored(raw: Any, *, default_concept_slug: str) -> AuthoredProblem | None:
    """Build one ``AuthoredProblem`` from a structured input record, classifying
    completeness. Returns ``None`` (a fail-soft DROP) on any validation error (a
    record with no statement, an out-of-range difficulty, …)."""
    if not isinstance(raw, Mapping):
        return None
    statement = str(raw.get("statement") or raw.get("problem_text") or "").strip()
    if not statement:
        return None
    solution = raw.get("solution")
    worked_procedure = raw.get("worked_procedure")
    try:
        return AuthoredProblem(
            problem_code=str(raw.get("problem_code") or authored_problem_code(statement)),
            concept_slug=str(raw.get("concept_slug") or default_concept_slug),
            statement=statement,
            difficulty=raw.get("difficulty", "standard"),
            solution=(str(solution) if solution is not None else None),
            worked_procedure=(
                list(worked_procedure) if isinstance(worked_procedure, list) else None
            ),
            given_values=dict(raw.get("given_values") or {}),
            target_unknown=str(raw.get("target_unknown") or ""),
            completeness=classify_completeness(solution, worked_procedure),
        )
    except (ValidationError, ValueError, TypeError):
        return None


def load_authored_problems(
    records: Sequence[Mapping[str, Any]],
    *,
    default_concept_slug: str,
) -> tuple[list[AuthoredProblem], int]:
    """Parse a structured authored problem set into ``AuthoredProblem`` records.

    Returns ``(problems, n_dropped)``. Fail-soft: a record with no statement (or
    one failing validation) is dropped and counted — the rest of the set still
    loads (never a run abort)."""
    problems: list[AuthoredProblem] = []
    dropped = 0
    for raw in records:
        problem = _coerce_authored(raw, default_concept_slug=default_concept_slug)
        if problem is None:
            dropped += 1
            continue
        problems.append(problem)
    return problems, dropped


async def write_authored_tier1_problems(
    db: AsyncSession,
    problems: Sequence[AuthoredProblem],
    *,
    concept_id: int,
    search_space_id: int,
) -> int:
    """Persist authored problems as Tier-1 inventory rows. Returns the number of
    rows ACTUALLY inserted (0 on a full re-run).

    Mirrors ``scrape.write_tier1_problems``: ``tier=1`` EXPLICIT (never the
    teachable ORM default), content-derived ``problem_code``, denormalized
    ``search_space_id``, and a SELECT-then-skip idempotency guard on
    ``(concept_id, problem_code)``. ``solution_source='authored'`` is stamped here
    (promote keeps it). Flushes; the COMMIT is the caller's (``ingest_authored_problems``
    commits independently)."""
    inserted = 0
    for problem in problems:
        existing = (
            await db.execute(
                select(ConceptProblem.id)
                .where(ConceptProblem.concept_id == concept_id)
                .where(ConceptProblem.problem_code == problem.problem_code)
            )
        ).scalar_one_or_none()
        if existing is not None:
            continue

        db.add(
            ConceptProblem(
                concept_id=concept_id,
                problem_code=problem.problem_code,
                difficulty=problem.difficulty,
                payload=problem.tier1_payload(),
                tier=1,  # EXPLICIT: never inherit the teachable default (2)
                solution_source=_AUTHORED_SOLUTION_SOURCE,
                provenance={"source": "authored", "completeness": problem.completeness},
                search_space_id=search_space_id,
            )
        )
        inserted += 1

    await db.flush()
    return inserted


@dataclass(frozen=True)
class IngestResult:
    """Immutable aggregate of one authored-set ingest."""

    n_loaded: int
    n_written: int
    n_dropped: int
    completeness_counts: dict[str, int]


async def ingest_authored_problems(
    db: AsyncSession,
    records: Sequence[Mapping[str, Any]],
    *,
    subject_id: int,
    concept_id: int,
    search_space_id: int,
    default_concept_slug: str = "provisional.inventory",
    commit: bool = True,
) -> IngestResult:
    """Stage-1 entry for subject-agnostic Apollo: load -> classify -> Tier-1 write
    -> COMMIT INDEPENDENTLY.

    The independent commit (``commit=True``, the default) is the write-then-rollback
    fix: the ingested inventory is durable the moment ingest returns, so a later
    downstream failure can never roll it back. Pass ``commit=False`` to compose
    ingest inside a caller-owned transaction (e.g. a test that asserts pre-commit
    state). ``subject_id`` is retained for course-scoping / logging; gate
    applicability is now content-derived at promote time, so no subject profile is
    detected or persisted here."""
    problems, dropped = load_authored_problems(records, default_concept_slug=default_concept_slug)

    n_written = await write_authored_tier1_problems(
        db, problems, concept_id=concept_id, search_space_id=search_space_id
    )

    if commit:
        # INDEPENDENT commit — the durable ingest output (inventory) persists
        # regardless of the downstream find/generate/promote stages.
        await db.commit()

    completeness_counts: dict[str, int] = {"worked": 0, "answer_only": 0, "none": 0}
    for problem in problems:
        completeness_counts[problem.completeness] += 1

    _LOG.info(
        "provisioning_ingest_authored",
        extra={
            "event": "provisioning_ingest_authored",
            "subject_id": subject_id,
            "concept_id": concept_id,
            "n_loaded": len(problems),
            "n_written": n_written,
            "n_dropped": dropped,
            "completeness_counts": completeness_counts,
        },
    )

    return IngestResult(
        n_loaded=len(problems),
        n_written=n_written,
        n_dropped=dropped,
        completeness_counts=completeness_counts,
    )
