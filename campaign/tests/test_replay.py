"""Unit tests for campaign.replay (Task 8 — F1c resolution+grading replay
benchmark).

Feeds a small fixture-copied slice of the frozen F1c transcript log
(``campaign/tests/fixtures/f1c_sample/attempts.jsonl`` — attempt_ids 15/strong,
7/partial, 1/misconception, plus one error-status row) through the replay
entry points. ``build_rerun_inputs``/``run_graph_simulation`` are patched at
the ``campaign.replay`` import site (same pattern
``apollo/handlers/tests/test_done_grading_unit.py`` uses for the chain they
wrap) so no DB/Neo4j/LLM ever runs here; the real ``ShadowGradeResult`` ->
``build_graph_artifact`` reshaping is exercised for real (only the two DB/
Neo4j-touching loaders are faked), so a batch of these attempts reproduces the
live F1c ``unresolved_rate_above_threshold`` abstention pattern from
deterministic, in-memory data.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from apollo.errors import LearnerUpdateUnreconstructableError
from apollo.grading.audited_grade import AuditedGrade
from apollo.graph_compare.core import COMPARISON_VERSION, GradeResult
from apollo.graph_compare.findings import Finding, FindingKind
from apollo.handlers.done_grading import ShadowGradeResult
from apollo.handlers.done_inputs import RerunInputs
from apollo.ontology import KGGraph
from apollo.resolution.result import ResolutionResult, ResolvedNode
from campaign import replay

pytestmark = pytest.mark.unit

_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "f1c_sample"
_FIXTURE = _FIXTURE_DIR / "attempts.jsonl"


def _grade(
    findings: tuple[Finding, ...], *, node_coverage: float, edge_coverage: float
) -> GradeResult:
    return GradeResult(
        coverage_score=node_coverage,
        soundness_score=1.0,
        bisimilarity_score=node_coverage,
        node_coverage_score=node_coverage,
        edge_coverage_score=edge_coverage,
        scoping_score=1.0,
        usage_score=1.0,
        procedure_order_score=1.0,
        dependency_score=1.0,
        contradiction_score=1.0,
        comparison_confidence=1.0,
        findings=findings,
        comparison_version=COMPARISON_VERSION,
    )


def _shadow_for(
    *,
    resolved: tuple[ResolvedNode, ...],
    findings: tuple[Finding, ...],
    abstained: bool,
    abstention_reasons: tuple[str, ...],
    node_coverage: float = 0.3,
    edge_coverage: float = 0.0,
) -> ShadowGradeResult:
    """Build a REAL ShadowGradeResult (same construction ``test_adapters.py``
    uses) rather than a bare MagicMock, so ``build_graph_artifact`` (called
    for real inside ``replay_attempt``) reshapes genuine data."""
    resolution = ResolutionResult(
        resolved=resolved,
        tier_counts={},
        llm_calls=0,
    )
    grade = _grade(findings, node_coverage=node_coverage, edge_coverage=edge_coverage)
    audited = AuditedGrade(
        grade=grade,
        findings=findings,
        abstention_reasons=abstention_reasons,
        abstained=abstained,
        suppressed_event_kinds=frozenset(),
        alias_candidates=(),
    )
    return ShadowGradeResult(
        run_id=1,
        grade=grade,
        audited=audited,
        normalization_confidence=1.0,
        reference_graph_hash="refhash-v1:deadbeef",
        opposes_map={},
        turn_order={},
        graph_sim_rubric={},
        calibration=object(),  # type: ignore[arg-type]
        diagnostic=object(),  # type: ignore[arg-type]
        resolution=resolution,
    )


def _rerun_inputs() -> RerunInputs:
    """A minimal REAL ``RerunInputs`` (the ``build_rerun_inputs`` return type)
    so patched calls in ``replay_attempt`` see the real attribute shape rather
    than a bare sentinel."""
    return RerunInputs(
        problem_payload={"reference_solution": [], "declared_paths": [], "symbolic_mappings": {}},
        old_rubric={"overall": {"score": 70, "letter": "B-"}},
        student_graph=KGGraph(),
        parser_confidence=1.0,
        graded_at_iso="2026-07-02T00:00:00+00:00",
    )


# ---------------------------------------------------------------------------
# load_records
# ---------------------------------------------------------------------------


def test_load_records_skips_error_status_and_null_attempt_id():
    records = replay.load_records(_FIXTURE)
    assert [r["attempt_id"] for r in records] == [15, 7, 1]
    assert all(r["status"] == "ok" for r in records)


def test_load_records_filters_by_persona():
    records = replay.load_records(_FIXTURE, personas=["strong", "partial"])
    assert {r["persona"] for r in records} == {"strong", "partial"}
    assert [r["attempt_id"] for r in records] == [15, 7]


def test_load_records_rejects_unknown_persona_class():
    """A --personas value that matches NOTHING recorded in the corpus must be
    a loud error, not a silent zero-record filter (the exact mistake that
    produced a controls-free 'baseline': ``control`` is a ROLE served by the
    ``misconception``/``vague_then_clarifies`` classes, not a persona key)."""
    with pytest.raises(ValueError, match=r"control") as excinfo:
        replay.load_records(_FIXTURE, personas=["strong", "control"])
    # The error must teach the fix: name every class the corpus actually has.
    for known in ("strong", "partial", "misconception", "vague_then_clarifies"):
        assert known in str(excinfo.value)


def test_load_records_accepts_persona_class_present_only_on_error_rows():
    """A persona class recorded ONLY on non-gradeable rows is still a real
    class of this corpus — filtering to it yields zero records (correct),
    but must not raise as 'unknown'."""
    records = replay.load_records(_FIXTURE, personas=["vague_then_clarifies"])
    assert records == []


def test_load_records_never_mutates_source_file():
    before = _FIXTURE.read_text(encoding="utf-8")
    replay.load_records(_FIXTURE)
    after = _FIXTURE.read_text(encoding="utf-8")
    assert before == after


def test_load_records_skips_blank_lines(tmp_path: Path):
    attempts_path = tmp_path / "attempts.jsonl"
    attempts_path.write_text(
        '{"attempt_id": 1, "persona": "strong", "status": "ok", "expected": {}}\n'
        "\n"
        '{"attempt_id": 2, "persona": "strong", "status": "ok", "expected": {}}\n',
        encoding="utf-8",
    )
    records = replay.load_records(attempts_path)
    assert [r["attempt_id"] for r in records] == [1, 2]


# ---------------------------------------------------------------------------
# _load_attempt_and_session
# ---------------------------------------------------------------------------


async def test_load_attempt_and_session_raises_when_attempt_missing():
    db = AsyncMock()
    db.get.return_value = None
    with pytest.raises(LearnerUpdateUnreconstructableError):
        await replay._load_attempt_and_session(db, attempt_id=99)


async def test_load_attempt_and_session_raises_when_session_missing():
    db = AsyncMock()
    attempt = type("Attempt", (), {"session_id": 5})()
    db.get.side_effect = [attempt, None]
    with pytest.raises(LearnerUpdateUnreconstructableError):
        await replay._load_attempt_and_session(db, attempt_id=99)


async def test_load_attempt_and_session_returns_pair_on_success():
    db = AsyncMock()
    attempt = type("Attempt", (), {"session_id": 5})()
    sess = type("Session", (), {})()
    db.get.side_effect = [attempt, sess]
    result_attempt, result_sess = await replay._load_attempt_and_session(db, attempt_id=99)
    assert result_attempt is attempt
    assert result_sess is sess


# ---------------------------------------------------------------------------
# _mean
# ---------------------------------------------------------------------------


def test_mean_of_empty_is_none():
    assert replay._mean([]) is None


def test_mean_of_values():
    assert replay._mean([1.0, 2.0, 3.0]) == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# replay_attempt — reproduces the F1c live abstention pattern
# ---------------------------------------------------------------------------


async def test_replay_attempt_propagates_abstention_reason_from_mocked_sim():
    """``replay_attempt`` must propagate ``abstention_reasons`` verbatim from
    the (mocked) ``run_graph_simulation`` result onto ``ReplayOutcome`` — this
    pins reason PROPAGATION through the reshaping in ``replay_attempt``/
    ``build_graph_artifact``, not that the real resolver/grader reproduces
    this reason live (the grading chain itself is mocked here; the live
    F1c 31/31 ``unresolved_rate_above_threshold`` pattern is instead pinned
    against the frozen baseline JSON by the ``test_baseline_pin`` tests
    below)."""
    findings = (
        Finding(kind=FindingKind.MISSING_NODE, canonical_key="eq.bernoulli", score=0.0),
        Finding(kind=FindingKind.UNRESOLVED, student_node_ids=("n_x",), evidence_spans=("?",)),
        Finding(kind=FindingKind.UNRESOLVED, student_node_ids=("n_y",), evidence_spans=("?",)),
    )
    resolved = (
        ResolvedNode(
            node_id="n_a",
            resolution="resolved",
            resolved_key="eq.continuity",
            resolved_canon_key=1,
            method="alias",
            confidence=0.92,
        ),
        ResolvedNode(
            node_id="n_x",
            resolution="unresolved",
            resolved_key=None,
            resolved_canon_key=None,
            method="unresolved",
            confidence=0.0,
        ),
        ResolvedNode(
            node_id="n_y",
            resolution="unresolved",
            resolved_key=None,
            resolved_canon_key=None,
            method="unresolved",
            confidence=0.0,
        ),
    )
    shadow = _shadow_for(
        resolved=resolved,
        findings=findings,
        abstained=True,
        abstention_reasons=("unresolved_rate_above_threshold",),
    )

    records = replay.load_records(_FIXTURE, personas=["strong"])
    record = records[0]

    with (
        patch.object(replay, "build_rerun_inputs", new=AsyncMock(return_value=_rerun_inputs())),
        patch.object(
            replay, "_load_attempt_and_session", new=AsyncMock(return_value=(object(), object()))
        ),
        patch.object(replay, "run_graph_simulation", new=AsyncMock(return_value=shadow)),
    ):
        outcome = await replay.replay_attempt(db=object(), neo=object(), record=record)

    assert isinstance(outcome, replay.ReplayOutcome)
    assert outcome.attempt_id == 15
    assert outcome.abstained is True
    assert outcome.abstention_reasons == ("unresolved_rate_above_threshold",)
    assert outcome.unresolved_rate == pytest.approx(2 / 3)


async def test_replay_attempt_control_persona_credit_leak_flagged():
    """A control (misconception) persona attempt that ends up crediting a key
    NOT in its expected-credited set must be flagged — the resolver must never
    grant it undeserved credit."""
    findings = (
        Finding(
            kind=FindingKind.COVERED_NODE,
            canonical_key="cond.incompressibility",
            student_node_ids=("n_a",),
            evidence_spans=("evidence",),
            confidence=0.9,
        ),
    )
    resolved = (
        ResolvedNode(
            node_id="n_a",
            resolution="resolved",
            resolved_key="cond.incompressibility",
            resolved_canon_key=1,
            method="alias",
            confidence=0.92,
        ),
    )
    shadow = _shadow_for(
        resolved=resolved,
        findings=findings,
        abstained=False,
        abstention_reasons=(),
        node_coverage=1.0,
    )

    records = replay.load_records(_FIXTURE, personas=["misconception"])
    record = records[0]
    assert "cond.incompressibility" not in record["expected"]["credited"]

    with (
        patch.object(replay, "build_rerun_inputs", new=AsyncMock(return_value=_rerun_inputs())),
        patch.object(
            replay, "_load_attempt_and_session", new=AsyncMock(return_value=(object(), object()))
        ),
        patch.object(replay, "run_graph_simulation", new=AsyncMock(return_value=shadow)),
    ):
        outcome = await replay.replay_attempt(db=object(), neo=object(), record=record)

    assert outcome.is_control is True
    assert outcome.control_credit_leak is True


async def test_replay_attempt_control_persona_credit_subset_is_not_a_leak():
    """A control (misconception) persona attempt whose credited keys are a
    SUBSET of its expected-credited set must NOT be flagged — pins the
    ``is_control and bool(set(actual_credited) - set(expected_credited))``
    conjunction in ``replay_attempt`` (a leak is only real credit the
    expected ledger never granted, not merely partial credit)."""
    findings = (
        Finding(
            kind=FindingKind.COVERED_NODE,
            canonical_key="eq.continuity",
            student_node_ids=("n_a",),
            evidence_spans=("evidence",),
            confidence=0.9,
        ),
    )
    resolved = (
        ResolvedNode(
            node_id="n_a",
            resolution="resolved",
            resolved_key="eq.continuity",
            resolved_canon_key=1,
            method="alias",
            confidence=0.92,
        ),
    )
    shadow = _shadow_for(
        resolved=resolved,
        findings=findings,
        abstained=False,
        abstention_reasons=(),
        node_coverage=1.0,
    )

    records = replay.load_records(_FIXTURE, personas=["misconception"])
    record = records[0]
    # Sanity: "eq.continuity" IS in this attempt's expected-credited set, so
    # crediting only it is a genuine subset, not accidentally the full leak
    # scenario covered by the sibling test above.
    assert set(("eq.continuity",)) <= set(record["expected"]["credited"])

    with (
        patch.object(replay, "build_rerun_inputs", new=AsyncMock(return_value=_rerun_inputs())),
        patch.object(
            replay, "_load_attempt_and_session", new=AsyncMock(return_value=(object(), object()))
        ),
        patch.object(replay, "run_graph_simulation", new=AsyncMock(return_value=shadow)),
    ):
        outcome = await replay.replay_attempt(db=object(), neo=object(), record=record)

    assert outcome.is_control is True
    assert outcome.control_credit_leak is False


async def test_replay_attempt_non_control_credit_is_not_a_leak():
    findings = (
        Finding(
            kind=FindingKind.COVERED_NODE,
            canonical_key="eq.continuity",
            student_node_ids=("n_a",),
            evidence_spans=("evidence",),
            confidence=0.9,
        ),
    )
    resolved = (
        ResolvedNode(
            node_id="n_a",
            resolution="resolved",
            resolved_key="eq.continuity",
            resolved_canon_key=1,
            method="alias",
            confidence=0.92,
        ),
    )
    shadow = _shadow_for(
        resolved=resolved,
        findings=findings,
        abstained=False,
        abstention_reasons=(),
        node_coverage=1.0,
    )

    records = replay.load_records(_FIXTURE, personas=["strong"])
    record = records[0]

    with (
        patch.object(replay, "build_rerun_inputs", new=AsyncMock(return_value=_rerun_inputs())),
        patch.object(
            replay, "_load_attempt_and_session", new=AsyncMock(return_value=(object(), object()))
        ),
        patch.object(replay, "run_graph_simulation", new=AsyncMock(return_value=shadow)),
    ):
        outcome = await replay.replay_attempt(db=object(), neo=object(), record=record)

    assert outcome.is_control is False
    assert outcome.control_credit_leak is False


async def test_replay_attempt_catches_named_error_and_returns_replay_error():
    records = replay.load_records(_FIXTURE, personas=["partial"])
    record = records[0]

    with patch.object(
        replay,
        "_load_attempt_and_session",
        new=AsyncMock(
            side_effect=LearnerUpdateUnreconstructableError(
                attempt_id=7, reason="attempt_not_found_in_local_stack"
            )
        ),
    ):
        outcome = await replay.replay_attempt(db=object(), neo=object(), record=record)

    assert isinstance(outcome, replay.ReplayError)
    assert outcome.attempt_id == 7
    assert outcome.reason == "LearnerUpdateUnreconstructableError"


# ---------------------------------------------------------------------------
# summarize — the four required metric keys
# ---------------------------------------------------------------------------


def _outcome(**overrides: object) -> replay.ReplayOutcome:
    base: dict[str, Any] = dict(
        attempt_id=1,
        persona="strong",
        is_control=False,
        unresolved_rate=0.5,
        abstained=True,
        abstention_reasons=("unresolved_rate_above_threshold",),
        graph_composite=0.2,
        node_coverage=0.5,
        actual_credited=("eq.continuity",),
        actual_unresolved=(),
        actual_misconceptions=(),
        expected_credited=("eq.continuity",),
        expected_unresolved=(),
        expected_misconceptions=(),
        control_credit_leak=False,
    )
    base.update(overrides)
    return replay.ReplayOutcome(**base)


def test_summarize_has_four_required_metric_keys():
    metrics = replay.summarize([_outcome()])
    payload = metrics.as_dict()
    assert set(payload) >= {
        "unresolved_rate",
        "abstention_reasons",
        "graph_composite",
        "band_vs_expected",
    }


def test_summarize_groups_unresolved_rate_and_composite_by_persona():
    outcomes = [
        _outcome(attempt_id=1, persona="strong", unresolved_rate=0.2, graph_composite=0.4),
        _outcome(attempt_id=2, persona="strong", unresolved_rate=0.4, graph_composite=0.2),
        _outcome(attempt_id=3, persona="partial", unresolved_rate=0.6, graph_composite=0.1),
    ]
    metrics = replay.summarize(outcomes)

    assert metrics.unresolved_rate["strong"]["n"] == 2
    assert metrics.unresolved_rate["strong"]["mean"] == pytest.approx(0.3)
    assert metrics.unresolved_rate["partial"]["n"] == 1

    assert metrics.graph_composite["strong"]["mean"] == pytest.approx(0.3)


def test_summarize_abstention_histogram_counts_reasons_and_replay_errors():
    outcomes = [
        _outcome(attempt_id=1, abstention_reasons=("unresolved_rate_above_threshold",)),
        _outcome(attempt_id=2, abstention_reasons=("unresolved_rate_above_threshold",)),
        _outcome(attempt_id=3, abstained=False, abstention_reasons=()),
    ]
    errors = [
        replay.ReplayError(attempt_id=4, persona="strong", reason="ResolutionUnavailableError")
    ]
    metrics = replay.summarize([*outcomes, *errors])

    assert metrics.abstention_reasons["unresolved_rate_above_threshold"] == 2
    assert metrics.abstention_reasons["<none>"] == 1
    assert metrics.abstention_reasons["replay_error:ResolutionUnavailableError"] == 1


def test_summarize_band_vs_expected_carries_ledger_diff():
    outcomes = [
        _outcome(
            attempt_id=1,
            actual_credited=("eq.a",),
            expected_credited=("eq.a", "eq.b"),
        )
    ]
    metrics = replay.summarize(outcomes)
    row = metrics.band_vs_expected[0]
    assert row["attempt_id"] == 1
    assert row["actual_credited"] == ["eq.a"]
    assert row["expected_credited"] == ["eq.a", "eq.b"]


def test_summarize_excludes_errors_from_persona_grouping():
    errors = [replay.ReplayError(attempt_id=9, persona="strong", reason="StudentGraphInvalidError")]
    metrics = replay.summarize(errors)
    assert metrics.unresolved_rate == {}
    assert metrics.graph_composite == {}
    assert metrics.errors == [
        {"attempt_id": 9, "persona": "strong", "reason": "StudentGraphInvalidError"}
    ]


# ---------------------------------------------------------------------------
# run_replay — end-to-end over the fixture (db/neo faked)
# ---------------------------------------------------------------------------


async def test_run_replay_end_to_end_over_fixture():
    shadow = _shadow_for(
        resolved=(
            ResolvedNode(
                node_id="n_a",
                resolution="unresolved",
                resolved_key=None,
                resolved_canon_key=None,
                method="unresolved",
                confidence=0.0,
            ),
        ),
        findings=(
            Finding(kind=FindingKind.UNRESOLVED, student_node_ids=("n_a",), evidence_spans=("?",)),
        ),
        abstained=True,
        abstention_reasons=("unresolved_rate_above_threshold",),
    )

    with (
        patch.object(
            replay, "_load_attempt_and_session", new=AsyncMock(return_value=(object(), object()))
        ),
        patch.object(replay, "build_rerun_inputs", new=AsyncMock(return_value=_rerun_inputs())),
        patch.object(replay, "run_graph_simulation", new=AsyncMock(return_value=shadow)),
    ):
        metrics = await replay.run_replay(
            run_dir=_FIXTURE_DIR,
            personas=["strong", "partial", "misconception"],
            db=object(),
            neo=object(),
        )

    payload = metrics.as_dict()
    assert set(payload) >= {
        "unresolved_rate",
        "abstention_reasons",
        "graph_composite",
        "band_vs_expected",
    }
    assert len(payload["band_vs_expected"]) == 3
    assert payload["abstention_reasons"]["unresolved_rate_above_threshold"] == 3


# ---------------------------------------------------------------------------
# _amain — CLI wiring (DB/Neo4j construction + run_replay call faked)
# ---------------------------------------------------------------------------


async def _fake_get_db_session():
    yield object()


async def test_amain_writes_metrics_to_out_file(tmp_path: Path):
    out_path = tmp_path / "metrics.json"
    canned = replay.summarize([_outcome(persona="strong")])
    fake_neo = AsyncMock()

    with (
        patch("apollo.persistence.neo4j_client.Neo4jClient.from_env", return_value=fake_neo),
        patch("database.session.get_db_session", new=_fake_get_db_session),
        patch.object(replay, "run_replay", new=AsyncMock(return_value=canned)),
    ):
        result = await replay._amain(
            ["--run-dir", str(_FIXTURE_DIR), "--personas", "strong,partial", "--out", str(out_path)]
        )

    assert result is canned
    fake_neo.close.assert_awaited()
    written = json.loads(out_path.read_text(encoding="utf-8"))
    assert set(written) >= {
        "unresolved_rate",
        "abstention_reasons",
        "graph_composite",
        "band_vs_expected",
    }


async def test_amain_prints_metrics_when_no_out_given(capsys: pytest.CaptureFixture[str]):
    canned = replay.summarize([_outcome(persona="strong")])
    fake_neo = AsyncMock()

    with (
        patch("apollo.persistence.neo4j_client.Neo4jClient.from_env", return_value=fake_neo),
        patch("database.session.get_db_session", new=_fake_get_db_session),
        patch.object(replay, "run_replay", new=AsyncMock(return_value=canned)),
    ):
        await replay._amain(["--run-dir", str(_FIXTURE_DIR)])

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert "unresolved_rate" in payload


# ---------------------------------------------------------------------------
# Baseline pin — campaign/tests/fixtures/replay-baseline-2c2dc5f.json
# (frozen copy of the f1c run's replay baseline; the ephemeral campaign/out/
# run tree is no longer tracked)
#
# Pure JSON reads, no grading stack: recomputes every derived value in the
# COMMITTED frozen baseline from its own raw rows and asserts it matches what
# is on disk, so a later hand-edit of the baseline JSON (mean, a leak flag,
# an abstention count, a class count) turns CI red instead of silently
# drifting the fix-iteration flywheel's reference point.
# ---------------------------------------------------------------------------

_BASELINE_PATH = Path(__file__).parent / "fixtures" / "replay-baseline-2c2dc5f.json"


def _load_baseline() -> dict[str, Any]:
    return json.loads(_BASELINE_PATH.read_text(encoding="utf-8"))


def test_baseline_pin_per_class_means_match_recomputed_mean():
    baseline = _load_baseline()
    for metric_key in ("unresolved_rate", "graph_composite"):
        for persona, stats in baseline[metric_key].items():
            recomputed = sum(stats["values"]) / len(stats["values"])
            assert recomputed == pytest.approx(stats["mean"], abs=1e-9), (
                f"{metric_key}.{persona} mean does not match the arithmetic "
                "mean of its own recorded per-attempt values"
            )
            assert stats["n"] == len(stats["values"])


def test_baseline_pin_control_credit_leak_flags_match_recomputation():
    """Recompute ``control_credit_leak`` for every ``band_vs_expected`` row
    per the code's own definition in ``replay.replay_attempt``
    (``is_control and bool(set(actual_credited) - set(expected_credited))``)
    and assert it matches the frozen flag on disk."""
    baseline = _load_baseline()
    for row in baseline["band_vs_expected"]:
        recomputed_leak = row["is_control"] and bool(
            set(row["actual_credited"]) - set(row["expected_credited"])
        )
        assert recomputed_leak == row["control_credit_leak"], (
            f"attempt_id={row['attempt_id']} control_credit_leak mismatch"
        )


def test_baseline_pin_abstention_histogram():
    """Assert exactly what the frozen baseline records: 31/31 gradeable
    attempts abstain on ``unresolved_rate_above_threshold``, with one of
    those 31 additionally co-occurring with
    ``min_parser_confidence_below_threshold`` (reason occurrences, not
    attempt counts — an attempt with 2 reasons increments both buckets)."""
    baseline = _load_baseline()
    assert baseline["abstention_reasons"] == {
        "unresolved_rate_above_threshold": 31,
        "min_parser_confidence_below_threshold": 1,
    }


def test_baseline_pin_class_counts_and_controls_and_leak_count():
    baseline = _load_baseline()
    rows = baseline["band_vs_expected"]
    assert len(rows) == 31

    persona_counts: dict[str, int] = {}
    for row in rows:
        persona_counts[row["persona"]] = persona_counts.get(row["persona"], 0) + 1
    assert persona_counts == {
        "strong": 10,
        "partial": 8,
        "misconception": 7,
        "vague_then_clarifies": 6,
    }

    controls = [row for row in rows if row["is_control"]]
    assert len(controls) == 13

    leaks = [row for row in rows if row["control_credit_leak"]]
    assert len(leaks) == 9

    for metric_key in ("unresolved_rate", "graph_composite"):
        assert baseline[metric_key]["strong"]["n"] == 10
        assert baseline[metric_key]["partial"]["n"] == 8
        assert baseline[metric_key]["misconception"]["n"] == 7
        assert baseline[metric_key]["vague_then_clarifies"]["n"] == 6
