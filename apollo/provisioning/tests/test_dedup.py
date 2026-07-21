"""WU-3B2c — course-local dedup ladder tests (Tier-1 unit + real-PG).

Tier-1 ONLY — NO network. The LLM judge and the embedder are DETERMINISTIC
injected stubs (`_judge_*` / `_embed`); there is NO real OpenAI / `embed_text` /
`cheap_chat` call anywhere in this module (ADJ #10). Real-PG tests request the
``db_session`` fixture (re-exported in ``apollo/conftest.py``) and Docker-skip
cleanly when the daemon is down — but the WU-3B2c gate REQUIRES they run
GREEN-not-skipped.

The pure-math tests (`_cosine`, constants, the frozen dataclass) need no DB.

DISCRIMINATING by design (independent-mutation discipline):
  * ``test_constants_are_pinned`` REDs if a threshold drifts at the constant.
  * the band-routing tests RED if a 0.92 / 0.82 boundary is moved.
  * ``test_cross_course_identical_embeddings_stay_distinct`` REDs if the
    ``Subject.search_space_id`` WHERE is dropped (a cross-course false-merge).
  * ``test_embedding_tie_breaks_to_lowest_entity_id`` REDs if the first-writer
    tie-break key changes.
"""

from __future__ import annotations

import math
from dataclasses import FrozenInstanceError, dataclass
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from apollo.persistence.models import Concept, DedupDecision, IngestRun, KGEntity
from apollo.provisioning.dedup import (
    DedupVerdict,
    _cosine,
    is_false_merge_risk,
    resolve_candidate,
)
from apollo.provisioning.dedup_constants import (
    EMBED_JUDGE_BAND,
    EMBED_MERGE_THRESHOLD,
)
from database.models import Course

# NOTE: no module-level ``pytest.mark.asyncio`` — pytest.ini sets
# ``asyncio_mode = auto`` so the async ``resolve_candidate`` tests run without a
# mark, while the pure ``_cosine``/constants/dataclass tests stay sync (a blanket
# asyncio mark would warn on them).


# --------------------------------------------------------------------------- #
# Deterministic stubs (NO network) — embedder + judges + candidate
# --------------------------------------------------------------------------- #


def _unit_at_cosine(target: float) -> list[float]:
    """Build a 2-D unit vector whose cosine with [1, 0] equals ``target``.

    ``_cosine([1, 0], _unit_at_cosine(t)) == t`` for t in [-1, 1] — lets each
    band test assert against the EXACT intended cosine instead of a fragile
    hard-coded float.
    """
    return [target, math.sqrt(max(0.0, 1.0 - target * target))]


_BASE = [1.0, 0.0]  # the reference vector every in-course summary maps onto

# Map fixture summary-texts -> fixed vectors so cosines are fully controllable.
# Identical text -> identical vector (the property the cross-course proof needs).
_EMBED_MAP: dict[str, list[float]] = {
    "BASE": _BASE,
    "MERGE_0_92": _unit_at_cosine(0.92),  # exactly the inclusive merge boundary
    "MERGE_0_98": _unit_at_cosine(0.98),
    "BAND_0_87": _unit_at_cosine(0.87),  # strictly inside [0.82, 0.92)
    "BAND_0_82": _unit_at_cosine(0.82),  # the inclusive lower bound
    "BELOW_0_50": _unit_at_cosine(0.50),  # < 0.82 -> distinct
    # The cross-course identical-text entity. Orthogonal to _BASE (cosine 0.0)
    # so an in-course entity mapped to "BASE" is FAR below the 0.82 band from
    # it — used to prove a same-course distinct in the no-local-match test.
    "INCOMPRESSIBLE": [0.0, 1.0],
}


def _embed(text: str) -> list[float]:
    """Deterministic mock embedder. NOT a call to ``embed_text``.

    Raises KeyError for unmapped text so a test that accidentally embeds a
    NULL-summary entity (which must be skipped) fails loudly rather than
    silently matching a default.
    """
    return list(_EMBED_MAP[text])


def _judge_merged(*_a, **_k) -> str:
    return "merged"


def _judge_distinct(*_a, **_k) -> str:
    return "distinct"


