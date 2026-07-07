"""T7 smoke tests: engine orchestration + done_grading/artifact integration.

Prototype gate per the design card (95% patch gate explicitly deferred):

* engine end-to-end on a tiny fixture graph — stub NLI, stub selector, stub
  grayzone fn (zero model loads, zero network);
* pair-budget threading across nodes AND edges + edge pull-up floors landing
  on node credits before aggregation;
* trace shape (json round-trip);
* ``substitute_scores`` touches EXACTLY three ``GradeResult`` fields;
* ``v1_inputs_from_canonical`` (misc.* excluded, explicit/inferred split);
* ``load_student_turns`` role filter + turn ordering (sqlite fixture);
* ``apply_resolver_v2`` flag-ON substitution + trace dump + engine-failure
  fallback to the untouched v1 grade;
* flag-OFF byte-identity: importing ``done_grading`` pulls no heavy modules
  (subprocess check) and ``build_graph_artifact`` output is identical except
  for ``scores.resolver_v2`` when a trace is present.
"""

from __future__ import annotations

import copy
import json
import math
import subprocess
import sys
from dataclasses import fields, replace
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from apollo.grading.artifact_build import build_graph_artifact
from apollo.grading.audited_grade import AuditedGrade
from apollo.grading.composite import load_weights
from apollo.graph_compare.canonical import (
    CanonicalEdge,
    CanonicalGraph,
    CanonicalNode,
    ReferenceGraph,
    ReferencePathView,
)
from apollo.graph_compare.core import COMPARISON_VERSION, GradeResult
from apollo.handlers.done_grading import ShadowGradeResult
from apollo.ontology.edges import EdgeType
from apollo.resolution.nli_adjudicator import NLIResult
from apollo.resolution.result import ResolutionResult
from apollo.resolver_v2 import integration
from apollo.resolver_v2.config import ResolverV2Params
from apollo.resolver_v2.engine import run_resolver_v2
from apollo.resolver_v2.grayzone import GrayzoneVerdict
from apollo.resolver_v2.integration import (
    apply_resolver_v2,
    load_student_turns,
    substitute_scores,
    v1_inputs_from_canonical,
)
from apollo.resolver_v2.types import SelectFn

_PARAMS = ResolverV2Params()

# ---------------------------------------------------------------------------
# Engine fixtures — tiny 2-node / 1-edge reference, 2 student turns
# ---------------------------------------------------------------------------

_TURNS = (
    "The pipe narrows and water speeds up. Mass is conserved in the pipe.",
    "The flow rate is equal at both sections.",
)
# Deterministic window texts (build_windows on _TURNS with default params).
_W0 = "The pipe narrows and water speeds up. Mass is conserved in the pipe."
_W1 = "The flow rate is equal at both sections."

_LABEL_A = "Label A: mass conservation"
_LABEL_B = "Label B: equal flow rate"
_EDGE_HYPOTHESIS = f"{_LABEL_A} uses {_LABEL_B}."


def _reference() -> ReferenceGraph:
    return ReferenceGraph(
        nodes=(
            CanonicalNode(
                canonical_key="concept.a",
                node_type="definition",
                source_node_ids=("r1",),
                evidence_spans=(),
            ),
            CanonicalNode(
                canonical_key="concept.b",
                node_type="definition",
                source_node_ids=("r2",),
                evidence_spans=(),
            ),
        ),
        edges=(
            CanonicalEdge(
                edge_type=EdgeType.USES,
                from_key="concept.a",
                to_key="concept.b",
                provenance="explicit",
            ),
        ),
        paths=(ReferencePathView(canonical_keys=("concept.a", "concept.b")),),
    )


def _payload() -> dict:
    # concept_id/id NOT in the committed views cache -> label-only views
    # (the documented degrade), so the engine is fully deterministic here.
    return {
        "concept_id": "t7_engine_test",
        "id": "t7_problem",
        "reference_solution": [
            {"id": "s1", "entity_key": "concept.a", "content": {"label": _LABEL_A}},
            {"id": "s2", "entity_key": "concept.b", "content": {"label": _LABEL_B}},
        ],
    }


def _nli(entailment: float, contradiction: float = 0.0) -> NLIResult:
    neutral = max(0.0, 1.0 - entailment - contradiction)
    label = max(
        ("entailment", entailment),
        ("neutral", neutral),
        ("contradiction", contradiction),
        key=lambda kv: kv[1],
    )[0]
    return NLIResult(label, entailment, contradiction, neutral, "fake-nli")


