"""Seeder: ingest the filesystem concept registry into Postgres.

Walks `apollo/subjects/<subject>/concepts/<concept>/...` and writes:
  apollo_subjects   — one row per subject directory
  apollo_concepts   — one row per concept directory; columns are the JSON /
                      Markdown payloads verbatim
  apollo_concept_problems — one row per problems/problem_*.json

Idempotent: re-running updates existing rows by (subject_slug) /
(subject_id, concept_slug) / (concept_id, problem_code) keys.

Usage:
    python -m scripts.seed_apollo_concept_registry [--dry-run]

This is a one-shot data-import tool. It IS allowed to know specific
subject/concept slugs because it's reading them off disk; the runtime
(handlers, parser, coverage, misconception inference) only ever sees
concept_id (FK BIGINT). After running this, the on-disk files become
deletable.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# Make the package importable when run as `python -m scripts....`
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from apollo.persistence.models import (  # noqa: E402
    Concept,
    ConceptProblem,
    Subject,
)

_LOG = logging.getLogger(__name__)
_REGISTRY_ROOT = Path(__file__).resolve().parents[1] / "apollo" / "subjects"


def _read_json(path: Path) -> Any:
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _read_text(path: Path) -> str:
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8")


def _human_name(slug: str) -> str:
    return slug.replace("_", " ").title()


async def _upsert_subject(session: AsyncSession, slug: str) -> int:
    row = (
        await session.execute(select(Subject).where(Subject.slug == slug))
    ).scalar_one_or_none()
    if row is not None:
        row.display_name = _human_name(slug)
        return row.id

    new = Subject(slug=slug, display_name=_human_name(slug))
    session.add(new)
    await session.flush()
    return new.id


async def _upsert_concept(
    session: AsyncSession,
    *,
    subject_id: int,
    slug: str,
    payloads: dict[str, Any],
) -> int:
    row = (
        await session.execute(
            select(Concept)
            .where(Concept.subject_id == subject_id)
            .where(Concept.slug == slug)
        )
    ).scalar_one_or_none()

    if row is None:
        row = Concept(subject_id=subject_id, slug=slug, display_name=_human_name(slug))
        session.add(row)

    row.display_name = _human_name(slug)
    row.canonical_symbols = payloads["canonical_symbols"]
    row.normalization_map = payloads["normalization_map"]
    row.parser_prompt_template = payloads["parser_prompt_template"]
    row.solver_hints = payloads["solver_hints"]
    row.forbidden_named_laws = payloads["forbidden_named_laws"]
    row.concept_dag = payloads["concept_dag"]

    await session.flush()
    return row.id


async def _upsert_problem(
    session: AsyncSession,
    *,
    concept_id: int,
    problem_code: str,
    difficulty: str,
    payload: dict[str, Any],
) -> None:
    row = (
        await session.execute(
            select(ConceptProblem)
            .where(ConceptProblem.concept_id == concept_id)
            .where(ConceptProblem.problem_code == problem_code)
        )
    ).scalar_one_or_none()

    if row is None:
        row = ConceptProblem(
            concept_id=concept_id,
            problem_code=problem_code,
            difficulty=difficulty,
            payload=payload,
        )
        session.add(row)
    else:
        row.difficulty = difficulty
        row.payload = payload


def _scan_registry() -> list[tuple[str, str, dict[str, Any], list[dict[str, Any]]]]:
    """Walk the filesystem registry. Returns
    [(subject_slug, concept_slug, payloads_dict, problems_list)].

    Errors out loudly if a required file is missing — a partially-populated
    concept row would be worse than a clear failure.
    """
    out: list[tuple[str, str, dict[str, Any], list[dict[str, Any]]]] = []
    for subject_dir in sorted(_REGISTRY_ROOT.iterdir()):
        if not subject_dir.is_dir() or subject_dir.name.startswith(("_", ".")):
            continue
        concepts_root = subject_dir / "concepts"
        if not concepts_root.is_dir():
            continue
        for concept_dir in sorted(concepts_root.iterdir()):
            if not concept_dir.is_dir() or concept_dir.name.startswith("."):
                continue
            payloads = {
                "canonical_symbols": _read_json(concept_dir / "canonical_symbols.json"),
                "normalization_map": _read_json(concept_dir / "normalization_map.json"),
                "parser_prompt_template": _read_text(concept_dir / "parser_prompt_template.md"),
                "solver_hints": _read_json(concept_dir / "solver_hints.json"),
                "forbidden_named_laws": _read_json(concept_dir / "forbidden_named_laws.json"),
                "concept_dag": _read_json(concept_dir / "concept_dag.json"),
            }
            problems_dir = concept_dir / "problems"
            problems: list[dict[str, Any]] = []
            if problems_dir.is_dir():
                for p in sorted(problems_dir.glob("problem_*.json")):
                    payload = json.loads(p.read_text(encoding="utf-8"))
                    problems.append({
                        "code": payload.get("id") or p.stem,
                        "difficulty": payload.get("difficulty", "intro"),
                        "payload": payload,
                    })
            out.append((subject_dir.name, concept_dir.name, payloads, problems))
    return out


async def seed(database_url: str, *, dry_run: bool = False) -> dict[str, int]:
    """Run the seeder. Returns a small stats dict for logging."""
    engine = create_async_engine(database_url)
    Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    stats = {"subjects": 0, "concepts": 0, "problems": 0}
    discovered = _scan_registry()

    async with Session() as session:
        for subject_slug, concept_slug, payloads, problems in discovered:
            subject_id = await _upsert_subject(session, subject_slug)
            stats["subjects"] += 1
            concept_id = await _upsert_concept(
                session,
                subject_id=subject_id,
                slug=concept_slug,
                payloads=payloads,
            )
            stats["concepts"] += 1
            for prob in problems:
                await _upsert_problem(
                    session,
                    concept_id=concept_id,
                    problem_code=prob["code"],
                    difficulty=prob["difficulty"],
                    payload=prob["payload"],
                )
                stats["problems"] += 1

        if dry_run:
            await session.rollback()
            _LOG.info("seed_dry_run", extra={"stats": stats})
        else:
            await session.commit()
            _LOG.info("seed_committed", extra={"stats": stats})

    await engine.dispose()
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", default=None,
                        help="async PostgreSQL URL (defaults to env DATABASE_URL)")
    parser.add_argument("--dry-run", action="store_true",
                        help="walk + parse but rollback transaction")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    import os
    db_url = args.database_url or os.environ.get("DATABASE_URL")
    if not db_url:
        print("ERROR: pass --database-url or set DATABASE_URL", file=sys.stderr)
        return 2

    stats = asyncio.run(seed(db_url, dry_run=args.dry_run))
    print(f"seeded: {stats}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
