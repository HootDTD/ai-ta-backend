"""RED->GREEN tests for the misconception-detector orchestrator (T9).

Frozen contract: ``docs/_archive/plans/2026-07-08-apollo-misconception-detector-plan.md``
section 5.8, amended by A1/A2/A5 as they bear on this module.

``detect_misconceptions`` is pure aggregation across the three detection
tiers (sympy_veto, bank_pattern, judge) plus bank loading. It does NOT gate
or merge — that happens downstream in ``done.py`` via ``gate.py``/``merge.py``
(kept separate so the graph grader can reuse just the detection step). Every
external touchpoint (bank load, judge, embed) is DI'd or soft-failed so this
suite never hits OpenAI, Postgres, or pgvector.

Key assertions (per task prompt):
  * aggregates sympy_veto + bank_pattern + judge into ONE DetectionResult
  * a tier raising -> 0 findings from THAT tier, others still returned
    (soft-fail; the whole call never raises)
  * empty bank -> sympy_veto/bank_pattern abstain, judge-only findings
  * DI judge_fn/embed_fn are plain stubs -- never any live OpenAI/network call
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from apollo.ontology.graph import KGGraph
from apollo.ontology.nodes import build_node
from apollo.overseer.misconception_bank import MisconceptionEntry
from apollo.overseer.misconception_detector.detector import detect_misconceptions
from apollo.overseer.misconception_detector.types import DetectionResult, JudgeRaw
from apollo.persistence.models import Concept, Misconception, Subject
from database.models import Base


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest_asyncio.fixture
async def sqlite_db():
    """Bare in-memory SQLite session (mirrors test_misconception_detector_bank_pattern.py).

    ``load_for_concept`` issues a real SELECT against ``apollo_misconceptions``
    here, so the bank tables are created for real (unlike bank_pattern's own
    suite, which never queries them)."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    tables = [
        Subject.__table__,
        Concept.__table__,
        Misconception.__table__,
    ]
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: Base.metadata.create_all(sync_conn, tables=tables)
        )
    Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with Session() as s:
        yield s
    await engine.dispose()


async def _seed_bank_entry(
    db: AsyncSession,
    *,
    concept_id: int,
    code: str = "includes_transfers",
    trigger_phrases: tuple[str, ...] = (),
) -> None:
    # SQLite (test mode) does not enforce FK constraints by default, so a
    # dangling search_space_id/subject_id is fine here -- mirrors the same
    # bare-table pattern used by test_misconception_detector_bank_pattern.py.
    db.add(Subject(id=1, search_space_id=1, slug="macro", display_name="Macro"))
    db.add(
        Concept(
            id=concept_id,
            subject_id=1,
            slug="gdp-identity",
            display_name="GDP identity",
        )
    )
    db.add(
        Misconception(
            id=1,
            concept_id=concept_id,
            code=code,
            description="GDP includes transfer payments",
            confusion_pair_a=None,
            confusion_pair_b=None,
            trigger_phrases=list(trigger_phrases),
            probe_question="Are transfers part of GDP?",
            rt_steps=[],
        )
    )
    await db.flush()


def _eq_node(node_id: str, symbolic: str, label: str = "", *, attempt_id: int = 1):
    return build_node(
        node_type="equation",
        node_id=node_id,
        attempt_id=attempt_id,
        source="parser",
        content={"symbolic": symbolic, "label": label},
    )


def _def_node(node_id: str, concept: str, meaning: str, *, attempt_id: int = 1):
    return build_node(
        node_type="definition",
        node_id=node_id,
        attempt_id=attempt_id,
        source="parser",
        content={"concept": concept, "meaning": meaning},
    )


class _StubJudgeFn:
    """Records call count/args; returns a canned all-clear JudgeRaw by default."""

    def __init__(self, raw: JudgeRaw | None = None, *, raises: bool = False) -> None:
        self._raw = raw or JudgeRaw(content='{"concepts": []}', verdict_token_prob=None)
        self._raises = raises
        self.calls: list[dict] = []

    def __call__(self, *, system: str, user: str) -> JudgeRaw:
        self.calls.append({"system": system, "user": user})
        if self._raises:
            raise RuntimeError("simulated judge network failure")
        return self._raw


