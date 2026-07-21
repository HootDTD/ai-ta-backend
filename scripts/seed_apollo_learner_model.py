"""Seeder: Apollo learner-model Layer-1 rows for one subject's concept(s).

Layers on TOP of ``scripts/seed_apollo_concept_registry.py`` (which must have run
first to create the ``apollo_concepts`` / ``apollo_concept_problems`` rows this
reads). Converts the hand-authored concept source files into migration-026
Layer-1 rows and annotates each concept's problems' reference solutions:

  * ``apollo_kg_entities``   — concept + variable + reference-derived
                               (equations/conditions/simplifications/procedures,
                               deduped by canonical_key across a concept's
                               problems) + ``misc.*`` (with an opposes-link) + any
                               authored definitions.
  * ``apollo_entity_prereqs`` — one edge per concept_dag edge.
  * misconception opposes-link — resolved to ``payload.opposes_entity_id`` in a
                               second pass once every entity row exists (D3).
  * ``apollo_concept_problems.payload`` — each reference-solution step gains an
                               ``entity_key``; each problem gains
                               ``declared_paths`` + ``layer1_seeded`` (D2/D6), so
                               the §6.1 validation contract passes (the WU-4A
                               fixture gate). Optionally written back to the
                               on-disk ``problem_*.json`` too (``--write-disk``).

Subject/concept-generic (WU-2 of the macro graph-grading probe): ``--subject-slug``
selects the on-disk ``apollo/subjects/<subject>/concepts/<concept>/`` trees and
the DB subject; ``--concept-slug`` narrows to one concept (default: every concept
the subject teaches). The conversion core is already subject-agnostic; this driver
resolves the concept(s) from the DB and reads each concept's own source dir.

Backward compatibility: with no ``--subject-slug`` (default ``fluid_mechanics``)
and no ``--concept-slug`` the bernoulli behavior is unchanged — bernoulli's
authored ``def.pressure_velocity_tradeoff`` (not a reference node) is still minted
from ``_AUTHORED_DEFINITIONS`` so its existing opposes-link resolves.

Course-scoped (resolves the subject via ``apollo_subjects.search_space_id``, D7)
and idempotent (entity upsert keyed on ``(concept_id, canonical_key)``; prereqs
``ON CONFLICT DO NOTHING``; re-annotation is a no-op). NO LLM, NO embeddings, NO
new DDL.

Usage:
    python -m scripts.seed_apollo_learner_model [--database-url URL]
        [--subject-slug SLUG] [--concept-slug SLUG] [--search-space-id N]
        [--dry-run] [--write-disk/--no-write-disk] [-v]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# Make the package importable when run as `python -m scripts....`
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from apollo.persistence.learner_model_seed import (  # noqa: E402
    EntitySpec,
    SeedError,
    annotate_reference_solution,
    authored_definitions,
    authored_definitions_from_spec,
    concept_dag_to_entities,
    concept_dag_to_prereqs,
    misconceptions_to_entities,
    reference_solution_to_entities,
    symbols_to_entities,
)
from apollo.persistence.models import (  # noqa: E402
    Concept,
    ConceptProblem,
    EntityPrereq,
    KGEntity,
    Subject,
)
from database.models import Course  # noqa: E402

_LOG = logging.getLogger(__name__)

# Backward-compat default: a bare invocation seeds the bernoulli concept of the
# fluid_mechanics subject exactly as before.
_DEFAULT_SUBJECT_SLUG = "fluid_mechanics"
_BERNOULLI_SLUG = "bernoulli_principle"

_SUBJECTS_ROOT = Path(__file__).resolve().parents[1] / "apollo" / "subjects"


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _concept_dir(subject_slug: str, concept_slug: str) -> Path:
    """On-disk source dir for one concept: ``.../subjects/<s>/concepts/<c>/``."""
    return _SUBJECTS_ROOT / subject_slug / "concepts" / concept_slug


@dataclass(frozen=True)
class _ConceptTarget:
    """One concept to seed: its DB row id + slug + on-disk source dir."""

    concept_id: int
    slug: str
    source_dir: Path


# ---------------------------------------------------------------------------
# Spec assembly (pure) — dedup the union of every entity source by canonical_key
# ---------------------------------------------------------------------------


def _authored_definitions_for(concept_slug: str, source_dir: Path) -> list[EntitySpec]:
    """Authored ``def.*`` entities for one concept.

    Resolution order (first match wins):
      1. An optional ``authored_definitions.json`` in the concept dir (generic
         mechanism — a list of definition dicts).
      2. The bernoulli special case: its ``def.pressure_velocity_tradeoff`` is
         not a reference node, so it is minted from ``_AUTHORED_DEFINITIONS``.
      3. Nothing — a generic concept whose misconceptions oppose REAL reference
         keys (the macro content guarantee) needs no standalone definitions.
    """
    disk_path = source_dir / "authored_definitions.json"
    if disk_path.is_file():
        entries = _read_json(disk_path).get("definitions", [])
        return authored_definitions_from_spec(entries)
    if concept_slug == _BERNOULLI_SLUG:
        return authored_definitions()
    return []


def _collect_entity_specs(
    concept_slug: str, source_dir: Path, problems: list[dict]
) -> list[EntitySpec]:
    """Build the deduped union of all Layer-1 entity specs for one concept.

    Dedup is by ``canonical_key`` (per spec §8 promotion-lint intent: shared
    reference nodes never become several entities). First spec for a key wins;
    later duplicates with the same key are dropped.
    """
    dag = _read_json(source_dir / "concept_dag.json")
    symbols = _read_json(source_dir / "canonical_symbols.json")
    normalization = _read_json(source_dir / "normalization_map.json")
    misc = _read_json(source_dir / "misconceptions.json")

    specs: list[EntitySpec] = []
    specs.extend(concept_dag_to_entities(dag))
    specs.extend(symbols_to_entities(symbols, normalization))
    for problem in problems:
        specs.extend(reference_solution_to_entities(problem))
    specs.extend(_authored_definitions_for(concept_slug, source_dir))
    specs.extend(misconceptions_to_entities(misc))

    deduped: dict[str, EntitySpec] = {}
    for spec in specs:
        deduped.setdefault(spec.canonical_key, spec)
    return list(deduped.values())


def _node_key_index(problems: list[dict]) -> dict[str, dict[str, str]]:
    """Per-problem map: problem id -> {reference-node id -> canonical_key}.

    Mirrors the conversion: ``reference_solution_to_entities`` preserves step
    order, so zipping its output with the steps recovers each node's key.
    """
    index: dict[str, dict[str, str]] = {}
    for problem in problems:
        specs = reference_solution_to_entities(problem)
        mapping = {
            step["id"]: spec.canonical_key
            for step, spec in zip(problem["reference_solution"], specs, strict=True)
        }
        index[problem["id"]] = mapping
    return index


# ---------------------------------------------------------------------------
# DB write layer
# ---------------------------------------------------------------------------


async def _resolve_search_space_id(
    session: AsyncSession, search_space_id: int | None
) -> int:
    """Resolve the course id, defaulting to the smallest course."""
    if search_space_id is not None:
        return search_space_id
    resolved = (await session.execute(select(func.min(Course.id)))).scalar_one_or_none()
    if resolved is None:
        raise SeedError("no app.courses rows — seed a course before the learner model")
    return resolved


async def _resolve_concepts(
    session: AsyncSession,
    *,
    subject_slug: str,
    concept_slug: str | None,
    search_space_id: int | None,
    source_subject_slug: str,
) -> list[_ConceptTarget]:
    """Resolve the target concept(s) for one course generically (D7).

    Resolution: ``Subject.search_space_id == <course>`` AND ``Subject.slug ==
    subject_slug`` -> its ``Concept`` rows. When ``concept_slug`` is given, narrow
    to that one concept; otherwise return every concept the subject teaches
    (slug-sorted for stable iteration). Raises ``SeedError`` if the subject or a
    requested concept row is missing (the registry seeder must run first).

    ``source_subject_slug`` names the on-disk source tree
    (``apollo/subjects/<source>/concepts/<c>/``); it defaults to the DB
    ``subject_slug`` but may differ when one physical concept dir is shared by
    several course-scoped DB subjects whose slugs must be globally unique.
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

    return [
        _ConceptTarget(
            concept_id=row_id,
            slug=row_slug,
            source_dir=_concept_dir(source_subject_slug, row_slug),
        )
        for row_id, row_slug in rows
    ]