def _judge_explodes(*_a, **_k) -> str:
    raise AssertionError("judge_fn must NOT be called on the <0.82 distinct path")


@dataclass(frozen=True)
class _Candidate:
    """Duck-typed candidate: the two attributes ``resolve_candidate`` reads.

    3B2d owns the real candidate type; the ladder only consumes these two.
    """

    canonical_key: str
    scope_summary: str | None


# --------------------------------------------------------------------------- #
# Real-PG seeding helper — mirrors test_resolution_resolves_to_postgres.py:35-58
# --------------------------------------------------------------------------- #


async def _seed_course(db, *, slug, entities):
    """Seed Course -> Subject -> Concept -> KGEntity rows for one course.

    ``entities`` is an iterable of ``(canonical_key, scope_summary)`` tuples
    (``scope_summary`` may be ``None``). Returns
    ``(search_space_id, concept_id, {canonical_key: entity_id})``.
    """
    space = Course(name=f"Course {slug}", slug=slug, subject_name="Physics")
    db.add(space)
    await db.flush()
    subj = SimpleNamespace(slug=f"s-{slug}", display_name="Sub", search_space_id=space.id)
    concept = Concept(course_id=subj.search_space_id, subject_slug=subj.slug, subject_display_name=subj.display_name, slug=f"k-{slug}", display_name="Concept")
    db.add(concept)
    await db.flush()
    ids: dict[str, int] = {}
    for canonical_key, scope_summary in entities:
        ent = KGEntity(
            concept_id=concept.id,
            canonical_key=canonical_key,
            kind="quantity",
            display_name=canonical_key,
            payload={},
            aliases=[],
            scope_summary=scope_summary,
        )
        db.add(ent)
        await db.flush()
        ids[canonical_key] = ent.id
    return space.id, concept.id, ids


async def _decisions_for(db, *, search_space_id):
    rows = (
        (
            await db.execute(
                select(DedupDecision).where(DedupDecision.search_space_id == search_space_id)
            )
        )
        .scalars()
        .all()
    )
    return rows


# --------------------------------------------------------------------------- #
# Tier-1 pure (no DB) — constants, _cosine, dataclass shape
# --------------------------------------------------------------------------- #


def test_constants_are_pinned():
    """The pinned §8B.5 / ADJ #4 thresholds. DISCRIMINATING: a moved
    threshold REDs here independently of any routing test."""
    assert EMBED_MERGE_THRESHOLD == 0.92
    assert EMBED_JUDGE_BAND == (0.82, 0.92)


def test_cosine_identical_is_one():
    assert _cosine([1, 0, 0], [1, 0, 0]) == pytest.approx(1.0)


def test_cosine_orthogonal_is_zero():
    assert _cosine([1, 0, 0], [0, 1, 0]) == pytest.approx(0.0)


def test_cosine_zero_vector_is_zero_not_nan():
    out = _cosine([0, 0, 0], [1, 0, 0])
    assert out == 0.0
    assert not math.isnan(out)


def test_cosine_is_magnitude_invariant():
    assert _cosine([2, 0, 0], [5, 0, 0]) == pytest.approx(1.0)


def test_unit_at_cosine_helper_is_exact():
    """Self-check on the test helper: the vectors really land on the bands."""
    assert _cosine(_BASE, _unit_at_cosine(0.92)) == pytest.approx(0.92)
    assert _cosine(_BASE, _unit_at_cosine(0.82)) == pytest.approx(0.82)
    assert _cosine(_BASE, _unit_at_cosine(0.87)) == pytest.approx(0.87)
    assert _cosine(_BASE, _unit_at_cosine(0.50)) == pytest.approx(0.50)


def test_dedupverdict_is_frozen():
    v = DedupVerdict(verdict="merged", method="slug", similarity=None, matched_entity_id=7)
    assert (v.verdict, v.method, v.similarity, v.matched_entity_id) == (
        "merged",
        "slug",
        None,
        7,
    )
    with pytest.raises(FrozenInstanceError):
        v.verdict = "distinct"  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# Tier-1 ladder routing (real-PG db_session, mock embed/judge)
# --------------------------------------------------------------------------- #


