"""Shared real-PG seed helpers for the WU-3D curriculum-cutover tests.

These insert the course-scoped curriculum chain
    app.courses -> apollo_subjects -> apollo_concepts -> apollo_concept_problems
using only ORM/`text()` inserts on the function-scoped ``db_session`` fixture
(real pgvector container, savepoint rollback per test). No SQLite, no
filesystem reads. Immutable: every helper returns ids; no global state.

`app.courses` is part of ``Base.metadata`` (the ``db_session`` fixture
``create_all``s it), so a search-space row is a plain ``text()`` insert.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from apollo.persistence.models import Concept, ConceptProblem, Subject

_BERNOULLI_DIR = (
    Path(__file__).resolve().parents[2]
    / "subjects"
    / "fluid_mechanics"
    / "concepts"
    / "bernoulli_principle"
)
_PROBLEMS_DIR = _BERNOULLI_DIR / "problems"

# Minimal-but-valid payloads for the JSONB/TEXT concept columns. The pydantic
# validators (CanonicalSymbols / SolverHints / ForbiddenNamedLaws) must accept
# these when load_concept_definition re-validates them.
_MIN_CANONICAL_SYMBOLS: dict[str, Any] = {
    "symbols": ["P", "v"],
    "description": {"P": "pressure", "v": "velocity"},
}
_MIN_SOLVER_HINTS: dict[str, Any] = {"constants": {"g": 9.81}}
_MIN_FORBIDDEN: dict[str, Any] = {"named_laws": ["bernoulli"]}


def load_bernoulli_problem_payloads() -> list[dict[str, Any]]:
    """Every real bernoulli problem JSON (full payload, incl. reference_solution)."""
    return [
        json.loads(p.read_text(encoding="utf-8"))
        for p in sorted(_PROBLEMS_DIR.glob("problem_*.json"))
    ]


def load_bernoulli_concept_payloads() -> dict[str, Any]:
    """The real bernoulli concept JSONB/TEXT columns (full fidelity)."""
    return {
        "canonical_symbols": json.loads(
            (_BERNOULLI_DIR / "canonical_symbols.json").read_text(encoding="utf-8")
        ),
        "normalization_map": json.loads(
            (_BERNOULLI_DIR / "normalization_map.json").read_text(encoding="utf-8")
        ),
        "parser_prompt_template": (_BERNOULLI_DIR / "parser_prompt_template.md").read_text(
            encoding="utf-8"
        ),
        "solver_hints": json.loads(
            (_BERNOULLI_DIR / "solver_hints.json").read_text(encoding="utf-8")
        ),
        "forbidden_named_laws": json.loads(
            (_BERNOULLI_DIR / "forbidden_named_laws.json").read_text(encoding="utf-8")
        ),
        "concept_dag": json.loads(
            (_BERNOULLI_DIR / "concept_dag.json").read_text(encoding="utf-8")
        ),
    }


async def seed_search_space(db: AsyncSession) -> int:
    """Insert one app.courses row, return its id.

    name/slug/subject_name are NOT NULL (and slug is UNIQUE), so a unique slug is
    generated per call to let multiple courses coexist inside one test.
    """
    slug = f"course-{uuid.uuid4().hex[:12]}"
    return (
        await db.execute(
            text(
                "INSERT INTO app.courses "
                "(name, slug, subject_name, created_at, updated_at) "
                "VALUES (:name, :slug, :subject, now(), now()) RETURNING id"
            ),
            {"name": slug, "slug": slug, "subject": "Test Course"},
        )
    ).scalar_one()


async def seed_concept(
    db: AsyncSession,
    *,
    search_space_id: int,
    subject_slug: str,
    concept_slug: str,
    concept_payloads: dict[str, Any] | None = None,
) -> int:
    """Insert a subject + one concept under a course; return the concept_id."""
    subject = Subject(
        slug=subject_slug,
        display_name=subject_slug.replace("_", " ").title(),
        search_space_id=search_space_id,
    )
    db.add(subject)
    await db.flush()

    payloads = concept_payloads or {
        "canonical_symbols": _MIN_CANONICAL_SYMBOLS,
        "normalization_map": {"pressure": "P"},
        "parser_prompt_template": f"Template for {concept_slug}",
        "solver_hints": _MIN_SOLVER_HINTS,
        "forbidden_named_laws": _MIN_FORBIDDEN,
        "concept_dag": {},
    }
    concept = Concept(
        subject_id=subject.id,
        slug=concept_slug,
        display_name=concept_slug.replace("_", " ").title(),
        canonical_symbols=payloads["canonical_symbols"],
        normalization_map=payloads["normalization_map"],
        parser_prompt_template=payloads["parser_prompt_template"],
        solver_hints=payloads["solver_hints"],
        forbidden_named_laws=payloads["forbidden_named_laws"],
        concept_dag=payloads.get("concept_dag", {}),
    )
    db.add(concept)
    await db.flush()
    return int(concept.id)


def minimal_problem_payload(code: str = "p1", difficulty: str = "intro") -> dict[str, Any]:
    """A minimal ``apollo_concept_problems`` payload for the curriculum tests.

    ``seed_problems`` reads only ``id`` + ``difficulty`` off a payload, and
    ``list_course_concepts``' teachable-pool ``EXISTS`` filter never reads the
    payload at all, so these two keys suffice — no full ``Problem`` round-trip is
    exercised by the curriculum-loader tests (those that need the real shape pass
    ``load_bernoulli_problem_payloads()``)."""
    return {"id": code, "difficulty": difficulty}


async def seed_problems(
    db: AsyncSession,
    *,
    concept_id: int,
    payloads: Sequence[dict[str, Any]],
    tier: int = 2,  # WU-3B2a: seeded curriculum problems are teachable (Tier-2),
                    # mirroring migration 030's backfill of existing §8-seeded rows.
                    # The param lets the selector tests seed an explicit tier=1 row
                    # to prove the Tier-1 exclusion.
    quarantined_at: datetime | None = None,  # WU-3B2h: a tz-aware datetime stamps
                    # every seeded row as quarantined (NOT teachable); None (default)
                    # leaves the rows live. Lets the curriculum tests prove the
                    # quarantined_at-IS-NULL leg of the teachable-pool filter.
) -> list[str]:
    """Insert one apollo_concept_problems row per payload; return the problem_codes."""
    codes: list[str] = []
    for payload in payloads:
        code = payload["id"]
        db.add(
            ConceptProblem(
                concept_id=concept_id,
                problem_code=code,
                difficulty=payload["difficulty"],
                payload=payload,
                tier=tier,
                quarantined_at=quarantined_at,
            )
        )
        codes.append(code)
    await db.flush()
    return codes


async def problem_database_id(
    db: AsyncSession, *, concept_id: int, problem_code: str
) -> int:
    """Resolve an API-facing problem code to its persisted bigint identity."""
    return int(
        (
            await db.execute(
                select(ConceptProblem.id).where(
                    ConceptProblem.concept_id == concept_id,
                    ConceptProblem.problem_code == problem_code,
                )
            )
        ).scalar_one()
    )


async def seed_course(
    db: AsyncSession,
    *,
    subject_slug: str,
    concept_slug: str,
    problems: Sequence[dict[str, Any]],
    search_space_id: int | None = None,
    concept_payloads: dict[str, Any] | None = None,
) -> tuple[int, int, list[str]]:
    """Seed a full course curriculum chain in one call.

    Returns ``(search_space_id, concept_id, problem_codes)``.
    """
    sid = search_space_id if search_space_id is not None else await seed_search_space(db)
    concept_id = await seed_concept(
        db,
        search_space_id=sid,
        subject_slug=subject_slug,
        concept_slug=concept_slug,
        concept_payloads=concept_payloads,
    )
    codes = await seed_problems(db, concept_id=concept_id, payloads=list(problems))
    return sid, concept_id, codes