class _StubEmbedFn:
    """Deterministic offline embedding stub -- never touches OpenAI."""

    def __init__(self, *, raises: bool = False, vector: list[float] | None = None) -> None:
        self._raises = raises
        self._vector = vector or [1.0, 0.0]
        self.calls: list[str] = []

    def __call__(self, text: str) -> list[float]:
        self.calls.append(text)
        if self._raises:
            raise RuntimeError("simulated embedding failure")
        return self._vector


# --------------------------------------------------------------------------- #
# Aggregation across all three tiers
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_aggregates_all_three_tiers_into_one_detection_result(sqlite_db):
    """sympy_veto + bank_pattern + judge findings all land in one DetectionResult."""
    await _seed_bank_entry(
        sqlite_db,
        concept_id=42,
        code="sign_flip_continuity",
        trigger_phrases=("eq:A2*v2 - A1*v1",),
    )

    student_graph = KGGraph(nodes=[_eq_node("stu_eq1", "A2*v2 - A1*v1")])
    reference_graph = KGGraph(
        nodes=[_eq_node("ref_continuity", "A1*v1 - A2*v2", "Continuity")]
    )

    judge_fn = _StubJudgeFn(
        JudgeRaw(
            content=(
                '{"concepts": [{"concept_key": "ref_continuity", '
                '"verdict": "clear", "evidence_span": "", "confidence": 0.9}]}'
            ),
            verdict_token_prob=None,
        )
    )
    embed_fn = _StubEmbedFn()

    result = await detect_misconceptions(
        sqlite_db,
        attempt_id=1,
        concept_id=42,
        student_graph=student_graph,
        reference_graph=reference_graph,
        problem_text="Apply the continuity equation.",
        student_utterances=("flow rate goes A2*v2 - A1*v1",),
        judge_fn=judge_fn,
        embed_fn=embed_fn,
    )

    assert isinstance(result, DetectionResult)
    sources = {f.source for f in result.per_concept}
    # sympy_veto fires (sign-flip mutant); judge fires (one row per concept);
    # bank_pattern may or may not hit depending on the fixture embedding, but
    # both deterministic tiers below must be represented.
    assert "sympy_veto" in sources
    assert "judge" in sources
    assert judge_fn.calls, "judge_fn must be invoked exactly once (batched)"
    assert len(judge_fn.calls) == 1


@pytest.mark.asyncio
async def test_bank_pattern_finding_included_when_utterance_matches(sqlite_db):
    await _seed_bank_entry(sqlite_db, concept_id=42, code="includes_transfers")

    student_graph = KGGraph(nodes=[])
    reference_graph = KGGraph(
        nodes=[_def_node("ref_gdp", "GDP", "GDP excludes transfer payments")]
    )

    judge_fn = _StubJudgeFn()
    # Same vector for utterance + bank entry description -> cosine similarity 1.0,
    # comfortably above BANK_SIM_FLOOR.
    embed_fn = _StubEmbedFn(vector=[1.0, 0.0])

    result = await detect_misconceptions(
        sqlite_db,
        attempt_id=1,
        concept_id=42,
        student_graph=student_graph,
        reference_graph=reference_graph,
        problem_text="Define GDP.",
        student_utterances=("transfers are part of GDP",),
        judge_fn=judge_fn,
        embed_fn=embed_fn,
    )

    bank_findings = [f for f in result.per_concept if f.source == "bank_pattern"]
    assert len(bank_findings) == 1
    assert bank_findings[0].signature == "misc.includes_transfers"