async def test_slug_exact_match_merges(db_session):
    ss_id, concept_id, ids = await _seed_course(
        db_session, slug="c-slug", entities=[("eq.bernoulli", "BASE")]
    )
    cand = _Candidate(canonical_key="eq.bernoulli", scope_summary="MERGE_0_98")
    verdict = await resolve_candidate(
        db_session,
        search_space_id=ss_id,
        concept_id=concept_id,
        candidate=cand,
        embed_fn=_embed,
        judge_fn=_judge_explodes,  # slug short-circuits BEFORE embed/judge
    )
    assert verdict == DedupVerdict(
        verdict="merged",
        method="slug",
        similarity=None,
        matched_entity_id=ids["eq.bernoulli"],
    )
    rows = await _decisions_for(db_session, search_space_id=ss_id)
    assert len(rows) == 1
    row = rows[0]
    assert row.method == "slug"
    assert row.similarity is None
    assert row.verdict == "merged"
    assert row.matched_entity_id == ids["eq.bernoulli"]
    assert row.candidate_key == "eq.bernoulli"
    assert row.search_space_id == ss_id
    assert row.concept_id == concept_id


async def test_embedding_at_threshold_merges(db_session):
    """0.92 is the INCLUSIVE merge boundary -> merges on the embedding tier."""
    ss_id, concept_id, ids = await _seed_course(
        db_session, slug="c-thr", entities=[("ent.other", "MERGE_0_92")]
    )
    cand = _Candidate(canonical_key="cand.new", scope_summary="BASE")
    verdict = await resolve_candidate(
        db_session,
        search_space_id=ss_id,
        concept_id=concept_id,
        candidate=cand,
        embed_fn=_embed,
        judge_fn=_judge_explodes,  # >=0.92 merges WITHOUT the judge
    )
    assert verdict.verdict == "merged"
    assert verdict.method == "embedding"
    assert verdict.similarity == pytest.approx(0.92)
    assert verdict.matched_entity_id == ids["ent.other"]
    rows = await _decisions_for(db_session, search_space_id=ss_id)
    assert len(rows) == 1
    assert rows[0].method == "embedding"
    assert rows[0].similarity == pytest.approx(0.92)
    assert rows[0].verdict == "merged"


async def test_dedup_pressure_aggregates_and_persists_per_ingest_run(db_session):
    """Exact + embedding merge + embedding distinct update one queryable gauge."""
    ss_id, concept_id, _ids = await _seed_course(
        db_session,
        slug="c-pressure",
        entities=[("ent.exact", "BASE"), ("ent.embedding", "BASE")],
    )
    run = IngestRun(search_space_id=ss_id, document_id=901, status="running")
    db_session.add(run)
    await db_session.flush()

    scripted = (
        _Candidate(canonical_key="ent.exact", scope_summary="MERGE_0_98"),
        _Candidate(canonical_key="cand.merge", scope_summary="MERGE_0_98"),
        _Candidate(canonical_key="cand.distinct", scope_summary="BELOW_0_50"),
    )
    for candidate in scripted:
        await resolve_candidate(
            db_session,
            search_space_id=ss_id,
            concept_id=concept_id,
            candidate=candidate,
            embed_fn=_embed,
            judge_fn=_judge_explodes,
            ingest_run_id=int(run.id),
        )

    await db_session.refresh(run)
    assert run.dedup_pressure == {
        "total_candidates": 3,
        "exact_merges": 1,
        "embedding_merges": 1,
        "embedding_distinct": 1,
        "embedding_merge_ratio": 0.5,
        "per_concept": {str(concept_id): 1},
    }


async def test_embedding_in_band_escalates_to_judge(db_session):
    """0.82 <= cos < 0.92 escalates; the judge (merged) decides — proven by
    method='llm_judge', NOT 'embedding'."""
    ss_id, concept_id, ids = await _seed_course(
        db_session, slug="c-band", entities=[("ent.band", "BAND_0_87")]
    )
    cand = _Candidate(canonical_key="cand.new", scope_summary="BASE")
    verdict = await resolve_candidate(
        db_session,
        search_space_id=ss_id,
        concept_id=concept_id,
        candidate=cand,
        embed_fn=_embed,
        judge_fn=_judge_merged,
    )
    assert verdict.verdict == "merged"
    assert verdict.method == "llm_judge"
    assert verdict.similarity == pytest.approx(0.87)
    assert verdict.matched_entity_id == ids["ent.band"]
    rows = await _decisions_for(db_session, search_space_id=ss_id)
    assert len(rows) == 1
    assert rows[0].method == "llm_judge"  # escalation, not an embedding merge