class PermissiveFakeNLI:
    """Stub adjudicator: low-signal default, exact-pair overrides, call count."""

    def __init__(self, overrides: dict[tuple[str, str], NLIResult]):
        self._overrides = overrides
        self.calls = 0

    def classify(self, premise: str, hypothesis: str) -> NLIResult:
        self.calls += 1
        return self._overrides.get((premise, hypothesis), _nli(0.05))


def _fixed_select() -> SelectFn:
    """Stub SelectFn: every view sees window 0 (lex 1.0) then window 1 (0.8)."""

    def select(windows, view_text, k):  # noqa: ANN001 - SelectFn shape
        ranked = ((0, 1.0), (1, 0.8))
        return tuple(ranked[: max(0, k)])

    return select


def _engine_overrides() -> dict[tuple[str, str], NLIResult]:
    return {
        (_W0, _LABEL_A): _nli(1.0),  # fused 1.0 -> credit 1.0
        (_W1, _LABEL_B): _nli(0.5),  # fused 0.545 -> gray band (0.3)
        (_W0, _EDGE_HYPOTHESIS): _nli(0.9),  # >= t_edge -> ENTAIL
    }


class RecordingGrayzone:
    """Stub GrayzoneFn: verifies concept.b with a real transcript quote."""

    def __init__(self):
        self.calls: list[tuple[tuple, str]] = []

    def __call__(self, queries, transcript):  # noqa: ANN001 - GrayzoneFn shape
        self.calls.append((queries, transcript))
        return tuple(
            GrayzoneVerdict(
                canonical_key=q.canonical_key,
                taught=q.canonical_key == "concept.b",
                quote="The flow rate is equal at both sections",
                verified=q.canonical_key == "concept.b",
            )
            for q in queries
        )


# ---------------------------------------------------------------------------
# Engine end-to-end
# ---------------------------------------------------------------------------


def test_engine_end_to_end_happy_path_with_grayzone():
    nli = PermissiveFakeNLI(_engine_overrides())
    grayzone = RecordingGrayzone()
    result = run_resolver_v2(
        student_turns=_TURNS,
        reference_graph=_reference(),
        problem_payload=_payload(),
        v1_resolved_keys=frozenset(),
        v1_explicit_triples=frozenset(),
        v1_inferred_triples=frozenset(),
        nli=nli,
        grayzone_fn=grayzone,
        params=_PARAMS,
        select_fn=_fixed_select(),
    )
    by_key = {n.canonical_key: n for n in result.node_scores}
    # concept.a: entailed at 1.0 -> fused 1.0 -> credit 1.0 via NLI.
    assert by_key["concept.a"].credit == pytest.approx(1.0)
    assert by_key["concept.a"].source == "nli"
    # concept.b: gray band (fused 0.545) upgraded by the verified grayzone
    # check to the capped 0.7 — never 1.0.
    assert by_key["concept.b"].credit == pytest.approx(_PARAMS.grayzone_credit)
    assert by_key["concept.b"].source == "grayzone"
    assert result.grayzone_used is True
    assert len(grayzone.calls) == 1  # exactly ONE batched call
    # Edge: ENTAIL tier -> credit sqrt(max(1.0,.6) * max(0.7,.6)) = sqrt(0.7).
    assert len(result.edge_scores) == 1
    assert result.edge_scores[0].relation_evidence == "entail"
    assert result.edge_scores[0].credit == pytest.approx(math.sqrt(0.7))
    # Aggregation: winning path mean (1.0 + 0.7)/2; edge mean sqrt(0.7).
    assert result.node_coverage == pytest.approx(0.85)
    assert result.edge_coverage == pytest.approx(math.sqrt(0.7))
    assert result.winning_path_index == 0
    # Budget audit: 2 pairs per node + 1 edge pair, all uncached.
    assert result.pair_count == 5
    assert nli.calls == 5