# --------------------------------------------------------------------------- #
# Soft-fail: a raising tier contributes 0 findings, others still returned
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_judge_raising_yields_all_clear_judge_findings_others_unaffected(sqlite_db):
    """A raising JudgeFn must not propagate -- the orchestrator call itself
    never raises. ``judge_concepts`` soft-fails a raising JudgeFn to an
    all-`clear`, zero-confidence finding PER requested concept (T5's own
    documented contract) rather than omitting the concept outright; what this
    orchestrator-level test asserts is that (a) the crash never propagates and
    (b) sympy_veto's findings are still present alongside the soft-failed
    judge rows."""
    await _seed_bank_entry(
        sqlite_db,
        concept_id=42,
        code="sign_flip_continuity",
        trigger_phrases=("eq:A2*v2 - A1*v1",),
    )

    student_graph = KGGraph(nodes=[_eq_node("stu_eq1", "A2*v2 - A1*v1")])
    reference_graph = KGGraph(
        nodes=[_eq_node("ref_continuity", "A1*v1 - A2*v2", "Continuity")]
    )

    judge_fn = _StubJudgeFn(raises=True)
    embed_fn = _StubEmbedFn()

    result = await detect_misconceptions(
        sqlite_db,
        attempt_id=1,
        concept_id=42,
        student_graph=student_graph,
        reference_graph=reference_graph,
        problem_text="Apply the continuity equation.",
        student_utterances=(),
        judge_fn=judge_fn,
        embed_fn=embed_fn,
    )

    assert not result.is_empty
    judge_findings = [f for f in result.per_concept if f.source == "judge"]
    # Soft-failed to all-clear, zero-confidence -- never raised, never dropped.
    assert judge_findings
    assert all(f.verdict == "clear" and f.confidence == 0.0 for f in judge_findings)
    assert any(f.source == "sympy_veto" for f in result.per_concept)


@pytest.mark.asyncio
async def test_embed_fn_raising_yields_zero_bank_pattern_findings_others_unaffected(
    sqlite_db,
):
    """A raising EmbedFn must not propagate; sympy_veto/judge still contribute."""
    await _seed_bank_entry(
        sqlite_db,
        concept_id=42,
        code="sign_flip_continuity",
        trigger_phrases=("eq:A2*v2 - A1*v1",),
    )

    student_graph = KGGraph(nodes=[_eq_node("stu_eq1", "A2*v2 - A1*v1")])
    reference_graph = KGGraph(
        nodes=[_eq_node("ref_continuity", "A1*v1 - A2*v2", "Continuity")]
    )

    judge_fn = _StubJudgeFn(
        JudgeRaw(
            content=(
                '{"concepts": [{"concept_key": "ref_continuity", '
                '"verdict": "clear", "evidence_span": "", "confidence": 0.9}]}'
            ),
            verdict_token_prob=None,
        )
    )
    embed_fn = _StubEmbedFn(raises=True)

    result = await detect_misconceptions(
        sqlite_db,
        attempt_id=1,
        concept_id=42,
        student_graph=student_graph,
        reference_graph=reference_graph,
        problem_text="Apply the continuity equation.",
        student_utterances=("some student utterance",),
        judge_fn=judge_fn,
        embed_fn=embed_fn,
    )

    assert all(f.source != "bank_pattern" for f in result.per_concept)
    assert any(f.source == "sympy_veto" for f in result.per_concept)
    assert any(f.source == "judge" for f in result.per_concept)


@pytest.mark.asyncio
async def test_bank_load_raising_still_returns_judge_and_sympy_findings(sqlite_db, monkeypatch):
    """A raising bank load (e.g. a transient DB error) must not break the whole
    call -- sympy_veto degrades to an empty bank (no mutant match possible) and
    bank_pattern abstains, but judge still fires."""
    import apollo.overseer.misconception_detector.detector as detector_module

    async def _boom(db, *, concept_id):
        raise RuntimeError("simulated DB failure")

    monkeypatch.setattr(detector_module, "load_for_concept", _boom)

    student_graph = KGGraph(nodes=[_eq_node("stu_eq1", "A2*v2 - A1*v1")])
    reference_graph = KGGraph(
        nodes=[_eq_node("ref_continuity", "A1*v1 - A2*v2", "Continuity")]
    )

    judge_fn = _StubJudgeFn(
        JudgeRaw(
            content=(
                '{"concepts": [{"concept_key": "ref_continuity", '
                '"verdict": "misconception", "evidence_span": "x", "confidence": 0.9}]}'
            ),
            verdict_token_prob=None,
        )
    )
    embed_fn = _StubEmbedFn()

    result = await detect_misconceptions(
        sqlite_db,
        attempt_id=1,
        concept_id=42,
        student_graph=student_graph,
        reference_graph=reference_graph,
        problem_text="Apply the continuity equation.",
        student_utterances=("flow rate goes backwards",),
        judge_fn=judge_fn,
        embed_fn=embed_fn,
    )

    assert all(f.source != "bank_pattern" for f in result.per_concept)
    assert all(f.source != "sympy_veto" for f in result.per_concept)
    assert any(f.source == "judge" for f in result.per_concept)