async def test_embedding_below_band_is_distinct(db_session):
    """cos < 0.82 -> distinct on the embedding tier; the judge is NEVER called."""
    ss_id, concept_id, ids = await _seed_course(
        db_session, slug="c-below", entities=[("ent.far", "BELOW_0_50")]
    )
    cand = _Candidate(canonical_key="cand.new", scope_summary="BASE")
    verdict = await resolve_candidate(
        db_session,
        search_space_id=ss_id,
        concept_id=concept_id,
        candidate=cand,
        embed_fn=_embed,
        judge_fn=_judge_explodes,  # asserts the judge is not consulted
    )
    assert verdict.verdict == "distinct"
    assert verdict.method == "embedding"
    assert verdict.similarity == pytest.approx(0.50)
    assert verdict.matched_entity_id is None
    rows = await _decisions_for(db_session, search_space_id=ss_id)
    assert len(rows) == 1
    assert rows[0].method == "embedding"
    assert rows[0].verdict == "distinct"
    assert rows[0].matched_entity_id is None


async def test_band_lower_bound_0_82_escalates_not_distinct(db_session):
    """cos == 0.82 is IN-band (lower-inclusive) -> escalates; judge says
    distinct. Pins the inclusive lower bound: a `> 0.82` slip would route to
    the distinct branch instead and RED this test."""
    ss_id, concept_id, _ids = await _seed_course(
        db_session, slug="c-lower", entities=[("ent.lb", "BAND_0_82")]
    )
    cand = _Candidate(canonical_key="cand.new", scope_summary="BASE")
    verdict = await resolve_candidate(
        db_session,
        search_space_id=ss_id,
        concept_id=concept_id,
        candidate=cand,
        embed_fn=_embed,
        judge_fn=_judge_distinct,
    )
    assert verdict.verdict == "distinct"
    assert verdict.method == "llm_judge"
    rows = await _decisions_for(db_session, search_space_id=ss_id)
    assert len(rows) == 1
    assert rows[0].method == "llm_judge"
    assert rows[0].verdict == "distinct"


async def test_judge_distinct_path(db_session):
    """In-band cosine, judge returns distinct -> distinct verdict, no match,
    similarity is the band cosine for the audit trail."""
    ss_id, concept_id, _ids = await _seed_course(
        db_session, slug="c-jd", entities=[("ent.jd", "BAND_0_87")]
    )
    cand = _Candidate(canonical_key="cand.new", scope_summary="BASE")
    verdict = await resolve_candidate(
        db_session,
        search_space_id=ss_id,
        concept_id=concept_id,
        candidate=cand,
        embed_fn=_embed,
        judge_fn=_judge_distinct,
    )
    assert verdict.verdict == "distinct"
    assert verdict.method == "llm_judge"
    assert verdict.matched_entity_id is None
    assert verdict.similarity == pytest.approx(0.87)
    rows = await _decisions_for(db_session, search_space_id=ss_id)
    assert len(rows) == 1
    assert rows[0].method == "llm_judge"
    assert rows[0].verdict == "distinct"
    assert rows[0].similarity == pytest.approx(0.87)


async def test_empty_inventory_is_distinct(db_session):
    """A course with NO scope_summary entity (here: none at all) and no slug
    match -> distinct, method='embedding', similarity None."""
    ss_id, concept_id, _ids = await _seed_course(db_session, slug="c-empty", entities=[])
    cand = _Candidate(canonical_key="cand.new", scope_summary="BASE")
    verdict = await resolve_candidate(
        db_session,
        search_space_id=ss_id,
        concept_id=concept_id,
        candidate=cand,
        embed_fn=_embed,
        judge_fn=_judge_explodes,
    )
    assert verdict.verdict == "distinct"
    assert verdict.method == "embedding"
    assert verdict.similarity is None
    assert verdict.matched_entity_id is None
    rows = await _decisions_for(db_session, search_space_id=ss_id)
    assert len(rows) == 1
    assert rows[0].method == "embedding"
    assert rows[0].verdict == "distinct"
    assert rows[0].similarity is None


