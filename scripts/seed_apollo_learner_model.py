"""Seeder: WU-3B Apollo learner-model Layer-1 rows for the bernoulli concept.

Layers on TOP of ``scripts/seed_apollo_concept_registry.py`` (which must have run
first to create the ``apollo_concepts`` / ``apollo_concept_problems`` rows this
reads). Converts the hand-authored bernoulli source files into migration-026
Layer-1 rows and annotates the bernoulli problems' reference solutions:

  * ``apollo_kg_entities``   — concept (14) + variable (8) + reference-derived
                               (equations/conditions/simplifications/procedures,
                               deduped by canonical_key across the 5 problems) +
                               ``misc.*`` (with an opposes-link) + the authored
                               ``def.pressure_velocity_tradeoff``.
  * ``apollo_entity_prereqs`` — one edge per concept_dag edge (16).
  * misconception opposes-link — resolved to ``payload.opposes_entity_id`` in a
                               second pass once every entity row exists (D3).
  * ``apollo_concept_problems.payload`` — each reference-solution step gains an
                               ``entity_key``; each problem gains
                               ``declared_paths`` + ``layer1_seeded`` (D2/D6), so
                               the §6.1 validation contract passes (the WU-4A
                               fixture gate). Optionally written back to the
                               on-disk ``problem_*.json`` too (``--write-disk``).

Course-scoped (resolves the bernoulli concept via subject.search_space_id, D7)
and idempotent (entity upsert keyed on ``(concept_id, canonical_key)``; prereqs
``ON CONFLICT DO NOTHING``; re-annotation is a no-op). NO LLM, NO embeddings, NO
new DDL.

Usage:
    python -m scripts.seed_apollo_learner_model [--database-url URL]
        [--search-space-id N] [--dry-run] [--write-disk/--no-write-disk] [-v]
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

from apollo.persistence.learner_model_seed import (  # noqa: E402
    EntitySpec,
    SeedError,
    annotate_reference_solution,
    authored_definitions,
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

_LOG = logging.getLogger(__name__)
_BERNOULLI_SLUG = "bernoulli_principle"
_BERNOULLI_DIR = (
    Path(__file__).resolve().parents[1]
    / "apollo"
    / "subjects"
    / "fluid_mechanics"
    / "concepts"
    / _BERNOULLI_SLUG
)


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Spec assembly (pure) — dedup the union of every entity source by canonical_key
# ---------------------------------------------------------------------------


def _collect_entity_specs(problems: list[dict]) -> list[EntitySpec]:
    """Build the deduped union of all Layer-1 entity specs for bernoulli.

    Dedup is by ``canonical_key`` (per spec §8 promotion-lint intent: shared
    reference nodes like continuity never become several entities). First spec
    for a key wins; later duplicates with the same key are dropped.
    """
    dag = _read_json(_BERNOULLI_DIR / "concept_dag.json")
    symbols = _read_json(_BERNOULLI_DIR / "canonical_symbols.json")
    normalization = _read_json(_BERNOULLI_DIR / "normalization_map.json")
    misc = _read_json(_BERNOULLI_DIR / "misconceptions.json")

    specs: list[EntitySpec] = []
    specs.extend(concept_dag_to_entities(dag))
    specs.extend(symbols_to_entities(symbols, normalization))
    for problem in problems:
        specs.extend(reference_solution_to_entities(problem))
    specs.extend(authored_definitions())
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


async def _resolve_concept(session: AsyncSession, search_space_id: int | None) -> int:
    """Resolve the bernoulli concept id for a course (D7). Raises SeedError if
    the concept row is missing (the registry seeder must run first)."""
    if search_space_id is None:
        search_space_id = (
            await session.execute(text("SELECT MIN(id) FROM aita_search_spaces"))
        ).scalar_one_or_none()
        if search_space_id is None:
            raise SeedError(
                "no aita_search_spaces rows — seed a course before the learner model"
            )

    concept_id = (
        await session.execute(
            select(Concept.id)
            .join(Subject, Subject.id == Concept.subject_id)
            .where(Subject.search_space_id == search_space_id)
            .where(Concept.slug == _BERNOULLI_SLUG)
        )
    ).scalar_one_or_none()
    if concept_id is None:
        raise SeedError(
            f"no '{_BERNOULLI_SLUG}' concept for search_space_id={search_space_id}; "
            "run scripts.seed_apollo_concept_registry first"
        )
    return concept_id


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
        return row.id, True

    # Idempotent update in place (same values on a re-run -> no semantic change).
    row.kind = spec.kind
    row.display_name = spec.display_name
    row.payload = dict(spec.payload)
    row.aliases = list(spec.aliases)
    await session.flush()
    return row.id, False


async def _link_opposes(
    session: AsyncSession, concept_id: int, key_to_id: dict[str, int]
) -> int:
    """Second pass: resolve each misconception's payload.opposes_entity_key to
    opposes_entity_id (now that every entity row exists). Returns the count
    linked. Idempotent (re-resolves to the same id)."""
    rows = (
        await session.execute(
            select(KGEntity)
            .where(KGEntity.concept_id == concept_id)
            .where(KGEntity.kind == "misconception")
        )
    ).scalars().all()

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
        row.payload = payload
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
    concept_id: int,
    node_key_by_problem: dict[str, dict[str, str]],
    *,
    write_disk: bool,
) -> int:
    """Annotate each bernoulli problem's stored payload with entity links +
    declared path + layer1_seeded (idempotent). Optionally mirror the annotated
    payload back to the on-disk problem_*.json (additive keys only)."""
    rows = (
        await session.execute(
            select(ConceptProblem).where(ConceptProblem.concept_id == concept_id)
        )
    ).scalars().all()

    annotated_count = 0
    disk_by_code = _disk_problem_paths()
    for row in rows:
        payload = dict(row.payload or {})
        mapping = node_key_by_problem.get(payload.get("id"))
        if mapping is None:
            # A problem whose reference nodes we have no mapping for is unexpected
            # for the bernoulli fixture set; skip rather than mislink.
            continue
        annotated = annotate_reference_solution(payload, lambda nid: mapping[nid])
        row.payload = annotated
        annotated_count += 1

        if write_disk:
            disk_path = disk_by_code.get(payload.get("id"))
            if disk_path is not None:
                disk_path.write_text(
                    json.dumps(annotated, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8",
                )
    await session.flush()
    return annotated_count


def _disk_problem_paths() -> dict[str, Path]:
    """Map problem ``id`` -> on-disk problem_*.json path."""
    out: dict[str, Path] = {}
    problems_dir = _BERNOULLI_DIR / "problems"
    if not problems_dir.is_dir():
        return out
    for path in sorted(problems_dir.glob("problem_*.json")):
        payload = _read_json(path)
        out[payload.get("id", path.stem)] = path
    return out


def _load_problems_from_db_rows(rows) -> list[dict]:
    return [dict(r.payload) for r in rows]


async def seed(
    database_url: str,
    *,
    search_space_id: int | None = None,
    dry_run: bool = False,
    write_disk: bool = True,
) -> dict[str, int]:
    """Run the WU-3B Layer-1 seed for the bernoulli concept of one course.

    Returns a stats dict::

        {"entities_inserted", "entities_updated", "prereqs_inserted",
         "prereqs_skipped", "misconceptions_linked", "problems_annotated"}
    """
    engine = create_async_engine(database_url)
    Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    stats = {
        "entities_inserted": 0,
        "entities_updated": 0,
        "prereqs_inserted": 0,
        "prereqs_skipped": 0,
        "misconceptions_linked": 0,
        "problems_annotated": 0,
    }

    try:
        async with Session() as session:
            concept_id = await _resolve_concept(session, search_space_id)

            # Load the bernoulli problems from the DB (the registry seeder wrote
            # them); these drive reference-entity minting + annotation.
            problem_rows = (
                await session.execute(
                    select(ConceptProblem).where(
                        ConceptProblem.concept_id == concept_id
                    )
                )
            ).scalars().all()
            problems = _load_problems_from_db_rows(problem_rows)

            specs = _collect_entity_specs(problems)
            key_to_id: dict[str, int] = {}
            for spec in specs:
                entity_id, inserted = await _upsert_entity(session, concept_id, spec)
                key_to_id[spec.canonical_key] = entity_id
                if inserted:
                    stats["entities_inserted"] += 1
                else:
                    stats["entities_updated"] += 1

            stats["misconceptions_linked"] = await _link_opposes(
                session, concept_id, key_to_id
            )

            dag = _read_json(_BERNOULLI_DIR / "concept_dag.json")
            inserted, skipped = await _insert_prereqs(
                session, key_to_id, concept_dag_to_prereqs(dag)
            )
            stats["prereqs_inserted"] = inserted
            stats["prereqs_skipped"] = skipped

            node_key_by_problem = _node_key_index(problems)
            stats["problems_annotated"] = await _annotate_problems(
                session, concept_id, node_key_by_problem, write_disk=write_disk
            )

            if dry_run:
                await session.rollback()
                _LOG.info("seed_dry_run stats=%s", stats)
            else:
                await session.commit()
                _LOG.info("seed_committed stats=%s", stats)
    finally:
        await engine.dispose()

    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database-url", default=None,
        help="async PostgreSQL URL (defaults to env DATABASE_URL)",
    )
    parser.add_argument(
        "--search-space-id", type=int, default=None,
        help="course id (defaults to MIN(aita_search_spaces.id))",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="seed but rollback the transaction"
    )
    parser.add_argument(
        "--write-disk", dest="write_disk", action="store_true", default=True,
        help="mirror annotated reference solutions back to problem_*.json (default)",
    )
    parser.add_argument(
        "--no-write-disk", dest="write_disk", action="store_false",
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
            search_space_id=args.search_space_id,
            dry_run=args.dry_run,
            write_disk=args.write_disk,
        )
    )
    print(f"seeded: {stats}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