# --------------------------------------------------------------------------- #
# Empty bank -> judge-only findings
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_empty_bank_yields_judge_only_findings(sqlite_db):
    """No concept_id / no bank rows -> sympy_veto and bank_pattern abstain
    entirely; the judge tier still runs and contributes findings."""
    student_graph = KGGraph(nodes=[_eq_node("stu_eq1", "A1*v1 - A2*v2")])
    reference_graph = KGGraph(
        nodes=[_eq_node("ref_continuity", "A1*v1 - A2*v2", "Continuity")]
    )

    judge_fn = _StubJudgeFn(
        JudgeRaw(
            content=(
                '{"concepts": [{"concept_key": "ref_continuity", '
                '"verdict": "clear", "evidence_span": "", "confidence": 0.95}]}'
            ),
            verdict_token_prob=None,
        )
    )
    embed_fn = _StubEmbedFn()

    result = await detect_misconceptions(
        sqlite_db,
        attempt_id=1,
        concept_id=None,
        student_graph=student_graph,
        reference_graph=reference_graph,
        problem_text="Apply the continuity equation.",
        student_utterances=("the flow is conserved",),
        judge_fn=judge_fn,
        embed_fn=embed_fn,
    )

    assert not result.is_empty
    assert all(f.source == "judge" for f in result.per_concept)


@pytest.mark.asyncio
async def test_empty_reference_graph_and_no_utterances_yields_empty_result(sqlite_db):
    """No reference concepts to judge, no student equations, no utterances,
    no concept_id -> every tier abstains -> an empty DetectionResult (not a
    crash, not a spurious finding)."""
    result = await detect_misconceptions(
        sqlite_db,
        attempt_id=1,
        concept_id=None,
        student_graph=KGGraph(nodes=[]),
        reference_graph=KGGraph(nodes=[]),
        problem_text="",
        student_utterances=(),
        judge_fn=_StubJudgeFn(),
        embed_fn=_StubEmbedFn(),
    )

    assert result.is_empty


# --------------------------------------------------------------------------- #
# DI seam: judge_fn/embed_fn are plain stubs, never live OpenAI
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_di_seam_never_touches_openai(sqlite_db):
    """Passing plain-object stubs (not `OpenAI` instances) must work end to
    end with zero network access -- this IS the DI-seam contract."""
    student_graph = KGGraph(nodes=[])
    reference_graph = KGGraph(
        nodes=[_def_node("ref_gdp", "GDP", "GDP excludes transfer payments")]
    )
    judge_fn = _StubJudgeFn()
    embed_fn = _StubEmbedFn()

    result = await detect_misconceptions(
        sqlite_db,
        attempt_id=1,
        concept_id=None,
        student_graph=student_graph,
        reference_graph=reference_graph,
        problem_text="Define GDP.",
        student_utterances=(),
        judge_fn=judge_fn,
        embed_fn=embed_fn,
    )

    assert isinstance(result, DetectionResult)
    # The judge is still invoked once (batched call for the one reference concept).
    assert len(judge_fn.calls) == 1
    # embed_fn was never called: no utterances and no concept_id => bank_pattern abstains.
    assert embed_fn.calls == []