def test_engine_budget_threads_across_nodes_and_edges():
    """max_nli_pairs=4 is fully consumed by the node pass -> the edge ENTAIL
    tier never runs (gate sees zero budget) and COOCCUR wins instead."""
    nli = PermissiveFakeNLI(_engine_overrides())
    params = replace(_PARAMS, max_nli_pairs=4)
    result = run_resolver_v2(
        student_turns=_TURNS,
        reference_graph=_reference(),
        problem_payload=_payload(),
        v1_resolved_keys=frozenset(),
        v1_explicit_triples=frozenset(),
        v1_inferred_triples=frozenset(),
        nli=nli,
        grayzone_fn=None,
        params=params,
        select_fn=_fixed_select(),
    )
    assert result.pair_count == 4
    assert nli.calls == 4
    # Best windows: concept.a -> w0, concept.b -> w1 (adjacent) -> COOCCUR.
    assert result.edge_scores[0].relation_evidence == "cooccur"
    # r=0.7 on credits (1.0, 0.3 gray default — grayzone disabled).
    assert result.edge_scores[0].credit == pytest.approx(0.7 * math.sqrt(1.0 * 0.3))
    assert result.grayzone_used is False


def test_engine_edge_pullup_floors_node_credit_before_aggregation():
    """An entailed edge pulls a zero-credit endpoint up to the 0.6 floor and
    node_coverage is computed AFTER the pull-up (the T5->T7 contract)."""
    overrides = {
        (_W0, _LABEL_A): _nli(1.0),
        (_W0, _EDGE_HYPOTHESIS): _nli(0.9),
        # concept.b stays at the low default (fused < t_low -> credit 0.0).
    }
    result = run_resolver_v2(
        student_turns=_TURNS,
        reference_graph=_reference(),
        problem_payload=_payload(),
        v1_resolved_keys=frozenset(),
        v1_explicit_triples=frozenset(),
        v1_inferred_triples=frozenset(),
        nli=PermissiveFakeNLI(overrides),
        grayzone_fn=None,
        params=_PARAMS,
        select_fn=_fixed_select(),
    )
    by_key = {n.canonical_key: n for n in result.node_scores}
    assert by_key["concept.b"].credit == pytest.approx(_PARAMS.edge_pullup_floor)
    assert by_key["concept.b"].source == "edge_pullup"
    assert result.node_coverage == pytest.approx((1.0 + 0.6) / 2)
    # edge_credit = 1.0 * sqrt(max(1.0,.6) * max(0.0,.6)) = sqrt(0.6).
    assert result.edge_scores[0].credit == pytest.approx(math.sqrt(0.6))


def test_engine_trace_shape_json_round_trip():
    result = run_resolver_v2(
        student_turns=_TURNS,
        reference_graph=_reference(),
        problem_payload=_payload(),
        v1_resolved_keys=frozenset({"concept.a"}),
        v1_explicit_triples=frozenset(),
        v1_inferred_triples=frozenset(),
        nli=None,  # lexical-only degrade
        grayzone_fn=None,
        params=_PARAMS,
        select_fn=_fixed_select(),
    )
    trace = json.loads(json.dumps(result.trace()))
    assert set(trace) == {"summary", "nodes", "edges"}
    assert trace["summary"]["node_count"] == 2
    assert trace["summary"]["edge_count"] == 1
    assert trace["summary"]["pair_count"] == 0
    assert trace["summary"]["grayzone_used"] is False
    assert {n["canonical_key"] for n in trace["nodes"]} == {"concept.a", "concept.b"}


# ---------------------------------------------------------------------------
# substitute_scores / v1_inputs_from_canonical
# ---------------------------------------------------------------------------


def _grade() -> GradeResult:
    return GradeResult(
        coverage_score=0.123,
        soundness_score=0.5,
        bisimilarity_score=0.5,
        node_coverage_score=0.123,
        edge_coverage_score=0.456,
        scoping_score=1.0,
        usage_score=1.0,
        procedure_order_score=1.0,
        dependency_score=1.0,
        contradiction_score=1.0,
        comparison_confidence=1.0,
        findings=(),
        comparison_version=COMPARISON_VERSION,
    )


def _lexical_only_result():
    return run_resolver_v2(
        student_turns=_TURNS,
        reference_graph=_reference(),
        problem_payload=_payload(),
        v1_resolved_keys=frozenset({"concept.a"}),
        v1_explicit_triples=frozenset({("USES", "concept.a", "concept.b")}),
        v1_inferred_triples=frozenset(),
        nli=None,
        grayzone_fn=None,
        params=_PARAMS,
        select_fn=_fixed_select(),
    )


def test_substitute_scores_changes_exactly_three_fields():
    grade = _grade()
    v2 = _lexical_only_result()
    substituted = substitute_scores(grade, v2)
    assert substituted.coverage_score == pytest.approx(v2.node_coverage)
    assert substituted.node_coverage_score == pytest.approx(v2.node_coverage)
    assert substituted.edge_coverage_score == pytest.approx(v2.edge_coverage)
    changed = {"coverage_score", "node_coverage_score", "edge_coverage_score"}
    for field in fields(GradeResult):
        if field.name in changed:
            continue
        assert getattr(substituted, field.name) == getattr(grade, field.name), field.name


