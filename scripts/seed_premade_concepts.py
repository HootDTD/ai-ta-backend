"""Register a PREMADE concept list into a course (reversed provisioning).

Reads a concepts.json of shape ``{"concepts": [{"slug", "name", "desc"}, ...]}``
(e.g. apollo/provisioning/corpora/calc2/concepts.json) and upserts one
apollo_subjects row + one apollo_concepts row per entry for the target
search space. Idempotent: concepts are matched by NORMALIZED slug
(hyphen/underscore/case-insensitive, ``concept_match.norm_slug``) so a premade
list with hyphens updates registry-seeded underscore rows instead of
duplicating them. Existing rows keep their slug spelling and vocabulary; only
description (and display_name when empty) are backfilled. NEVER writes
problems — the authored-set upload path is what populates the problem bank
under the reversed model, and pre-seeding identical problems would gate-8
duplicate-reject the uploads.

``--vocab-from-subject <subject_dir>``: additionally copies
canonical_symbols / normalization_map from
``apollo/subjects/<subject_dir>/concepts/<slug>/`` into slug-matching rows
WHOSE VOCAB IS EMPTY (first-writer-wins, like ``author_concept_symbols``).

Usage:
  python -m scripts.seed_premade_concepts \\
      --database-url postgresql+asyncpg://postgres:postgres@127.0.0.1:57322/postgres \\
      --search-space-id 2 --subject-slug calculus_2 \\
      --concepts-json apollo/provisioning/corpora/calc2/concepts.json \\
      --vocab-from-subject calculus_2
"""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from apollo.persistence.models import Concept, Subject
from apollo.provisioning.concept_match import norm_slug

_SUBJECTS_ROOT = Path(__file__).resolve().parent.parent / "apollo" / "subjects"


@dataclass(frozen=True)
class PremadeSeedReport:
    created: int
    updated: int
    vocab_copied: int


async def _get_or_create_subject(
    db: AsyncSession, *, search_space_id: int, subject_slug: str, display_name: str
) -> Subject:
    row = (
        await db.execute(
            select(Subject).where(
                Subject.search_space_id == search_space_id,
                Subject.slug == subject_slug,
            )
        )
    ).scalar_one_or_none()
    if row is not None:
        return row
    row = Subject(slug=subject_slug, display_name=display_name, search_space_id=search_space_id)
    db.add(row)
    await db.flush()
    return row


def _vocab_for(vocab_dir: Path | None, slug: str) -> tuple[dict, dict]:
    if vocab_dir is None:
        return {}, {}
    cdir = vocab_dir / norm_slug(slug)
    sym_path = cdir / "canonical_symbols.json"
    norm_path = cdir / "normalization_map.json"
    symbols = json.loads(sym_path.read_text()) if sym_path.is_file() else {}
    norm_map = json.loads(norm_path.read_text()) if norm_path.is_file() else {}
    return symbols, norm_map


async def seed_premade_concepts(
    db: AsyncSession,
    *,
    search_space_id: int,
    subject_slug: str,
    subject_display_name: str,
    concepts: list[dict],
    vocab_dir: Path | None = None,
) -> PremadeSeedReport:
    subject = await _get_or_create_subject(
        db,
        search_space_id=search_space_id,
        subject_slug=subject_slug,
        display_name=subject_display_name,
    )
    existing = (
        (await db.execute(select(Concept).where(Concept.subject_id == subject.id))).scalars().all()
    )
    by_norm = {norm_slug(str(c.slug)): c for c in existing}

    created = updated = vocab_copied = 0
    for entry in concepts:
        slug = str(entry["slug"])
        name = str(entry.get("name") or slug)
        desc = str(entry.get("desc") or "")
        symbols, norm_map = _vocab_for(vocab_dir, slug)
        row = by_norm.get(norm_slug(slug))
        if row is None:
            row = Concept(
                subject_id=subject.id,
                slug=slug,
                display_name=name,
                description=desc,
                canonical_symbols=symbols,
                normalization_map=norm_map,
            )
            db.add(row)
            by_norm[norm_slug(slug)] = row
            created += 1
            if symbols:
                vocab_copied += 1
        else:
            changed = False
            if desc and row.description != desc:
                row.description = desc  # type: ignore[assignment]
                changed = True
            if not row.display_name and name:
                row.display_name = name  # type: ignore[assignment]
                changed = True
            # first-writer-wins: only fill EMPTY vocab
            if symbols and not dict(row.canonical_symbols or {}).get("symbols"):
                row.canonical_symbols = symbols  # type: ignore[assignment]
                row.normalization_map = norm_map  # type: ignore[assignment]
                vocab_copied += 1
                changed = True
            if changed:
                updated += 1
    await db.flush()
    return PremadeSeedReport(created=created, updated=updated, vocab_copied=vocab_copied)


async def _main() -> None:  # pragma: no cover - CLI entrypoint (engine + argparse)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--search-space-id", type=int, required=True)
    parser.add_argument("--subject-slug", required=True)
    parser.add_argument("--subject-display-name", default=None)
    parser.add_argument("--concepts-json", type=Path, required=True)
    parser.add_argument("--vocab-from-subject", default=None)
    args = parser.parse_args()

    payload = json.loads(args.concepts_json.read_text())
    concepts = payload["concepts"] if isinstance(payload, dict) else payload
    vocab_dir = (
        _SUBJECTS_ROOT / args.vocab_from_subject / "concepts" if args.vocab_from_subject else None
    )

    engine = create_async_engine(args.database_url)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as db:
        report = await seed_premade_concepts(
            db,
            search_space_id=args.search_space_id,
            subject_slug=args.subject_slug,
            subject_display_name=args.subject_display_name or args.subject_slug,
            concepts=concepts,
            vocab_dir=vocab_dir,
        )
        await db.commit()
    await engine.dispose()
    print(
        f"premade concepts: created={report.created} "
        f"updated={report.updated} vocab_copied={report.vocab_copied}"
    )


if __name__ == "__main__":
    asyncio.run(_main())
