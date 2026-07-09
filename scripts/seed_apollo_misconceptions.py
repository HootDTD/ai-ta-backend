"""Seeder: ``apollo_misconceptions`` TABLE bank (migration 019) for one
subject's concept(s), from the on-disk ``misconceptions.json`` authoring
source.

Layers on TOP of ``scripts/seed_apollo_concept_registry.py`` (which must have
run first to create the ``apollo_concepts`` rows this reads). Distinct from
``scripts/seed_apollo_learner_model.py``, which mints ``kind='misconception'``
``apollo_kg_entities`` rows from the SAME source file — that is a different
store consumed by grading's opposes-link graph, not the runtime
misconception-inference bank / soundness-axis bank this script seeds. See
``apollo/persistence/misconception_bank_seed.py`` module docstring for the
two-store distinction.

Course-scoped (resolves the subject via ``apollo_subjects.search_space_id``)
and idempotent (``upsert_entry`` is keyed on ``(concept_id, code)``, migration
019's UNIQUE constraint). Embeds each ``description`` via ``embed_text``
(OpenAI, text-embedding-3-large) unless ``--no-embeddings`` is passed (rows
still insert with a NULL ``description_embedding`` — a bank exists for the
soundness-applicable D5/D6 gate, but the embedding-match retrieval path
degrades to "no candidates" for that row until a later embedding backfill).

Usage:
    python -m scripts.seed_apollo_misconceptions [--database-url URL]
        --subject-slug SLUG [--concept-slug SLUG] [--search-space-id N]
        [--dry-run] [--no-embeddings] [-v]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# Make the package importable when run as `python -m scripts....`
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from apollo.overseer.misconception_bank import upsert_entry  # noqa: E402
from apollo.persistence.misconception_bank_seed import (  # noqa: E402
    MisconceptionBankSpec,
    misconceptions_json_to_bank_specs,
)
from apollo.persistence.models import Concept, Subject  # noqa: E402

_LOG = logging.getLogger(__name__)

_SUBJECTS_ROOT = Path(__file__).resolve().parents[1] / "apollo" / "subjects"


class SeedError(RuntimeError):
    """Raised when the DB is missing a prerequisite row (registry not seeded)."""


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _concept_dir(subject_slug: str, concept_slug: str) -> Path:
    return _SUBJECTS_ROOT / subject_slug / "concepts" / concept_slug


async def _resolve_search_space_id(session: AsyncSession, search_space_id: int | None) -> int:
    if search_space_id is not None:
        return search_space_id
    resolved = (
        await session.execute(text("SELECT MIN(id) FROM aita_search_spaces"))
    ).scalar_one_or_none()
    if resolved is None:
        raise SeedError("no aita_search_spaces rows — seed a course before the misconception bank")
    return resolved


async def _resolve_concepts(
    session: AsyncSession,
    *,
    subject_slug: str,
    concept_slug: str | None,
    search_space_id: int | None,
) -> list[tuple[int, str]]:
    """Resolve target ``(concept_id, concept_slug)`` pairs for one course.

    Mirrors ``seed_apollo_learner_model._resolve_concepts`` (same DB shape;
    no ``source_subject_slug`` override here since the misconceptions.json
    source dir is always keyed by the DB subject slug for the campaign's
    two seeded incumbents).
    """
    space_id = await _resolve_search_space_id(session, search_space_id)

    subject_id = (
        await session.execute(
            select(Subject.id)
            .where(Subject.search_space_id == space_id)
            .where(Subject.slug == subject_slug)
        )
    ).scalar_one_or_none()
    if subject_id is None:
        raise SeedError(
            f"no '{subject_slug}' subject for search_space_id={space_id}; "
            "run scripts.seed_apollo_concept_registry first"
        )

    stmt = select(Concept.id, Concept.slug).where(Concept.subject_id == subject_id)
    if concept_slug is not None:
        stmt = stmt.where(Concept.slug == concept_slug)
    rows = (await session.execute(stmt.order_by(Concept.slug))).all()

    if not rows:
        scope = f"concept '{concept_slug}'" if concept_slug else "any concept"
        raise SeedError(
            f"no {scope} under subject '{subject_slug}' for search_space_id={space_id}; "
            "run scripts.seed_apollo_concept_registry first"
        )
    return [(int(row_id), row_slug) for row_id, row_slug in rows]


def _embed(spec: MisconceptionBankSpec, *, embed: bool) -> list[float] | None:
    if not embed or not spec.description:
        return None
    from indexing.document_embedder import embed_text

    return embed_text(spec.description)


async def _seed_concept(
    session: AsyncSession,
    *,
    concept_id: int,
    concept_slug: str,
    subject_slug: str,
    embed: bool,
) -> int:
    """Seed one concept's misconception bank rows. Returns the count upserted.
    A concept with no ``misconceptions.json`` on disk is a no-op (0), not an
    error — not every concept authors misconceptions."""
    misc_path = _concept_dir(subject_slug, concept_slug) / "misconceptions.json"
    if not misc_path.is_file():
        return 0

    specs = misconceptions_json_to_bank_specs(_read_json(misc_path))
    for spec in specs:
        await upsert_entry(
            session,
            concept_id=concept_id,
            code=spec.code,
            description=spec.description,
            description_embedding=_embed(spec, embed=embed),
            confusion_pair_a=spec.confusion_pair_a,
            confusion_pair_b=spec.confusion_pair_b,
            trigger_phrases=list(spec.trigger_phrases),
            probe_question=spec.probe_question,
            rt_steps=list(spec.rt_steps),
            opposes=spec.opposes,
        )
    return len(specs)


async def seed(
    database_url: str,
    *,
    subject_slug: str,
    concept_slug: str | None = None,
    search_space_id: int | None = None,
    dry_run: bool = False,
    embed: bool = True,
) -> dict[str, int]:
    """Seed the ``apollo_misconceptions`` bank for one subject's concept(s) of
    one course. Returns ``{"entries_upserted", "concepts_seeded"}``."""
    engine = create_async_engine(database_url)
    Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    stats = {"entries_upserted": 0, "concepts_seeded": 0}

    try:
        async with Session() as session:
            targets = await _resolve_concepts(
                session,
                subject_slug=subject_slug,
                concept_slug=concept_slug,
                search_space_id=search_space_id,
            )

            for cid, cslug in targets:
                count = await _seed_concept(
                    session,
                    concept_id=cid,
                    concept_slug=cslug,
                    subject_slug=subject_slug,
                    embed=embed,
                )
                stats["entries_upserted"] += count
                stats["concepts_seeded"] += 1

            if dry_run:
                await session.rollback()
                _LOG.info("seed_dry_run subject=%s stats=%s", subject_slug, stats)
            else:
                await session.commit()
                _LOG.info("seed_committed subject=%s stats=%s", subject_slug, stats)
    finally:
        await engine.dispose()

    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database-url",
        default=None,
        help="async PostgreSQL URL (defaults to env DATABASE_URL)",
    )
    parser.add_argument("--subject-slug", required=True, help="subject slug to seed")
    parser.add_argument(
        "--concept-slug",
        default=None,
        help="concept slug to seed (default: every concept under the subject)",
    )
    parser.add_argument(
        "--search-space-id",
        type=int,
        default=None,
        help="course id (defaults to MIN(aita_search_spaces.id))",
    )
    parser.add_argument("--dry-run", action="store_true", help="seed but rollback the transaction")
    parser.add_argument(
        "--no-embeddings",
        dest="embed",
        action="store_false",
        default=True,
        help="skip OpenAI embedding calls (rows insert with NULL description_embedding)",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    db_url = args.database_url or os.environ.get("DATABASE_URL")
    if not db_url:
        print("ERROR: pass --database-url or set DATABASE_URL", file=sys.stderr)
        return 2

    stats = asyncio.run(
        seed(
            db_url,
            subject_slug=args.subject_slug,
            concept_slug=args.concept_slug,
            search_space_id=args.search_space_id,
            dry_run=args.dry_run,
            embed=args.embed,
        )
    )
    print(f"seeded: {stats}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