def _student_canonical() -> CanonicalGraph:
    return CanonicalGraph(
        nodes=(
            CanonicalNode(
                canonical_key="concept.a",
                node_type="definition",
                source_node_ids=("n1",),
                evidence_spans=("mass is conserved",),
            ),
            CanonicalNode(
                canonical_key="misc.wrong",
                node_type="definition",
                source_node_ids=("n2",),
                evidence_spans=("pressure always rises",),
            ),
        ),
        edges=(
            CanonicalEdge(
                edge_type=EdgeType.USES,
                from_key="concept.a",
                to_key="concept.b",
                provenance="explicit",
            ),
            CanonicalEdge(
                edge_type=EdgeType.PRECEDES,
                from_key="concept.a",
                to_key="concept.b",
                provenance="inferred",
            ),
        ),
        unresolved_nodes=(),
        dropped_edge_count=0,
    )


def test_v1_inputs_from_canonical_excludes_misc_and_splits_provenance():
    resolved, explicit, inferred = v1_inputs_from_canonical(_student_canonical())
    assert resolved == frozenset({"concept.a"})  # misc.wrong excluded
    assert explicit == frozenset({("USES", "concept.a", "concept.b")})
    assert inferred == frozenset({("PRECEDES", "concept.a", "concept.b")})


# ---------------------------------------------------------------------------
# DB integration — sqlite fixture (mirrors handler-test idiom)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def db_with_attempt():
    from apollo.conftest import TEST_USER_ID
    from apollo.persistence.models import (
        ApolloSession,
        Message,
        ProblemAttempt,
        SessionPhase,
        SessionStatus,
    )
    from database.models import Base

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    tables = [ApolloSession.__table__, Message.__table__, ProblemAttempt.__table__]
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: Base.metadata.create_all(sync_conn, tables=tables)
        )
    Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with Session() as s:
        sess = ApolloSession(
            user_id=TEST_USER_ID,  # letter-leading UUID (sqlite affinity trap)
            search_space_id=1,  # FK not enforced on sqlite (no PRAGMA)
            status=SessionStatus.active.value,
            phase=SessionPhase.TEACHING.value,
            current_problem_id="t7_problem",
        )
        s.add(sess)
        await s.commit()
        await s.refresh(sess)
        attempt = ProblemAttempt(session_id=sess.id, problem_id="t7_problem", difficulty="intro")
        s.add(attempt)
        await s.commit()
        await s.refresh(attempt)
        # Interleaved roles, INSERTED out of turn order to prove the ORDER BY.
        for role, content, turn in (
            ("student", _TURNS[1], 2),
            ("apollo", "Interesting, tell me more?", 1),
            ("student", _TURNS[0], 0),
        ):
            s.add(
                Message(
                    session_id=sess.id,
                    attempt_id=attempt.id,
                    role=role,
                    content=content,
                    turn_index=turn,
                )
            )
        await s.commit()
        yield s, int(attempt.id)
    await engine.dispose()


@pytest.mark.asyncio
async def test_load_student_turns_filters_role_and_orders_by_turn(db_with_attempt):
    db, attempt_id = db_with_attempt
    turns = await load_student_turns(db, attempt_id)
    assert turns == _TURNS  # student-only, turn_index order, apollo row dropped


