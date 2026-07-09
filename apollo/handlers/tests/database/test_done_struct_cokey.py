"""Task 10 (F-struct 10/11) — wiring ``build_opposes_index`` into ``done.py``.

Frozen contract: ``.superpowers/sdd/task-10-brief.md`` + the F-struct spec
(``docs/_archive/specs/2026-07-09-apollo-misconception-struct-cokey-design.md``).

``APOLLO_MISC_STRUCT_COKEY`` (``config.py::struct_cokey_enabled()``) is a
SEPARATE sub-flag from ``APOLLO_MISCONCEPTION_DETECTOR`` — it only matters
when the detector itself is ON. These tests pin the wiring contract:

  * **Sub-flag OFF** (default, and even with the detector ON): ``done.py``
    never imports ``build_opposes_index``, passes ``opposes_index={}`` to
    both ``gate_findings`` and ``trace_attempt``, and a judge finding that
    localizes an error to a node WITHOUT naming a bank code (no corroborating
    ``bank_pattern`` hit, no lone-solo dock either — nothing keyed) docks
    NOTHING — byte-identical to pre-F-struct behavior.
  * **Sub-flag ON**: the SAME judge finding, now resolved against a bank
    entry whose ``opposes`` matches the reference node's ``entity_key``,
    docks via the structural co-key path and the misconception surfaces in
    the artifact's ``misconceptions[]`` (bare ``misc.<code>`` canonical key).
  * The bank load is soft-failing (``_load_bank_entries`` mirrors
    ``detector._load_bank``): a DB error degrades to an empty bank rather
    than raising, so it stays within the T13 outer soft-fail envelope too.

Reuses the exact mocked OLD-path harness from
``test_done_shadow_flag._old_path_patches`` (MagicMock DB/Neo4j, every
OLD-path collaborator patched) plus the T13 integer-scored rubric override
from ``test_done_misconception`` — no real database, no live LLM, no
network. The gate/merge/apply chain downstream of the (patched)
``detect_misconceptions`` runs FOR REAL so this exercises the actual
structural-dock arithmetic, not a mocked outcome.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from apollo.handlers.done import _load_bank_entries, handle_done
from apollo.handlers.tests.database.test_done_misconception import (
    _OLD_RUBRIC,
    _patches_with_rubric,
)
from apollo.handlers.tests.test_done_shadow_flag import _old_path_patches
from apollo.ontology import KGGraph, build_node
from apollo.overseer.misconception_bank import MisconceptionEntry
from apollo.overseer.misconception_detector.types import ConceptFinding, DetectionResult

pytestmark = pytest.mark.unit

_DETECTOR_FLAG = "APOLLO_MISCONCEPTION_DETECTOR"
_STRUCT_FLAG = "APOLLO_MISC_STRUCT_COKEY"
_NODE_ID = "node-real-basis"
_ENTITY_KEY = "def.real_basis"
_BANK_CODE = "nominal_for_real"


def _reference_graph_with_entity_key() -> KGGraph:
    node = build_node(
        node_type="definition",
        node_id=_NODE_ID,
        attempt_id=1,
        source="reference",
        content={"concept": "real basis", "meaning": "inflation-adjusted"},
        entity_key=_ENTITY_KEY,
    )
    return KGGraph(nodes=[node])


def _bank_entry_opposing_real_basis(*, opposes: str | None = _ENTITY_KEY) -> MisconceptionEntry:
    return MisconceptionEntry(
        id=1,
        concept_id=3,
        code=_BANK_CODE,
        description="confuses nominal and real values",
        confusion_pair=None,
        trigger_phrases=(),
        probe_question="",
        rt_steps=(),
        opposes=opposes,
    )


def _unkeyed_judge_finding(*, confidence: float = 0.95, verdict: str = "wrong") -> ConceptFinding:
    """A judge finding that LOCALIZES the error to the reference node (high
    confidence, clears routed tau) but names NO validated bank_code — the
    exact shape the F-struct structural co-key branch requires
    (``best_judge.bank_code is None``)."""
    return ConceptFinding(
        concept_key=_NODE_ID,
        verdict=verdict,  # type: ignore[arg-type]
        confidence=confidence,
        severity=0.0,
        evidence_span="the real basis is whatever number is on the receipt",
        signature=f"unkeyed:{_NODE_ID}",
        source="judge",
        corroborated=False,
        bank_code=None,
    )


@pytest.fixture(autouse=True)
def _clear_flags(monkeypatch):
    monkeypatch.delenv(_DETECTOR_FLAG, raising=False)
    monkeypatch.delenv(_STRUCT_FLAG, raising=False)
    monkeypatch.delenv("APOLLO_GRAPH_SIM_SHADOW_ENABLED", raising=False)
    monkeypatch.delenv("APOLLO_GRADING_ARTIFACT_ENABLED", raising=False)
    monkeypatch.delenv("APOLLO_MISC_TRACE", raising=False)
    yield


async def _run(
    monkeypatch,
    *,
    struct_flag: str | None,
    bank_entries: tuple[MisconceptionEntry, ...] = (),
    load_bank_side_effect: Exception | None = None,
    write_mock: AsyncMock | None = None,
):
    """Drive handle_done with the detector ON, a reference graph carrying one
    entity-keyed node, and a single unkeyed-but-localized judge finding at
    that node. ``struct_flag`` sets/clears APOLLO_MISC_STRUCT_COKEY.
    ``bank_entries`` is what ``_load_bank_entries`` returns (or raises, via
    ``load_bank_side_effect``, to exercise the soft-fail branch).

    Returns (out, gate_spy) — ``gate_spy`` wraps the REAL gate_findings so we
    can assert on the opposes_index it was actually called with, while still
    running the real gate/merge arithmetic.
    """
    monkeypatch.setenv(_DETECTOR_FLAG, "true")
    if struct_flag is not None:
        monkeypatch.setenv(_STRUCT_FLAG, struct_flag)

    db, _sess, _attempt, patches = _old_path_patches()
    detection = DetectionResult(per_concept=(_unkeyed_judge_finding(),))

    from apollo.overseer.misconception_detector.gate import gate_findings as real_gate

    gate_spy = MagicMock(side_effect=real_gate)

    load_bank_kwargs: dict = {}
    if load_bank_side_effect is not None:
        load_bank_kwargs["side_effect"] = load_bank_side_effect
    else:
        load_bank_kwargs["return_value"] = bank_entries

    patches = _patches_with_rubric(patches, _OLD_RUBRIC)
    patches += [
        patch(
            "apollo.handlers.done.detect_misconceptions",
            new=AsyncMock(return_value=detection),
        ),
        patch("apollo.handlers.done.make_openai_judge", new=MagicMock()),
        patch("apollo.handlers.done._default_embed_fn", new=MagicMock()),
        patch(
            "apollo.handlers.done._student_utterances",
            new=AsyncMock(return_value=("the real basis is whatever is on the receipt",)),
        ),
        patch("apollo.handlers.done.gate_findings", new=gate_spy),
        patch(
            "apollo.handlers.done._load_bank_entries",
            new=AsyncMock(**load_bank_kwargs),
        ),
    ]
    if write_mock is not None:
        patches.append(patch("apollo.handlers.done.write_artifacts", new=write_mock))

    # Override the OLD-path problem stub's reference graph so it carries the
    # entity-keyed node the struct co-key branch needs.
    async def _find_problem(_db, _cid, _code):
        problem = MagicMock()
        problem.id = "p_code"
        problem.problem_text = "text"
        problem.reference_solution = []
        problem.to_kg_graph.return_value = _reference_graph_with_entity_key()
        return problem

    patches.append(
        patch("apollo.handlers.done._find_problem", new=AsyncMock(side_effect=_find_problem))
    )

    for p in patches:
        p.start()
    try:
        out = await handle_done(db=db, neo=MagicMock(), session_id=11)
    finally:
        for p in reversed(patches):
            p.stop()
    return out, gate_spy


# ── sub-flag OFF: byte-identical (empty opposes_index, nothing docks) ───────


async def test_struct_flag_off_opposes_index_empty_and_nothing_docks(monkeypatch):
    """Sub-flag OFF (default) — even with the detector ON and a bank entry
    that WOULD oppose the node, ``gate_findings`` is called with an empty
    ``opposes_index`` and the lone unkeyed judge finding never docks (it
    falls through to row7/8: clarify or drop, never `misconception`)."""
    out, gate_spy = await _run(
        monkeypatch,
        struct_flag=None,
        bank_entries=(_bank_entry_opposing_real_basis(),),
    )
    gate_spy.assert_called_once()
    assert gate_spy.call_args.kwargs["opposes_index"] == {}
    # Nothing docked -> rubric byte-identical to the OLD-path score.
    assert out["rubric"]["overall"]["score"] == 90
    assert out["rubric"]["overall"]["letter"] == "A"


async def test_struct_flag_explicit_false_is_also_off(monkeypatch):
    out, gate_spy = await _run(
        monkeypatch,
        struct_flag="false",
        bank_entries=(_bank_entry_opposing_real_basis(),),
    )
    assert gate_spy.call_args.kwargs["opposes_index"] == {}
    assert out["rubric"]["overall"]["score"] == 90


async def test_struct_flag_off_load_bank_entries_never_called(monkeypatch):
    """Sub-flag OFF -> done.py never even loads the bank for the struct-cokey
    path (no reason to pay the query if the index will be discarded)."""
    db, _sess, _attempt, patches = _old_path_patches()
    monkeypatch.setenv(_DETECTOR_FLAG, "true")
    monkeypatch.delenv(_STRUCT_FLAG, raising=False)

    detection = DetectionResult(per_concept=(_unkeyed_judge_finding(),))
    load_bank_mock = AsyncMock(
        side_effect=AssertionError("_load_bank_entries must not be called when sub-flag is OFF")
    )

    patches = _patches_with_rubric(patches, _OLD_RUBRIC)
    patches += [
        patch(
            "apollo.handlers.done.detect_misconceptions",
            new=AsyncMock(return_value=detection),
        ),
        patch("apollo.handlers.done.make_openai_judge", new=MagicMock()),
        patch("apollo.handlers.done._default_embed_fn", new=MagicMock()),
        patch(
            "apollo.handlers.done._student_utterances",
            new=AsyncMock(return_value=("x",)),
        ),
        patch("apollo.handlers.done._load_bank_entries", new=load_bank_mock),
    ]
    for p in patches:
        p.start()
    try:
        await handle_done(db=db, neo=MagicMock(), session_id=11)
    finally:
        for p in reversed(patches):
            p.stop()
    load_bank_mock.assert_not_awaited()


# ── sub-flag ON: structural co-key dock reaches misconceptions[] ────────────


async def test_struct_flag_on_docks_via_structural_cokey(monkeypatch):
    """Sub-flag ON + a bank entry opposing the localized node's entity_key ->
    the structural co-key branch docks, the rubric score drops below the
    OLD-path 90, and write_artifacts receives a MergeOutcome whose
    misconceptions[] carries the bare `misc.<code>` canonical key."""
    write_mock = AsyncMock(return_value=None)
    monkeypatch.setenv("APOLLO_GRADING_ARTIFACT_ENABLED", "true")

    out, gate_spy = await _run(
        monkeypatch,
        struct_flag="true",
        bank_entries=(_bank_entry_opposing_real_basis(),),
        write_mock=write_mock,
    )

    assert gate_spy.call_args.kwargs["opposes_index"] == {_NODE_ID: _BANK_CODE}
    assert out["rubric"]["overall"]["score"] < 90, (
        "structural co-key dock must reduce the student-facing score; got "
        f"{out['rubric']['overall']['score']}"
    )

    write_mock.assert_awaited_once()
    outcome = write_mock.await_args.kwargs["detection_outcome"]
    assert outcome is not None
    assert outcome.misconception_penalty > 0
    assert outcome.misconceptions
    assert outcome.misconceptions[0]["canonical_key"] == f"misc.{_BANK_CODE}"


async def test_struct_flag_on_but_no_opposing_bank_entry_docks_nothing(monkeypatch):
    """Sub-flag ON but the bank is empty (or has no entry opposing this
    node's entity_key) -> opposes_index is empty and the finding still falls
    through to clarify/drop, never `misconception` — the flag alone changes
    nothing without a real graph-name."""
    out, gate_spy = await _run(monkeypatch, struct_flag="true", bank_entries=())
    assert gate_spy.call_args.kwargs["opposes_index"] == {}
    assert out["rubric"]["overall"]["score"] == 90


async def test_struct_flag_on_bank_entry_opposes_different_key_docks_nothing(monkeypatch):
    """A bank entry with a NON-matching `opposes` never resolves into the
    index (build_opposes_index misses the lookup) -> no dock."""
    entry = _bank_entry_opposing_real_basis(opposes="def.some_other_concept")
    out, gate_spy = await _run(monkeypatch, struct_flag="true", bank_entries=(entry,))
    assert gate_spy.call_args.kwargs["opposes_index"] == {}
    assert out["rubric"]["overall"]["score"] == 90


# ── bank load soft-fail (within the outer T13 try/except envelope too) ──────


async def test_struct_flag_on_bank_load_failure_degrades_to_empty_index(monkeypatch):
    """`_load_bank_entries` raising is caught by done.py's own soft-fail (or
    bubbles into the outer T13 except, either way HTTP 200 + no structural
    dock) rather than ever propagating as a 500."""
    out, _gate_spy = await _run(
        monkeypatch,
        struct_flag="true",
        load_bank_side_effect=RuntimeError("db exploded"),
    )
    # Grade proceeded (no exception escaped) and, since the bank never
    # loaded, nothing docked structurally.
    assert out["rubric"]["overall"]["score"] == 90


# ── trace_attempt receives the SAME opposes_index (Task 9 param) ────────────


async def test_trace_attempt_receives_same_opposes_index_as_gate(monkeypatch):
    """When APOLLO_MISC_TRACE is also ON, trace_attempt is called with the
    IDENTICAL opposes_index object gate_findings used — proving the trace
    labels the real gate decision it is observing (Task 9's contract)."""
    monkeypatch.setenv("APOLLO_MISC_TRACE", "true")
    trace_mock = MagicMock(name="trace_attempt")

    db, _sess, _attempt, patches = _old_path_patches()
    monkeypatch.setenv(_DETECTOR_FLAG, "true")
    monkeypatch.setenv(_STRUCT_FLAG, "true")

    detection = DetectionResult(per_concept=(_unkeyed_judge_finding(),))
    patches = _patches_with_rubric(patches, _OLD_RUBRIC)

    async def _find_problem(_db, _cid, _code):
        problem = MagicMock()
        problem.id = "p_code"
        problem.problem_text = "text"
        problem.reference_solution = []
        problem.to_kg_graph.return_value = _reference_graph_with_entity_key()
        return problem

    patches += [
        patch(
            "apollo.handlers.done.detect_misconceptions",
            new=AsyncMock(return_value=detection),
        ),
        patch("apollo.handlers.done.make_openai_judge", new=MagicMock()),
        patch("apollo.handlers.done._default_embed_fn", new=MagicMock()),
        patch(
            "apollo.handlers.done._student_utterances",
            new=AsyncMock(return_value=("x",)),
        ),
        patch(
            "apollo.handlers.done._load_bank_entries",
            new=AsyncMock(return_value=(_bank_entry_opposing_real_basis(),)),
        ),
        patch("apollo.handlers.done._find_problem", new=AsyncMock(side_effect=_find_problem)),
        patch(
            "apollo.overseer.misconception_detector.trace.trace_attempt",
            new=trace_mock,
        ),
    ]
    for p in patches:
        p.start()
    try:
        await handle_done(db=db, neo=MagicMock(), session_id=11)
    finally:
        for p in reversed(patches):
            p.stop()

    trace_mock.assert_called_once()
    assert trace_mock.call_args.kwargs["opposes_index"] == {_NODE_ID: _BANK_CODE}


# ── _load_bank_entries — direct unit tests (mirrors detector._load_bank) ────


async def test_load_bank_entries_none_concept_id_returns_empty_without_query():
    """No concept_id -> empty bank, and the DB is never even queried (a valid,
    common case per the docstring — not every attempt is concept-scoped)."""
    db = MagicMock()
    db.execute = AsyncMock(side_effect=AssertionError("must not query when concept_id is None"))

    out = await _load_bank_entries(db, concept_id=None)

    assert out == ()
    db.execute.assert_not_called()


async def test_load_bank_entries_returns_tuple_of_loaded_entries():
    """A successful load_for_concept call returns its rows as an immutable
    tuple (never the raw list)."""
    entries = [_bank_entry_opposing_real_basis()]
    db = MagicMock()

    with patch(
        "apollo.handlers.done.load_for_concept",
        new=AsyncMock(return_value=entries),
    ) as load_mock:
        out = await _load_bank_entries(db, concept_id=3)

    load_mock.assert_awaited_once_with(db, concept_id=3)
    assert out == tuple(entries)
    assert isinstance(out, tuple)


async def test_load_bank_entries_soft_fails_to_empty_on_db_error():
    """A transient load_for_concept failure degrades to an empty bank rather
    than raising — the outer T13 try/except never even sees it, and
    build_opposes_index on an empty bank returns {} (byte-identical no-op)."""
    db = MagicMock()

    with patch(
        "apollo.handlers.done.load_for_concept",
        new=AsyncMock(side_effect=RuntimeError("transient db failure")),
    ):
        out = await _load_bank_entries(db, concept_id=3)

    assert out == ()