async def _upsert_entity(
    session: AsyncSession, concept_id: int, spec: EntitySpec
) -> tuple[int, bool]:
    """Upsert one entity keyed on (concept_id, canonical_key). Returns
    (entity_id, inserted)."""
    row = (
        await session.execute(
            select(KGEntity)
            .where(KGEntity.concept_id == concept_id)
            .where(KGEntity.canonical_key == spec.canonical_key)
        )
    ).scalar_one_or_none()

    if row is None:
        row = KGEntity(
            concept_id=concept_id,
            canonical_key=spec.canonical_key,
            kind=spec.kind,
            display_name=spec.display_name,
            payload=dict(spec.payload),
            aliases=list(spec.aliases),
        )
        session.add(row)
        await session.flush()
        return row.id, True  # type: ignore[return-value]

    # Idempotent update in place (same values on a re-run -> no semantic change).
    row.kind = spec.kind  # type: ignore[assignment]
    row.display_name = spec.display_name  # type: ignore[assignment]
    row.payload = dict(spec.payload)  # type: ignore[assignment]
    row.aliases = list(spec.aliases)  # type: ignore[assignment]
    await session.flush()
    return row.id, False  # type: ignore[return-value]


async def _link_opposes(session: AsyncSession, concept_id: int, key_to_id: dict[str, int]) -> int:
    """Second pass: resolve each misconception's payload.opposes_entity_key to
    opposes_entity_id (now that every entity row exists). Returns the count
    linked. Idempotent (re-resolves to the same id)."""
    rows = (
        (
            await session.execute(
                select(KGEntity)
                .where(KGEntity.concept_id == concept_id)
                .where(KGEntity.kind == "misconception")
            )
        )
        .scalars()
        .all()
    )

    linked = 0
    for row in rows:
        payload = dict(row.payload or {})
        opposes_key = payload.get("opposes_entity_key")
        if not opposes_key:
            continue
        target_id = key_to_id.get(opposes_key)
        if target_id is None:
            raise SeedError(
                f"misconception {row.canonical_key} opposes unknown key {opposes_key!r}"
            )
        payload["opposes_entity_id"] = target_id
        row.payload = payload  # type: ignore[assignment]
        linked += 1
    await session.flush()
    return linked


