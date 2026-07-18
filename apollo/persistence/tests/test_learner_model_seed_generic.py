"""Pure unit tests for the SUBJECT/CONCEPT-GENERIC learner-model seeder
(``scripts.seed_apollo_learner_model``, WU-2 of the macro graph-grading probe).

These exercise the generalization that lets the Layer-1 seeder run for ANY
subject/concept, not just bernoulli, WITHOUT a Postgres/Docker harness:

  * the DB write layer runs against in-memory **SQLite** with the REAL ORM
    tables (``_JSONType`` falls back to ``JSON`` on SQLite), so the actual
    ``select``/``flush``/``commit`` queries are exercised — a true mock DB, no
    network, no remote writes;
  * a synthetic two-concept subject is authored into ``tmp_path`` and
    ``_SUBJECTS_ROOT`` is monkeypatched, proving multi-concept resolution + the
    generic opposes-link (a misconception opposing a REAL reference key, with NO
    authored-definition file) without coupling to the macroeconomics deliverable;
  * backward compatibility is pinned against the REAL bernoulli source dir via
    the default ``subject_slug='fluid_mechanics'``.

NO real DB, NO LLM, NO network.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import Column, Integer, MetaData, Table, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import scripts.seed_apollo_learner_model as seeder
from apollo.persistence.learner_model_seed import (
    authored_definitions_from_spec,
    validate_reference_graph,
)
from apollo.persistence.models import (
    Concept,
    ConceptProblem,
    EntityPrereq,
    KGEntity,
    Subject,
)
from database.models import Base
from scripts.seed_apollo_learner_model import (
    _authored_definitions_for,
    _collect_entity_specs,
    _concept_dir,
    seed,
)

_REPO = Path(__file__).resolve().parents[3]
_BERNOULLI_DIR = (
    _REPO / "apollo" / "subjects" / "fluid_mechanics" / "concepts" / "bernoulli_principle"
)

# Tables the seeder touches via create_all. ConceptProblem is created with raw
# DDL instead (its provenance column carries a Postgres-only ``'{}'::jsonb``
# server_default that SQLite's DDL compiler rejects); the raw table matches the
# columns the ConceptProblem ORM maps so ``select(ConceptProblem)`` works.
_SEED_TABLES: list[Table] = [
    Subject.__table__,  # type: ignore[list-item]  # SA stubs expose __table__ as FromClause
    Concept.__table__,  # type: ignore[list-item]
    KGEntity.__table__,  # type: ignore[list-item]
    EntityPrereq.__table__,  # type: ignore[list-item]
]

_CONCEPT_PROBLEMS_DDL = """
CREATE TABLE apollo_concept_problems (
    id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
    concept_id BIGINT NOT NULL,
    problem_code TEXT NOT NULL,
    difficulty TEXT NOT NULL,
    payload JSON NOT NULL,
    tier SMALLINT NOT NULL DEFAULT 1,
    solution_source TEXT,
    provenance JSON NOT NULL DEFAULT '{}',
    quarantined_at TIMESTAMP,
    search_space_id INTEGER,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""


# ---------------------------------------------------------------------------
# In-memory SQLite "mock DB" harness
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def db_url() -> AsyncGenerator[str, None]:
    """A shared-cache in-memory SQLite URL with the seeder's ORM tables + an
    courses stub. The seeder opens its OWN engine on this URL, so a
    shared-cache name keeps the schema visible across connections."""
    # Unique shared-cache name per test so each gets a FRESH in-memory schema
    # (cache=shared keeps a named in-memory DB alive process-globally; a fixed
    # name would leak the schema across tests).
    name = f"memseed_{uuid.uuid4().hex}"
    url = f"sqlite+aiosqlite:///file:{name}?mode=memory&cache=shared&uri=true"
    engine = create_async_engine(
        url,
        execution_options={"schema_translate_map": {"app": None, "internal": None}},
    )
    spaces = Table("courses", MetaData(), Column("id", Integer, primary_key=True))
    async with engine.begin() as conn:
        await conn.run_sync(lambda sc: spaces.create(sc))
        await conn.run_sync(lambda sc: Base.metadata.create_all(sc, tables=_SEED_TABLES))
        await conn.exec_driver_sql(_CONCEPT_PROBLEMS_DDL)
    # Hold one connection open for the lifetime of the test so the shared
    # in-memory database is not torn down between the seeder's connections.
    keepalive = await engine.connect()
    try:
        yield url
    finally:
        await keepalive.close()
        await engine.dispose()


async def _insert_course(url: str, space_id: int) -> None:
    engine = create_async_engine(url)
    Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    try:
        async with Session() as s:
            await s.execute(
                text("INSERT INTO courses (id) VALUES (:i)"),
                {"i": space_id},
            )
            await s.commit()
    finally:
        await engine.dispose()


async def _insert_subject(url: str, *, slug: str, space_id: int) -> int:
    engine = create_async_engine(url)
    Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    try:
        async with Session() as s:
            subject = Subject(slug=slug, display_name=slug.title(), search_space_id=space_id)
            s.add(subject)
            await s.flush()
            sid: int = int(subject.id)  # type: ignore[arg-type]  # SA stubs expose .id as Column
            await s.commit()
            return sid
    finally:
        await engine.dispose()


async def _insert_concept_with_problems(
    url: str, *, subject_id: int, slug: str, problems: list[dict]
) -> int:
    engine = create_async_engine(url)
    Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    try:
        async with Session() as s:
            concept = Concept(subject_id=subject_id, slug=slug, display_name=slug.title())
            s.add(concept)
            await s.flush()
            cid: int = int(concept.id)  # type: ignore[arg-type]  # SA stubs expose .id as Column
            for p in problems:
                s.add(
                    ConceptProblem(
                        concept_id=cid,
                        problem_code=p["id"],
                        difficulty=p.get("difficulty", "intro"),
                        payload=p,
                    )
                )
            await s.commit()
            return cid
    finally:
        await engine.dispose()


async def _fetch_entities(url: str, concept_id: int) -> list[KGEntity]:
    engine = create_async_engine(url)
    Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    try:
        async with Session() as s:
            return list(
                (await s.execute(select(KGEntity).where(KGEntity.concept_id == concept_id)))
                .scalars()
                .all()
            )
    finally:
        await engine.dispose()


async def _fetch_problems(url: str, concept_id: int) -> list[dict]:
    engine = create_async_engine(url)
    Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    try:
        async with Session() as s:
            rows = (
                (
                    await s.execute(
                        select(ConceptProblem).where(ConceptProblem.concept_id == concept_id)
                    )
                )
                .scalars()
                .all()
            )
            return [dict(r.payload) for r in rows]
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# Synthetic two-concept subject authored into tmp_path (the macro shape)
# ---------------------------------------------------------------------------


def _write_concept_dir(
    base: Path,
    *,
    subject: str,
    concept: str,
    dag: dict,
    symbols: dict,
    normalization: dict,
    misconceptions: dict,
    problems: list[dict],
    authored_defs: dict | None = None,
) -> None:
    cdir = base / subject / "concepts" / concept
    (cdir / "problems").mkdir(parents=True, exist_ok=True)
    (cdir / "concept_dag.json").write_text(json.dumps(dag), encoding="utf-8")
    (cdir / "canonical_symbols.json").write_text(json.dumps(symbols), encoding="utf-8")
    (cdir / "normalization_map.json").write_text(json.dumps(normalization), encoding="utf-8")
    (cdir / "misconceptions.json").write_text(json.dumps(misconceptions), encoding="utf-8")
    if authored_defs is not None:
        (cdir / "authored_definitions.json").write_text(json.dumps(authored_defs), encoding="utf-8")
    for i, p in enumerate(problems, start=1):
        (cdir / "problems" / f"problem_{i:02d}.json").write_text(json.dumps(p), encoding="utf-8")


def _macro_problem(pid: str, *, cond_id: str = "final_goods_only") -> dict:
    """A small but faithful macro-shaped problem whose misconception opposes a
    REAL reference key (the condition node) — the generic opposes mechanism."""
    return {
        "id": pid,
        "concept_id": "gdp_components",
        "difficulty": "intro",
        "given_values": {"C": 400.0, "I": 60.0, "G": 120.0, "X": 100.0, "M": 120.0},
        "problem_text": "Compute GDP from its expenditure components.",
        "target_unknown": "GDP",
        "reference_solution": [
            {
                "id": "net_exports",
                "step": 1,
                "entry_type": "equation",
                "content": {"label": "Net exports", "symbolic": "NX - (X - M)"},
                "depends_on": [],
            },
            {
                "id": cond_id,
                "step": 2,
                "entry_type": "condition",
                "content": {
                    "label": "Final goods only",
                    "applies_when": "only final goods and services count",
                },
                "depends_on": [],
            },
            {
                "id": "sum_components",
                "step": 3,
                "entry_type": "procedure_step",
                "content": {
                    "order": 1,
                    "action": "sum C, I, G, NX",
                    "uses_equations": ["net_exports"],
                    "purpose": "obtain GDP",
                },
                "depends_on": ["net_exports"],
            },
        ],
    }


def _real_gdp_problem() -> dict:
    return {
        "id": "real_gdp_from_deflator",
        "concept_id": "nominal_vs_real_gdp",
        "difficulty": "standard",
        "given_values": {"nomGDP": 543.3, "PI": 19.0},
        "problem_text": "Find real GDP from the deflator.",
        "target_unknown": "realGDP",
        "reference_solution": [
            {
                "id": "gdp_deflator",
                "step": 1,
                "entry_type": "equation",
                "content": {
                    "label": "GDP deflator",
                    "symbolic": "deflator - (nomGDP/realGDP)*100",
                },
                "depends_on": [],
            },
            {
                "id": "real_basis",
                "step": 2,
                "entry_type": "definition",
                "content": {"concept": "real GDP", "meaning": "inflation-adjusted"},
                "depends_on": [],
            },
        ],
    }


def _make_macro_subject(base: Path, *, subject: str = "macroeconomics") -> None:
    """Author a 2-concept macro subject under ``base``.

    Concept A ``gdp_components``: misconception opposes ``cond.final_goods_only``
    (a REAL reference key minted from the problem) — the generic opposes path.
    Concept B ``nominal_vs_real_gdp``: misconception opposes ``def.real_basis``
    (also a real reference key).
    """
    _write_concept_dir(
        base,
        subject=subject,
        concept="gdp_components",
        dag={
            "nodes": [
                {"id": "gdp_components", "label": "GDP Components"},
                {"id": "expenditure_approach", "label": "Expenditure Approach"},
            ],
            "edges": [{"from": "gdp_components", "to": "expenditure_approach", "type": "requires"}],
        },
        symbols={
            "symbols": ["GDP", "C", "I", "G", "NX"],
            "description": {"GDP": "gross domestic product", "C": "consumption"},
        },
        normalization={"output": "GDP", "spending": "C"},
        misconceptions={
            "misconceptions": [
                {
                    "key": "misc.includes_transfers",
                    "display_name": "Counts transfer payments",
                    "description": "Counts transfers / used goods in GDP.",
                    "opposes": "cond.final_goods_only",
                    "trigger_phrases": ["count transfers", "include used goods"],
                }
            ]
        },
        problems=[_macro_problem("gdp_identity"), _macro_problem("net_exports_sign")],
    )
    _write_concept_dir(
        base,
        subject=subject,
        concept="nominal_vs_real_gdp",
        dag={
            "nodes": [{"id": "nominal_vs_real_gdp", "label": "Nominal vs Real GDP"}],
            "edges": [],
        },
        symbols={
            "symbols": ["nomGDP", "realGDP", "deflator", "PI"],
            "description": {"nomGDP": "nominal GDP"},
        },
        normalization={"price index": "PI"},
        misconceptions={
            "misconceptions": [
                {
                    "key": "misc.nominal_for_real",
                    "display_name": "Uses nominal for real",
                    "description": "Forgets to deflate.",
                    "opposes": "def.real_basis",
                    "trigger_phrases": ["use nominal", "skip deflating"],
                }
            ]
        },
        problems=[_real_gdp_problem()],
    )


# ---------------------------------------------------------------------------
# Pure helpers (no DB) — authored_definitions_from_spec, _concept_dir,
# _authored_definitions_for, _collect_entity_specs
# ---------------------------------------------------------------------------


def test_authored_definitions_from_spec_builds_definition_entities():
    entries = [
        {
            "key": "def.foo_bar",
            "display_name": "Foo Bar",
            "statement": "A foo is a bar.",
            "aliases": ["foobar", "the foo-bar relation"],
        }
    ]
    specs = authored_definitions_from_spec(entries)
    assert len(specs) == 1
    spec = specs[0]
    assert spec.canonical_key == "def.foo_bar"
    assert spec.kind == "definition"
    assert spec.display_name == "Foo Bar"
    assert spec.payload["statement"] == "A foo is a bar."
    assert spec.aliases == ("foobar", "the foo-bar relation")


def test_authored_definitions_from_spec_defaults_display_and_aliases():
    specs = authored_definitions_from_spec([{"key": "def.x_y", "statement": "s"}])
    assert specs[0].display_name == "X Y"  # humanized from the key tail
    assert specs[0].aliases == ()


def test_authored_definitions_from_spec_does_not_mutate_input():
    entries = [{"key": "def.k", "statement": "s"}]
    before = json.loads(json.dumps(entries))
    authored_definitions_from_spec(entries)
    assert entries == before


def test_concept_dir_path_shape(monkeypatch, tmp_path):
    monkeypatch.setattr(seeder, "_SUBJECTS_ROOT", tmp_path)
    p = _concept_dir("macroeconomics", "gdp_components")
    assert p == tmp_path / "macroeconomics" / "concepts" / "gdp_components"


def test_authored_definitions_for_bernoulli_uses_constant():
    """Backward compat: bernoulli (no authored_definitions.json) still mints its
    pressure-velocity tradeoff from the constant."""
    specs = _authored_definitions_for("bernoulli_principle", _BERNOULLI_DIR)
    keys = {s.canonical_key for s in specs}
    assert "def.pressure_velocity_tradeoff" in keys


def test_authored_definitions_for_generic_concept_is_empty(tmp_path):
    """A generic concept dir with no authored_definitions.json mints nothing —
    its misconceptions oppose REAL reference keys instead."""
    cdir = tmp_path / "macroeconomics" / "concepts" / "gdp_components"
    cdir.mkdir(parents=True)
    assert _authored_definitions_for("gdp_components", cdir) == []


def test_authored_definitions_for_reads_disk_file(tmp_path):
    cdir = tmp_path / "s" / "concepts" / "c"
    cdir.mkdir(parents=True)
    (cdir / "authored_definitions.json").write_text(
        json.dumps({"definitions": [{"key": "def.z", "statement": "zed"}]}),
        encoding="utf-8",
    )
    specs = _authored_definitions_for("c", cdir)
    assert [s.canonical_key for s in specs] == ["def.z"]
    assert specs[0].payload["statement"] == "zed"


def test_collect_entity_specs_generic_concept_opposes_real_reference_key(monkeypatch, tmp_path):
    """The generic opposes guarantee: every misconception's opposes_entity_key is
    a key minted by the concept's own sources (reference nodes / symbols /
    concept dag) — NO authored-definition file needed."""
    monkeypatch.setattr(seeder, "_SUBJECTS_ROOT", tmp_path)
    _make_macro_subject(tmp_path)
    cdir = _concept_dir("macroeconomics", "gdp_components")
    problems = [_macro_problem("gdp_identity"), _macro_problem("net_exports_sign")]

    specs = _collect_entity_specs("gdp_components", cdir, problems)
    by_key = {s.canonical_key: s for s in specs}

    # The misconception opposes cond.final_goods_only, which IS minted.
    assert "misc.includes_transfers" in by_key
    assert "cond.final_goods_only" in by_key
    assert by_key["misc.includes_transfers"].payload["opposes_entity_key"] == (
        "cond.final_goods_only"
    )
    # No bernoulli-style authored definition leaked in.
    assert "def.pressure_velocity_tradeoff" not in by_key
    # Dedup: the shared cond/eq across the two problems mints one entity each.
    assert sum(1 for s in specs if s.canonical_key == "cond.final_goods_only") == 1


# ---------------------------------------------------------------------------
# Macro multi-concept resolution (SQLite mock DB)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_seed_macro_all_concepts_under_subject(monkeypatch, tmp_path, db_url):
    """Default concept_slug=None seeds EVERY concept the subject teaches."""
    monkeypatch.setattr(seeder, "_SUBJECTS_ROOT", tmp_path)
    _make_macro_subject(tmp_path)

    await _insert_course(db_url, 1)
    subject_id = await _insert_subject(db_url, slug="macroeconomics", space_id=1)
    cid_a = await _insert_concept_with_problems(
        db_url,
        subject_id=subject_id,
        slug="gdp_components",
        problems=[_macro_problem("gdp_identity"), _macro_problem("net_exports_sign")],
    )
    cid_b = await _insert_concept_with_problems(
        db_url,
        subject_id=subject_id,
        slug="nominal_vs_real_gdp",
        problems=[_real_gdp_problem()],
    )

    stats = await seed(db_url, subject_slug="macroeconomics", write_disk=False)

    # Both concepts were seeded.
    assert stats["concepts_seeded"] == 2
    assert stats["entities_inserted"] > 0
    assert stats["misconceptions_linked"] == 2  # one per concept

    # Concept A got its misconception with a RESOLVED opposes_entity_id.
    ents_a = await _fetch_entities(db_url, cid_a)
    misc_a = next(e for e in ents_a if e.kind == "misconception")
    assert misc_a.payload["opposes_entity_key"] == "cond.final_goods_only"
    opposes_id = misc_a.payload["opposes_entity_id"]
    target = next(e for e in ents_a if e.id == opposes_id)
    assert target.canonical_key == "cond.final_goods_only"

    # Concept B too (def.real_basis is a real reference node here).
    ents_b = await _fetch_entities(db_url, cid_b)
    misc_b = next(e for e in ents_b if e.kind == "misconception")
    assert misc_b.payload["opposes_entity_key"] == "def.real_basis"
    assert misc_b.payload["opposes_entity_id"] is not None


@pytest.mark.asyncio
async def test_seed_macro_single_concept_slug_filters(monkeypatch, tmp_path, db_url):
    """--concept-slug narrows the seed to ONE concept; the other is untouched."""
    monkeypatch.setattr(seeder, "_SUBJECTS_ROOT", tmp_path)
    _make_macro_subject(tmp_path)

    await _insert_course(db_url, 1)
    subject_id = await _insert_subject(db_url, slug="macroeconomics", space_id=1)
    cid_a = await _insert_concept_with_problems(
        db_url,
        subject_id=subject_id,
        slug="gdp_components",
        problems=[_macro_problem("gdp_identity")],
    )
    cid_b = await _insert_concept_with_problems(
        db_url,
        subject_id=subject_id,
        slug="nominal_vs_real_gdp",
        problems=[_real_gdp_problem()],
    )

    stats = await seed(
        db_url, subject_slug="macroeconomics", concept_slug="gdp_components", write_disk=False
    )
    assert stats["concepts_seeded"] == 1

    assert len(await _fetch_entities(db_url, cid_a)) > 0
    assert len(await _fetch_entities(db_url, cid_b)) == 0  # not targeted


@pytest.mark.asyncio
async def test_seed_macro_annotates_every_problem_passing_validation(monkeypatch, tmp_path, db_url):
    """The annotate path runs for the generic subject: every seeded problem gains
    entity_key + declared_paths + layer1_seeded and passes §6.1 validation."""
    monkeypatch.setattr(seeder, "_SUBJECTS_ROOT", tmp_path)
    _make_macro_subject(tmp_path)

    await _insert_course(db_url, 1)
    subject_id = await _insert_subject(db_url, slug="macroeconomics", space_id=1)
    cid_a = await _insert_concept_with_problems(
        db_url,
        subject_id=subject_id,
        slug="gdp_components",
        problems=[_macro_problem("gdp_identity"), _macro_problem("net_exports_sign")],
    )

    stats = await seed(
        db_url, subject_slug="macroeconomics", concept_slug="gdp_components", write_disk=False
    )
    assert stats["problems_annotated"] == 2

    for payload in await _fetch_problems(db_url, cid_a):
        assert payload["layer1_seeded"] is True
        assert len(payload["declared_paths"]) == 1
        for step in payload["reference_solution"]:
            assert step["entity_key"]
        assert validate_reference_graph(payload).ok is True


# ---------------------------------------------------------------------------
# Backward compatibility — default subject_slug seeds bernoulli unchanged
# ---------------------------------------------------------------------------


def _load_bernoulli_problem(n: int) -> dict:
    return json.loads(
        (_BERNOULLI_DIR / "problems" / f"problem_{n:02d}.json").read_text(encoding="utf-8")
    )


@pytest.mark.asyncio
async def test_seed_backward_compat_bernoulli_default_subject(db_url):
    """A bare seed() (default subject_slug='fluid_mechanics', no concept_slug)
    reads the REAL bernoulli dir and mints the known counts: 14 concept + 8
    variable entities, 16 prereqs, both misconceptions opposes-linked (one of
    which opposes the authored def.pressure_velocity_tradeoff)."""
    await _insert_course(db_url, 1)
    subject_id = await _insert_subject(db_url, slug="fluid_mechanics", space_id=1)
    cid = await _insert_concept_with_problems(
        db_url,
        subject_id=subject_id,
        slug="bernoulli_principle",
        problems=[_load_bernoulli_problem(n) for n in range(1, 6)],
    )

    stats = await seed(db_url, write_disk=False)  # all defaults

    assert stats["concepts_seeded"] == 1
    assert stats["misconceptions_linked"] == 2

    ents = await _fetch_entities(db_url, cid)
    by_kind: dict[str, int] = {}
    for e in ents:
        by_kind[e.kind] = by_kind.get(e.kind, 0) + 1
    assert by_kind["concept"] == 14
    assert by_kind["variable"] == 8

    keys = {e.canonical_key for e in ents}
    # The authored definition (not a reference node) is still minted for bernoulli.
    assert "def.pressure_velocity_tradeoff" in keys

    # density_ignored opposes a real reference key; pressure_velocity opposes the
    # authored definition — both resolve to a real entity id.
    misc = {e.canonical_key: e for e in ents if e.kind == "misconception"}
    assert misc["misc.density_ignored"].payload["opposes_entity_key"] == ("cond.incompressibility")
    pv = misc["misc.pressure_velocity_same_direction"]
    assert pv.payload["opposes_entity_key"] == "def.pressure_velocity_tradeoff"
    assert pv.payload["opposes_entity_id"] is not None

    # 16 prereq edges.
    engine = create_async_engine(db_url)
    Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    try:
        async with Session() as s:
            n_edges = len(
                (
                    await s.execute(
                        select(EntityPrereq)
                        .join(KGEntity, KGEntity.id == EntityPrereq.from_entity_id)
                        .where(KGEntity.concept_id == cid)
                    )
                )
                .scalars()
                .all()
            )
    finally:
        await engine.dispose()
    assert n_edges == 16


@pytest.mark.asyncio
async def test_seed_idempotent_second_run_inserts_nothing(monkeypatch, tmp_path, db_url):
    """Re-running the generic seed inserts no new rows (idempotent upsert)."""
    monkeypatch.setattr(seeder, "_SUBJECTS_ROOT", tmp_path)
    _make_macro_subject(tmp_path)

    await _insert_course(db_url, 1)
    subject_id = await _insert_subject(db_url, slug="macroeconomics", space_id=1)
    cid = await _insert_concept_with_problems(
        db_url,
        subject_id=subject_id,
        slug="gdp_components",
        problems=[_macro_problem("gdp_identity")],
    )

    first = await seed(
        db_url, subject_slug="macroeconomics", concept_slug="gdp_components", write_disk=False
    )
    assert first["entities_inserted"] > 0
    before = len(await _fetch_entities(db_url, cid))

    second = await seed(
        db_url, subject_slug="macroeconomics", concept_slug="gdp_components", write_disk=False
    )
    assert second["entities_inserted"] == 0
    assert second["prereqs_inserted"] == 0
    assert second["entities_updated"] > 0  # the upsert update branch ran
    assert len(await _fetch_entities(db_url, cid)) == before


# ---------------------------------------------------------------------------
# Error paths (SeedError) — generic resolver
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_seed_errors_when_subject_missing(db_url):
    """A course with no matching subject raises SeedError."""
    await _insert_course(db_url, 1)
    with pytest.raises(seeder.SeedError, match="no 'macroeconomics' subject"):
        await seed(db_url, subject_slug="macroeconomics", write_disk=False)


@pytest.mark.asyncio
async def test_seed_errors_when_concept_slug_absent(monkeypatch, tmp_path, db_url):
    """A subject that exists but lacks the requested concept raises SeedError."""
    monkeypatch.setattr(seeder, "_SUBJECTS_ROOT", tmp_path)
    _make_macro_subject(tmp_path)
    await _insert_course(db_url, 1)
    subject_id = await _insert_subject(db_url, slug="macroeconomics", space_id=1)
    await _insert_concept_with_problems(
        db_url,
        subject_id=subject_id,
        slug="gdp_components",
        problems=[_macro_problem("gdp_identity")],
    )
    with pytest.raises(seeder.SeedError, match="no concept 'no_such_concept'"):
        await seed(
            db_url,
            subject_slug="macroeconomics",
            concept_slug="no_such_concept",
            write_disk=False,
        )


@pytest.mark.asyncio
async def test_seed_errors_when_no_courses(db_url):
    """No courses rows -> SeedError (no course to attribute the seed)."""
    with pytest.raises(seeder.SeedError, match="no app.courses"):
        await seed(db_url, subject_slug="macroeconomics", write_disk=False)


@pytest.mark.asyncio
async def test_seed_dry_run_rolls_back(monkeypatch, tmp_path, db_url):
    """dry_run computes stats but writes nothing."""
    monkeypatch.setattr(seeder, "_SUBJECTS_ROOT", tmp_path)
    _make_macro_subject(tmp_path)
    await _insert_course(db_url, 1)
    subject_id = await _insert_subject(db_url, slug="macroeconomics", space_id=1)
    cid = await _insert_concept_with_problems(
        db_url,
        subject_id=subject_id,
        slug="gdp_components",
        problems=[_macro_problem("gdp_identity")],
    )
    stats = await seed(
        db_url,
        subject_slug="macroeconomics",
        concept_slug="gdp_components",
        dry_run=True,
        write_disk=False,
    )
    assert stats["entities_inserted"] > 0  # would-be inserts counted
    assert len(await _fetch_entities(db_url, cid)) == 0  # rolled back


# ---------------------------------------------------------------------------
# CLI threading of the new args (no DB) — main() wires subject/concept slugs
# ---------------------------------------------------------------------------


def test_main_threads_subject_and_concept_slugs(monkeypatch):
    """main() forwards --subject-slug / --concept-slug to seed()."""
    captured: dict[str, object] = {}

    async def _fake_seed(database_url: str, **kwargs):
        captured["database_url"] = database_url
        captured.update(kwargs)
        return {"concepts_seeded": 0}

    monkeypatch.setattr(seeder, "seed", _fake_seed)
    rc = seeder.main(
        [
            "--database-url",
            "postgresql+asyncpg://u@h/d",
            "--subject-slug",
            "macroeconomics",
            "--concept-slug",
            "gdp_components",
            "--no-write-disk",
        ]
    )
    assert rc == 0
    assert captured["subject_slug"] == "macroeconomics"
    assert captured["concept_slug"] == "gdp_components"
    assert captured["write_disk"] is False


def test_main_defaults_subject_to_fluid_mechanics(monkeypatch):
    captured: dict[str, object] = {}

    async def _fake_seed(database_url: str, **kwargs):
        captured.update(kwargs)
        return {}

    monkeypatch.setattr(seeder, "seed", _fake_seed)
    rc = seeder.main(["--database-url", "postgresql+asyncpg://u@h/d", "--dry-run"])
    assert rc == 0
    assert captured["subject_slug"] == "fluid_mechanics"
    assert captured["concept_slug"] is None


# ---------------------------------------------------------------------------
# Remaining changed-line coverage: explicit search_space_id, write_disk=True,
# the source_subject_slug override, and the seeder edge branches.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_seed_explicit_search_space_id_and_source_override(monkeypatch, tmp_path, db_url):
    """An explicit search_space_id is honored (skips the MIN(id) default), and
    source_subject_slug points the on-disk reads at a different physical dir than
    the DB subject slug (the one-dir/many-courses case)."""
    monkeypatch.setattr(seeder, "_SUBJECTS_ROOT", tmp_path)
    _make_macro_subject(tmp_path, subject="macroeconomics")  # physical dir

    await _insert_course(db_url, 5)
    await _insert_course(db_url, 9)
    # DB subject slug differs from the physical source dir name.
    subject_id = await _insert_subject(db_url, slug="macro_course_9", space_id=9)
    cid = await _insert_concept_with_problems(
        db_url,
        subject_id=subject_id,
        slug="gdp_components",
        problems=[_macro_problem("gdp_identity")],
    )

    stats = await seed(
        db_url,
        subject_slug="macro_course_9",
        source_subject_slug="macroeconomics",
        search_space_id=9,  # explicit; not MIN(id)=5
        write_disk=False,
    )
    assert stats["concepts_seeded"] == 1
    assert len(await _fetch_entities(db_url, cid)) > 0


@pytest.mark.asyncio
async def test_seed_write_disk_mirrors_annotation_to_json(monkeypatch, tmp_path, db_url):
    """write_disk=True writes the annotated payload back to the concept dir's
    problem_*.json (additive entity_key / declared_paths / layer1_seeded)."""
    monkeypatch.setattr(seeder, "_SUBJECTS_ROOT", tmp_path)
    _make_macro_subject(tmp_path)

    await _insert_course(db_url, 1)
    subject_id = await _insert_subject(db_url, slug="macroeconomics", space_id=1)
    await _insert_concept_with_problems(
        db_url,
        subject_id=subject_id,
        slug="gdp_components",
        problems=[_macro_problem("gdp_identity")],
    )

    await seed(
        db_url, subject_slug="macroeconomics", concept_slug="gdp_components", write_disk=True
    )

    disk_file = (
        tmp_path / "macroeconomics" / "concepts" / "gdp_components" / "problems" / "problem_01.json"
    )
    on_disk = json.loads(disk_file.read_text(encoding="utf-8"))
    assert on_disk["layer1_seeded"] is True
    assert "declared_paths" in on_disk
    assert all(step["entity_key"] for step in on_disk["reference_solution"])


@pytest.mark.asyncio
async def test_seed_raises_on_unknown_opposes_key(monkeypatch, tmp_path, db_url):
    """A misconception opposing a key the seed does NOT mint raises SeedError in
    the second (opposes-link) pass."""
    monkeypatch.setattr(seeder, "_SUBJECTS_ROOT", tmp_path)
    # Author a concept whose misconception opposes a non-existent key.
    _write_concept_dir(
        tmp_path,
        subject="brokensub",
        concept="brokencpt",
        dag={"nodes": [{"id": "brokencpt"}], "edges": []},
        symbols={"symbols": ["X"], "description": {}},
        normalization={},
        misconceptions={
            "misconceptions": [
                {
                    "key": "misc.dangling",
                    "display_name": "Dangling",
                    "description": "",
                    "opposes": "cond.does_not_exist",
                    "trigger_phrases": [],
                }
            ]
        },
        problems=[_macro_problem("p1")],
    )

    await _insert_course(db_url, 1)
    subject_id = await _insert_subject(db_url, slug="brokensub", space_id=1)
    await _insert_concept_with_problems(
        db_url,
        subject_id=subject_id,
        slug="brokencpt",
        problems=[_macro_problem("p1")],
    )

    with pytest.raises(seeder.SeedError, match="opposes unknown key"):
        await seed(db_url, subject_slug="brokensub", write_disk=False)


def test_disk_problem_paths_missing_dir_returns_empty(tmp_path):
    """_disk_problem_paths on a concept dir with no problems/ subdir returns {}."""
    from scripts.seed_apollo_learner_model import _disk_problem_paths

    cdir = tmp_path / "s" / "concepts" / "c"
    cdir.mkdir(parents=True)
    assert _disk_problem_paths(cdir) == {}