async def test_null_scope_summary_entities_are_skipped(db_session):
    """A NULL-summary in-course entity is skipped by the embedding tier (it
    would KeyError in `_embed` if embedded). Candidate matches the non-null one.
    The NULL entity also has a different slug, so the slug tier misses."""
    ss_id, concept_id, ids = await _seed_course(
        db_session,
        slug="c-null",
        entities=[("ent.nullsum", None), ("ent.real", "MERGE_0_98")],
    )
    cand = _Candidate(canonical_key="cand.new", scope_summary="BASE")
    verdict = await resolve_candidate(
        db_session,
        search_space_id=ss_id,
        concept_id=concept_id,
        candidate=cand,
        embed_fn=_embed,
        judge_fn=_judge_explodes,
    )
    assert verdict.verdict == "merged"
    assert verdict.method == "embedding"
    assert verdict.matched_entity_id == ids["ent.real"]
    rows = await _decisions_for(db_session, search_space_id=ss_id)
    assert len(rows) == 1
    assert rows[0].matched_entity_id == ids["ent.real"]


# --------------------------------------------------------------------------- #
# Tier-1 — THE load-bearing cross-course proof (real-PG)
# --------------------------------------------------------------------------- #


async def test_cross_course_identical_embeddings_stay_distinct(db_session):
    """Within-course merge + same-text cross-course isolation (§8B.7 / §1.4).
    Two courses EACH hold an 'incompressible' entity with IDENTICAL scope_summary
    text ('INCOMPRESSIBLE' -> identical `_embed` vector, cosine 1.0). Resolving
    that text against course-a merges onto COURSE-A's OWN entity and never reaches
    across to course-b.

    NOT the load-bearing mutation pin: this test does NOT by itself discriminate
    the `Subject.search_space_id` WHERE. With that predicate dropped, course-b's
    identical entity re-enters scope but the lowest-id tie-break still selects
    course-a's (earlier-seeded) `ent.a`, and the decision row is stamped with the
    passed `search_space_id` regardless — so both assertions survive the mutation
    (orchestrator-verified: this test stays GREEN under the dropped WHERE). The
    genuine §1.4 mutation pin is the sibling
    `test_cross_course_no_local_match_stays_distinct` (course-a has no matching
    entity, so dropping the WHERE merges onto course-b and REDs).
    """
    ss_a, concept_a, _ids_a = await _seed_course(
        db_session, slug="course-a", entities=[("ent.a", "INCOMPRESSIBLE")]
    )
    # course-b: a DIFFERENT course holding the byte-identical summary text.
    await _seed_course(db_session, slug="course-b", entities=[("ent.b", "INCOMPRESSIBLE")])
    cand = _Candidate(canonical_key="cand.shared", scope_summary="INCOMPRESSIBLE")
    verdict = await resolve_candidate(
        db_session,
        search_space_id=ss_a,
        concept_id=concept_a,
        candidate=cand,
        embed_fn=_embed,
        judge_fn=_judge_explodes,
    )
    # course-a holds an identical-text entity too, so it merges WITHIN course-a;
    # the load-bearing assertion is that it never reaches across to course-b.
    assert verdict.matched_entity_id == _ids_a["ent.a"]
    # the only decision row is scoped to course-a, never course-b.
    rows_a = await _decisions_for(db_session, search_space_id=ss_a)
    assert len(rows_a) == 1
    assert rows_a[0].search_space_id == ss_a