# --------------------------------------------------------------------------- #
# Defense-in-depth: each tier wrapper's own try/except (in case a tier's
# documented "never raises" contract regresses in the future)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_sympy_veto_raising_is_soft_failed_by_the_wrapper(sqlite_db, monkeypatch):
    import apollo.overseer.misconception_detector.detector as detector_module

    def _boom(student_graph, reference_graph, *, bank_entries=()):
        raise RuntimeError("simulated sympy_veto crash")

    monkeypatch.setattr(detector_module, "detect_sign_veto", _boom)

    judge_fn = _StubJudgeFn(
        JudgeRaw(
            content=(
                '{"concepts": [{"concept_key": "ref_continuity", '
                '"verdict": "clear", "evidence_span": "", "confidence": 0.9}]}'
            ),
            verdict_token_prob=None,
        )
    )
    embed_fn = _StubEmbedFn()

    result = await detect_misconceptions(
        sqlite_db,
        attempt_id=1,
        concept_id=None,
        student_graph=KGGraph(nodes=[_eq_node("stu_eq1", "A1*v1 - A2*v2")]),
        reference_graph=KGGraph(
            nodes=[_eq_node("ref_continuity", "A1*v1 - A2*v2", "Continuity")]
        ),
        problem_text="Apply the continuity equation.",
        student_utterances=(),
        judge_fn=judge_fn,
        embed_fn=embed_fn,
    )

    assert all(f.source != "sympy_veto" for f in result.per_concept)
    assert any(f.source == "judge" for f in result.per_concept)


@pytest.mark.asyncio
async def test_bank_pattern_raising_is_soft_failed_by_the_wrapper(sqlite_db, monkeypatch):
    import apollo.overseer.misconception_detector.detector as detector_module

    async def _boom(db, *, concept_id, student_utterances, embed_fn, bank_entries):
        raise RuntimeError("simulated bank_pattern crash")

    monkeypatch.setattr(detector_module, "detect_bank_pattern", _boom)

    judge_fn = _StubJudgeFn(
        JudgeRaw(
            content=(
                '{"concepts": [{"concept_key": "ref_continuity", '
                '"verdict": "clear", "evidence_span": "", "confidence": 0.9}]}'
            ),
            verdict_token_prob=None,
        )
    )
    embed_fn = _StubEmbedFn()

    result = await detect_misconceptions(
        sqlite_db,
        attempt_id=1,
        concept_id=None,
        student_graph=KGGraph(nodes=[]),
        reference_graph=KGGraph(
            nodes=[_eq_node("ref_continuity", "A1*v1 - A2*v2", "Continuity")]
        ),
        problem_text="Apply the continuity equation.",
        student_utterances=("some student utterance",),
        judge_fn=judge_fn,
        embed_fn=embed_fn,
    )

    assert all(f.source != "bank_pattern" for f in result.per_concept)
    assert any(f.source == "judge" for f in result.per_concept)


@pytest.mark.asyncio
async def test_judge_concepts_raising_is_soft_failed_by_the_wrapper(sqlite_db, monkeypatch):
    """Exercises the orchestrator's OWN except around ``judge_concepts`` (as
    opposed to judge.py's internal soft-fail around a raising ``JudgeFn``,
    covered by ``test_judge_raising_...`` above) -- e.g. a defect in prompt
    construction that raises before ``judge_fn`` is ever called."""
    import apollo.overseer.misconception_detector.detector as detector_module

    def _boom(*, problem_text, concepts, judge_fn):
        raise RuntimeError("simulated judge_concepts crash")

    monkeypatch.setattr(detector_module, "judge_concepts", _boom)

    result = await detect_misconceptions(
        sqlite_db,
        attempt_id=1,
        concept_id=None,
        student_graph=KGGraph(nodes=[_eq_node("stu_eq1", "A2*v2 - A1*v1")]),
        reference_graph=KGGraph(
            nodes=[_eq_node("ref_continuity", "A1*v1 - A2*v2", "Continuity")]
        ),
        problem_text="Apply the continuity equation.",
        student_utterances=(),
        judge_fn=_StubJudgeFn(),
        embed_fn=_StubEmbedFn(),
    )

    assert all(f.source != "judge" for f in result.per_concept)