@pytest.mark.asyncio
async def test_apply_resolver_v2_flag_on_substitutes_scores_and_dumps_trace(
    db_with_attempt, monkeypatch, tmp_path
):
    db, attempt_id = db_with_attempt
    monkeypatch.setattr(integration, "get_adjudicator", lambda: None)  # lexical-only
    monkeypatch.setenv("APOLLO_RESOLVER_V2_TRACE_DIR", str(tmp_path))
    grade = _grade()
    new_grade, trace = await apply_resolver_v2(
        db,
        attempt_id=attempt_id,
        grade=grade,
        student_canonical=_student_canonical(),
        reference_graph=_reference(),
        problem_payload=_payload(),
    )
    # Substitution semantics: the three scores are the ENGINE's numbers (the
    # real lexical prefilter runs here, so we assert against the trace rather
    # than pinning lexical values), everything else untouched.
    assert trace is not None
    assert new_grade.coverage_score == pytest.approx(trace["summary"]["node_coverage"])
    assert new_grade.node_coverage_score == pytest.approx(trace["summary"]["node_coverage"])
    assert new_grade.edge_coverage_score == pytest.approx(trace["summary"]["edge_coverage"])
    assert new_grade.coverage_score != grade.coverage_score  # really substituted
    # v1 floors flow through: concept.a resolved -> credit 1.0 -> winning-path
    # mean >= 0.5; the explicit USES triple floors the only edge to 1.0.
    assert new_grade.node_coverage_score >= 0.5
    assert new_grade.edge_coverage_score == pytest.approx(1.0)
    assert new_grade.soundness_score == grade.soundness_score  # untouched
    dumped = tmp_path / f"attempt_{attempt_id}.json"
    assert dumped.exists()
    assert json.loads(dumped.read_text(encoding="utf-8"))["summary"][
        "edge_coverage"
    ] == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_apply_resolver_v2_engine_failure_falls_back_to_v1(
    db_with_attempt, monkeypatch, caplog
):
    db, attempt_id = db_with_attempt

    def boom(**_kwargs):
        raise RuntimeError("engine exploded")

    monkeypatch.setattr(integration, "run_resolver_v2", boom)
    monkeypatch.setattr(integration, "get_adjudicator", lambda: None)
    grade = _grade()
    with caplog.at_level("WARNING"):
        new_grade, trace = await apply_resolver_v2(
            db,
            attempt_id=attempt_id,
            grade=grade,
            student_canonical=_student_canonical(),
            reference_graph=_reference(),
            problem_payload=_payload(),
        )
    assert new_grade is grade  # the UNTOUCHED v1 grade
    assert trace is None
    assert "resolver_v2_failed_falling_back_to_v1" in caplog.text


# ---------------------------------------------------------------------------
# Flag-OFF byte-identity
# ---------------------------------------------------------------------------


def test_flag_off_import_pulls_no_heavy_modules():
    """Importing done_grading (fresh process) must not import transformers/
    torch or any resolver_v2 module beyond the tiny config/__init__ pair —
    the T7 lazy-import interlock (verification step 3, made durable)."""
    repo_root = Path(__file__).resolve().parents[3]
    code = (
        "import sys; import apollo.handlers.done_grading; "
        "banned = ['transformers', 'torch', 'apollo.resolver_v2.integration', "
        "'apollo.resolver_v2.engine', 'apollo.resolver_v2.scoring', "
        "'apollo.resolver_v2.nli_provider', 'apollo.resolver_v2.grayzone']; "
        "loaded = [m for m in banned if m in sys.modules]; "
        "assert not loaded, f'heavy modules imported flag-off: {loaded}'"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert proc.returncode == 0, proc.stderr


def _shadow(resolver_v2_trace: dict | None = None) -> ShadowGradeResult:
    grade = _grade()
    audited = AuditedGrade(
        grade=grade,
        findings=(),
        abstention_reasons=(),
        abstained=False,
        suppressed_event_kinds=frozenset(),
        alias_candidates=(),
    )
    resolution = ResolutionResult(resolved=(), tier_counts={}, llm_calls=0)
    return ShadowGradeResult(
        run_id=1,
        grade=grade,
        audited=audited,
        normalization_confidence=0.8,
        reference_graph_hash="refhash-v1:deadbeef",
        opposes_map={},
        turn_order={},
        graph_sim_rubric={},
        calibration=object(),  # type: ignore[arg-type]
        diagnostic=object(),  # type: ignore[arg-type]
        resolution=resolution,
        resolver_v2_trace=resolver_v2_trace,
    )


def test_artifact_nests_trace_summary_only_when_v2_ran():
    weights = load_weights()
    baseline = build_graph_artifact(
        shadow=_shadow(None), weights=weights, clarification_trace=[], latency_ms=None
    )
    assert "resolver_v2" not in baseline["scores"]  # flag-OFF: no key at all

    trace = {"summary": {"node_coverage": 0.85, "pair_count": 5}, "nodes": [], "edges": []}
    with_trace = build_graph_artifact(
        shadow=_shadow(trace), weights=weights, clarification_trace=[], latency_ms=None
    )
    assert with_trace["scores"]["resolver_v2"] == trace["summary"]

    # Byte-identity: removing the ONE nested key reproduces the baseline dict.
    stripped = copy.deepcopy(with_trace)
    del stripped["scores"]["resolver_v2"]
    assert stripped == baseline