async def _insert_prereqs(
    session: AsyncSession,
    key_to_id: dict[str, int],
    pairs: list[tuple[str, str]],
) -> tuple[int, int]:
    """Insert prereq edges (ON CONFLICT DO NOTHING). Returns (inserted, skipped)."""
    inserted = 0
    skipped = 0
    for from_key, to_key in pairs:
        from_id = key_to_id[from_key]
        to_id = key_to_id[to_key]
        existing = (
            await session.execute(
                select(EntityPrereq)
                .where(EntityPrereq.from_entity_id == from_id)
                .where(EntityPrereq.to_entity_id == to_id)
            )
        ).scalar_one_or_none()
        if existing is not None:
            skipped += 1
            continue
        session.add(EntityPrereq(from_entity_id=from_id, to_entity_id=to_id))
        inserted += 1
    await session.flush()
    return inserted, skipped


async def _annotate_problems(
    session: AsyncSession,
    target: _ConceptTarget,
    node_key_by_problem: dict[str, dict[str, str]],
    *,
    write_disk: bool,
) -> int:
    """Annotate each concept problem's stored payload with entity links +
    declared path + layer1_seeded (idempotent). Optionally mirror the annotated
    payload back to the on-disk problem_*.json (additive keys only)."""
    rows = (
        (
            await session.execute(
                select(ConceptProblem).where(ConceptProblem.concept_id == target.concept_id)
            )
        )
        .scalars()
        .all()
    )

    annotated_count = 0
    disk_by_code = _disk_problem_paths(target.source_dir)
    for row in rows:
        payload = dict(row.payload or {})
        mapping = node_key_by_problem.get(str(payload.get("id")))
        if mapping is None:
            # A problem whose reference nodes we have no mapping for is unexpected
            # for a Layer-1 fixture set; skip rather than mislink.
            continue
        annotated = annotate_reference_solution(payload, lambda nid: mapping[nid])  # noqa: B023
        row.payload = annotated  # type: ignore[assignment]
        annotated_count += 1

        if write_disk:
            disk_path = disk_by_code.get(str(payload.get("id")))
            if disk_path is not None:
                disk_path.write_text(
                    json.dumps(annotated, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8",
                )
    await session.flush()
    return annotated_count


def _disk_problem_paths(source_dir: Path) -> dict[str, Path]:
    """Map problem ``id`` -> on-disk problem_*.json path for one concept dir."""
    out: dict[str, Path] = {}
    problems_dir = source_dir / "problems"
    if not problems_dir.is_dir():
        return out
    for path in sorted(problems_dir.glob("problem_*.json")):
        payload = _read_json(path)
        out[payload.get("id", path.stem)] = path
    return out


def _load_problems_from_db_rows(rows) -> list[dict]:
    return [dict(r.payload) for r in rows]


async def _seed_concept(
    session: AsyncSession,
    target: _ConceptTarget,
    *,
    write_disk: bool,
) -> dict[str, int]:
    """Seed one concept's Layer-1 rows + annotate its problems. Returns a
    per-concept stats dict (the same keys the top-level ``seed`` sums)."""
    stats = {
        "entities_inserted": 0,
        "entities_updated": 0,
        "prereqs_inserted": 0,
        "prereqs_skipped": 0,
        "misconceptions_linked": 0,
        "problems_annotated": 0,
    }

    problem_rows = (
        (
            await session.execute(
                select(ConceptProblem).where(ConceptProblem.concept_id == target.concept_id)
            )
        )
        .scalars()
        .all()
    )
    problems = _load_problems_from_db_rows(problem_rows)

    specs = _collect_entity_specs(target.slug, target.source_dir, problems)
    key_to_id: dict[str, int] = {}
    for spec in specs:
        entity_id, inserted = await _upsert_entity(session, target.concept_id, spec)
        key_to_id[spec.canonical_key] = entity_id
        if inserted:
            stats["entities_inserted"] += 1
        else:
            stats["entities_updated"] += 1

    stats["misconceptions_linked"] = await _link_opposes(session, target.concept_id, key_to_id)

    dag = _read_json(target.source_dir / "concept_dag.json")
    prereqs_inserted, prereqs_skipped = await _insert_prereqs(
        session, key_to_id, concept_dag_to_prereqs(dag)
    )
    stats["prereqs_inserted"] = prereqs_inserted
    stats["prereqs_skipped"] = prereqs_skipped

    node_key_by_problem = _node_key_index(problems)
    stats["problems_annotated"] = await _annotate_problems(
        session, target, node_key_by_problem, write_disk=write_disk
    )
    return stats


async def seed(
    database_url: str,
    *,
    subject_slug: str = _DEFAULT_SUBJECT_SLUG,
    concept_slug: str | None = None,
    search_space_id: int | None = None,
    source_subject_slug: str | None = None,
    dry_run: bool = False,
    write_disk: bool = True,
) -> dict[str, int]:
    """Run the Layer-1 seed for a subject's concept(s) of one course.

    ``subject_slug`` selects the DB subject; ``concept_slug`` narrows to one
    concept (default: every concept the subject teaches). ``source_subject_slug``
    overrides the on-disk source tree when it differs from the DB subject slug
    (defaults to ``subject_slug``). Backward-compatible: a bare call seeds the
    bernoulli concept of ``fluid_mechanics`` exactly as before.

    Returns a stats dict summed across every seeded concept::

        {"entities_inserted", "entities_updated", "prereqs_inserted",
         "prereqs_skipped", "misconceptions_linked", "problems_annotated",
         "concepts_seeded"}
    """
    engine_options = (
        {"execution_options": {"schema_translate_map": {"app": None, "internal": None}}}
        if database_url.startswith("sqlite")
        else {}
    )
    engine = create_async_engine(database_url, **engine_options)
    Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    stats = {
        "entities_inserted": 0,
        "entities_updated": 0,
        "prereqs_inserted": 0,
        "prereqs_skipped": 0,
        "misconceptions_linked": 0,
        "problems_annotated": 0,
        "concepts_seeded": 0,
    }

    try:
        async with Session() as session:
            targets = await _resolve_concepts(
                session,
                subject_slug=subject_slug,
                concept_slug=concept_slug,
                search_space_id=search_space_id,
                source_subject_slug=source_subject_slug or subject_slug,
            )

            for target in targets:
                per = await _seed_concept(session, target, write_disk=write_disk)
                for key, value in per.items():
                    stats[key] += value
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
    parser.add_argument(
        "--subject-slug",
        default=_DEFAULT_SUBJECT_SLUG,
        help=f"subject slug to seed (default: {_DEFAULT_SUBJECT_SLUG})",
    )
    parser.add_argument(
        "--concept-slug",
        default=None,
        help="concept slug to seed (default: every concept under the subject)",
    )
    parser.add_argument(
        "--search-space-id",
        type=int,
        default=None,
        help="course id (defaults to MIN(app.courses.id))",
    )
    parser.add_argument("--dry-run", action="store_true", help="seed but rollback the transaction")
    parser.add_argument(
        "--write-disk",
        dest="write_disk",
        action="store_true",
        default=True,
        help="mirror annotated reference solutions back to problem_*.json (default)",
    )
    parser.add_argument(
        "--no-write-disk",
        dest="write_disk",
        action="store_false",
        help="annotate the DB payload only; leave on-disk JSON untouched",
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
            write_disk=args.write_disk,
        )
    )
    print(f"seeded: {stats}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