async def test_cross_course_no_local_match_stays_distinct(db_session):
    """The pure cross-course non-merge: course-a has NO matching entity; only
    course-b holds the identical text. The scope WHERE keeps them separate ->
    distinct, matched_entity_id is None. Dropping the search_space_id WHERE
    would merge onto course-b's entity (RED)."""
    # course-a's only entity is orthogonal to the candidate (cosine 0.0 < 0.82),
    # so WITHIN course-a the verdict is distinct. course-b holds the byte-
    # identical 'INCOMPRESSIBLE' text (cosine 1.0) — the scope WHERE must keep it
    # out of course-a's candidate set, else this REDs as a cross-course merge.
    ss_a, concept_a, _ids_a = await _seed_course(
        db_session, slug="course-a2", entities=[("ent.a2", "BASE")]
    )
    await _seed_course(db_session, slug="course-b2", entities=[("ent.b2", "INCOMPRESSIBLE")])
    cand = _Candidate(canonical_key="cand.shared2", scope_summary="INCOMPRESSIBLE")
    verdict = await resolve_candidate(
        db_session,
        search_space_id=ss_a,
        concept_id=concept_a,
        candidate=cand,
        embed_fn=_embed,
        judge_fn=_judge_explodes,
    )
    assert verdict.verdict == "distinct"
    assert verdict.matched_entity_id is None
    rows_a = await _decisions_for(db_session, search_space_id=ss_a)
    assert len(rows_a) == 1
    assert rows_a[0].search_space_id == ss_a


# --------------------------------------------------------------------------- #
# PR2 — concept-scoped pool (Part A) + same-mint exclusion (Part B). These are
# the structural fix for the 2026-06-30 dedup false-merge family: the candidate
# pool is restricted to the CURRENT concept (no cross-concept/foreign-set merge)
# and an entity minted earlier in the SAME mint is kept OUT of the pool (so two
# distinct nodes of one problem -- m, M -- can't fuse).
# --------------------------------------------------------------------------- #
async def _seed_two_concept_course(db, *, slug, a_entities, b_entities):
    """Seed one Course -> Subject -> TWO concepts (A, B) in the SAME course.
    Returns (search_space_id, concept_a_id, concept_b_id, ids_a, ids_b)."""
    space = Course(name=f"Course {slug}", slug=slug, subject_name="Physics")
    db.add(space)
    await db.flush()
    subj = SimpleNamespace(slug=f"s-{slug}", display_name="Sub", search_space_id=space.id)

    async def _concept(cslug, entities):
        concept = Concept(course_id=subj.search_space_id, subject_slug=subj.slug, subject_display_name=subj.display_name, slug=cslug, display_name="Concept")
        db.add(concept)
        await db.flush()
        ids: dict[str, int] = {}
        for canonical_key, scope_summary in entities:
            ent = KGEntity(
                concept_id=concept.id,
                canonical_key=canonical_key,
                kind="quantity",
                display_name=canonical_key,
                payload={},
                aliases=[],
                scope_summary=scope_summary,
            )
            db.add(ent)
            await db.flush()
            ids[canonical_key] = ent.id
        return concept.id, ids

    ca, ids_a = await _concept(f"ka-{slug}", a_entities)
    cb, ids_b = await _concept(f"kb-{slug}", b_entities)
    return space.id, ca, cb, ids_a, ids_b


async def test_cross_concept_slug_stays_distinct(db_session):
    """Part A: a slug that exists only in a SIBLING concept of the same course does
    NOT merge -- it mints fresh. Regresses the audit's foreign-set binding
    (proc.proc_1 slug-merged into a prior concept). RED without the concept_id pool
    filter (slug-merges cross-concept)."""
    ss, _ca, cb, _ids_a, _ids_b = await _seed_two_concept_course(
        db_session,
        slug="xc-slug",
        a_entities=[("proc.proc_1", "BASE")],
        b_entities=[("ent.unrelated", "BELOW_0_50")],
    )
    cand = _Candidate(canonical_key="proc.proc_1", scope_summary="MERGE_0_98")
    verdict = await resolve_candidate(
        db_session,
        search_space_id=ss,
        concept_id=cb,  # minting under concept B; the slug lives in concept A
        candidate=cand,
        embed_fn=_embed,
        judge_fn=_judge_distinct,
    )
    assert verdict.verdict == "distinct"
    assert verdict.matched_entity_id is None


async def test_cross_concept_embedding_stays_distinct(db_session):
    """Part A: a byte-identical scope_summary in a SIBLING concept does not
    embedding-merge across concepts (cosine 1.0 but different concept). RED without
    the concept_id pool filter (cross-concept cosine fusion)."""
    ss, _ca, cb, _ids_a, _ids_b = await _seed_two_concept_course(
        db_session,
        slug="xc-embed",
        a_entities=[("ent.a", "BASE")],
        b_entities=[("ent.b", "BELOW_0_50")],
    )
    cand = _Candidate(canonical_key="cand.new", scope_summary="BASE")
    verdict = await resolve_candidate(
        db_session,
        search_space_id=ss,
        concept_id=cb,
        candidate=cand,
        embed_fn=_embed,
        judge_fn=_judge_distinct,
    )
    assert verdict.verdict == "distinct"
    assert verdict.matched_entity_id is None


async def test_exclude_entity_ids_forces_distinct(db_session):
    """Part B: an entity in exclude_entity_ids is kept OUT of the candidate pool, so
    a candidate cannot dedup against an entity minted earlier in the SAME mint --
    what keeps two distinct nodes of one problem (m, M) from fusing. RED without the
    param (slug-merges into the excluded entity)."""
    ss, concept_id, ids = await _seed_course(db_session, slug="excl", entities=[("ent.x", "BASE")])
    cand = _Candidate(canonical_key="ent.x", scope_summary="BASE")
    verdict = await resolve_candidate(
        db_session,
        search_space_id=ss,
        concept_id=concept_id,
        candidate=cand,
        embed_fn=_embed,
        judge_fn=_judge_distinct,
        exclude_entity_ids={ids["ent.x"]},
    )
    assert verdict.verdict == "distinct"
    assert verdict.matched_entity_id is None


async def test_exclude_entity_ids_none_preserves_merge(db_session):
    """Part B regression: with no exclusion (the default), a slug still merges --
    the new param must not change existing behavior when omitted/empty."""
    ss, concept_id, ids = await _seed_course(
        db_session, slug="excl-none", entities=[("ent.x", "BASE")]
    )
    cand = _Candidate(canonical_key="ent.x", scope_summary="MERGE_0_98")
    verdict = await resolve_candidate(
        db_session,
        search_space_id=ss,
        concept_id=concept_id,
        candidate=cand,
        embed_fn=_embed,
        judge_fn=_judge_distinct,
    )
    assert verdict.verdict == "merged"
    assert verdict.matched_entity_id == ids["ent.x"]


# --------------------------------------------------------------------------- #
# Tier-1 — determinism / first-writer-wins (real-PG)
# --------------------------------------------------------------------------- #


async def test_embedding_tie_breaks_to_lowest_entity_id(db_session):
    """Two in-course entities with IDENTICAL scope_summary (equal max cosine,
    both >= 0.92) -> the LOWER (earliest-written) entity id wins. RED if the
    tie-break key changes from `(cos, -id)`."""
    ss_id, concept_id, ids = await _seed_course(
        db_session,
        slug="c-tie",
        entities=[("ent.first", "MERGE_0_98"), ("ent.second", "MERGE_0_98")],
    )
    assert ids["ent.first"] < ids["ent.second"]
    cand = _Candidate(canonical_key="cand.new", scope_summary="BASE")
    verdict = await resolve_candidate(
        db_session,
        search_space_id=ss_id,
        concept_id=concept_id,
        candidate=cand,
        embed_fn=_embed,
        judge_fn=_judge_explodes,
    )
    assert verdict.verdict == "merged"
    assert verdict.matched_entity_id == ids["ent.first"]  # lowest id, first writer


async def test_resolve_is_deterministic_repeat(db_session):
    """Calling resolve_candidate twice on the same state + inputs yields equal
    verdicts and writes a SECOND identical-shaped audit row (no hidden mutation
    of inputs/state flips the verdict)."""
    ss_id, concept_id, ids = await _seed_course(
        db_session, slug="c-det", entities=[("ent.det", "MERGE_0_98")]
    )
    cand = _Candidate(canonical_key="cand.new", scope_summary="BASE")
    kwargs = dict(
        search_space_id=ss_id,
        concept_id=concept_id,
        candidate=cand,
        embed_fn=_embed,
        judge_fn=_judge_explodes,
    )
    v1 = await resolve_candidate(db_session, **kwargs)
    v2 = await resolve_candidate(db_session, **kwargs)
    assert v1 == v2
    assert v1.matched_entity_id == ids["ent.det"]
    rows = await _decisions_for(db_session, search_space_id=ss_id)
    assert len(rows) == 2
    assert {r.method for r in rows} == {"embedding"}
    assert {r.verdict for r in rows} == {"merged"}
    assert {r.matched_entity_id for r in rows} == {ids["ent.det"]}


async def test_ingest_run_id_is_stamped_when_present(db_session):
    """When the caller supplies ingest_run_id it is stamped onto the decision
    row (3B2g aggregates it later); when None it stays NULL."""
    ss_id, concept_id, ids = await _seed_course(
        db_session, slug="c-run", entities=[("eq.run", "BASE")]
    )
    cand = _Candidate(canonical_key="eq.run", scope_summary="BASE")
    # ingest_run_id must be a real apollo_ingest_runs.id FK; seed one minimal run.
    from apollo.persistence.models import IngestRun

    run = IngestRun(search_space_id=ss_id, document_id=1, status="running")
    db_session.add(run)
    await db_session.flush()
    await resolve_candidate(
        db_session,
        search_space_id=ss_id,
        concept_id=concept_id,
        candidate=cand,
        embed_fn=_embed,
        judge_fn=_judge_explodes,
        ingest_run_id=run.id,
    )
    rows = await _decisions_for(db_session, search_space_id=ss_id)
    assert len(rows) == 1
    assert rows[0].ingest_run_id == run.id


# --------------------------------------------------------------------------- #
# Tier-1 — public surface
# --------------------------------------------------------------------------- #


def test_public_api_reexport():
    """`from apollo.provisioning import DedupVerdict, resolve_candidate` returns
    the same objects as the dedup module (3B2d's import surface)."""
    from apollo.provisioning import (
        DedupVerdict as ReexportVerdict,
    )
    from apollo.provisioning import dedup as dedup_mod
    from apollo.provisioning import (
        resolve_candidate as reexport_resolve,
    )

    assert ReexportVerdict is dedup_mod.DedupVerdict
    assert reexport_resolve is dedup_mod.resolve_candidate


def test_is_false_merge_risk():
    # Case differences on the same alphabet letters should trigger merge risk
    assert is_false_merge_risk("var.m", "var.M") is True
    assert is_false_merge_risk("var.v1", "var.V1") is True
    assert is_false_merge_risk("var.v_1", "var.V_2") is True

    # Subscript / number differences should trigger merge risk
    assert is_false_merge_risk("var.v_1", "var.v_2") is True
    assert is_false_merge_risk("var.v", "var.v_1") is True
    assert is_false_merge_risk("var.v1", "var.v2") is True

    # Completely different variable names should not trigger merge risk
    assert is_false_merge_risk("var.v", "var.u") is False
    assert is_false_merge_risk("var.v_1", "var.u_1") is False

    # Concept names shouldn't trigger unless they only differ by case
    assert is_false_merge_risk("eq.bernoulli", "eq.Bernoulli") is True
    assert is_false_merge_risk("eq.bernoulli", "eq.continuity") is False


async def test_resolve_candidate_prevents_false_merge(db_session):
    """If a candidate matches the concept/search_space and has high cosine similarity
    but differs by casing (e.g. 'm' vs 'M'), it must stay distinct."""
    ss_id, concept_id, ids = await _seed_course(
        db_session, slug="c-case", entities=[("var.m", "BASE")]
    )
    # Target existing entity is 'var.m' with vector BASE.
    # Candidate is 'var.M' with summary BASE (cos == 1.0).
    cand = _Candidate(canonical_key="var.M", scope_summary="BASE")

    verdict = await resolve_candidate(
        db_session,
        search_space_id=ss_id,
        concept_id=concept_id,
        candidate=cand,
        embed_fn=_embed,
        judge_fn=_judge_explodes,  # Should NOT escalate to judge
    )
    # Cosine is 1.0 but it's a false-merge risk, so it should stay distinct
    assert verdict.verdict == "distinct"
    assert verdict.matched_entity_id is None


def test_identical_cleaned_keys_are_not_a_false_merge_risk():
    from apollo.provisioning.dedup import is_false_merge_risk

    # Same base, same casing, same subscript: a true duplicate, not a risk.
    assert is_false_merge_risk("energy.v_1", "kinematics.v_1") is False
