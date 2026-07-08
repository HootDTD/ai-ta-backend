"""Trace + observability tests for the clarification-v2 pipeline (integration
spec §7, task T13). Covers: json.dumps round-trip, pool/ranked/questions
(topic keys + hint_dims only -- L1)/dedup/budget/seeded presence, the
leak-guard assertion (no rendered-hint or candidate-derived text anywhere in
the serialized trace), and the fallback log firing."""

from __future__ import annotations

import json
import logging

from apollo.clarification import turn, v2_selection
from apollo.clarification.probe import _FALLBACK, _HINT_BY_TYPE
from apollo.resolution.candidates import Candidate
from apollo.resolver_v2.incremental_types import IncrementalSnapshot
from apollo.resolver_v2.types import EdgeScore

# The full set of rendered hint strings the pipeline could ever emit -- none
# of these substrings may appear anywhere in a serialized trace (L1).
_ALL_RENDERED_HINTS: tuple[str, ...] = (*_HINT_BY_TYPE.values(), _FALLBACK)


def _candidate(key: str, node_type: str, display: str) -> Candidate:
    return Candidate(
        canonical_key=key,
        canon_key=1,
        node_type=node_type,
        is_misconception=False,
        symbolic=None,
        aliases=(),
        display_name=display,
        opposes_key=None,
        exact_aliases=(),
    )


def _edge(from_key: str, to_key: str, credit: float = 0.0, evidence: str = "none") -> EdgeScore:
    return EdgeScore(
        edge_type="USES", from_key=from_key, to_key=to_key, credit=credit, relation_evidence=evidence
    )


def _snapshot(
    node_credits: dict[str, float],
    gray: frozenset[str],
    *,
    edge_scores: tuple = (),
    pair_count_this_turn: int = 0,
    budget_truncated: bool = False,
) -> IncrementalSnapshot:
    return IncrementalSnapshot(
        node_credits=node_credits,
        edge_scores=edge_scores,
        node_cov=0.5,
        edge_cov=0.5,
        winning_path_index=0,
        gray=gray,
        pair_count_this_turn=pair_count_this_turn,
        budget_truncated=budget_truncated,
    )


def _patch_store(monkeypatch, asked_keys=frozenset(), writes=None):
    if writes is None:
        writes = []

    async def fake_load(db, *, attempt_id):
        return set(asked_keys)

    async def fake_write(db, **kw):
        writes.append(kw)

    monkeypatch.setattr(v2_selection, "load_asked_candidate_keys", fake_load)
    monkeypatch.setattr(v2_selection, "write_asked_waiting", fake_write)
    return writes


def _no_leaked_text(serialized: str, *, extra_forbidden: tuple[str, ...] = ()) -> None:
    lowered = serialized.lower()
    for hint in _ALL_RENDERED_HINTS:
        assert hint.lower() not in lowered, f"rendered hint text leaked into trace: {hint!r}"
    for forbidden in extra_forbidden:
        assert forbidden.lower() not in lowered, f"candidate text leaked into trace: {forbidden!r}"


async def test_trace_round_trips_json_and_contains_all_required_sections(monkeypatch, caplog):
    """End-to-end: pool/ranked/questions/dedup/budget/seeded all present,
    survives json.dumps, and the leak-guard holds (no rendered hint text or
    candidate display_name/content anywhere in the payload)."""
    writes = _patch_store(monkeypatch, asked_keys=frozenset({"cond.already_asked"}))

    node_credits = {
        "cond.bernoulli": 0.3,
        "cond.already_asked": 0.2,
        "eq.venturi": 0.0,
    }
    snapshot = _snapshot(
        node_credits,
        gray=frozenset({"cond.bernoulli", "cond.already_asked"}),
        edge_scores=(_edge("cond.bernoulli", "eq.venturi"),),
        pair_count_this_turn=4,
        budget_truncated=False,
    )
    candidates = (
        _candidate("cond.bernoulli", "condition", "SECRET PRESSURE CLAIM"),
        _candidate("cond.already_asked", "condition", "SECRET ALREADY ASKED CLAIM"),
        _candidate("eq.venturi", "equation", "SECRET VENTURI EQUATION"),
    )

    caplog.set_level(logging.INFO, logger="apollo.clarification.v2_selection")

    hints = await v2_selection.select(
        snapshot,
        candidates,
        db=object(),
        attempt_id=42,
        session_id=1,
        user_id="u",
        search_space_id=1,
        concept_id=2,
        asked_turn=3,
        snapshot_source="prior_turn",
        pair_count_total=9,
        seeded_keys=frozenset({"cond.seeded"}),
    )

    assert hints  # sanity: the pipeline actually produced probes

    # Pull the emitted trace payload out of the log record.
    record = next(r for r in caplog.records if r.message.startswith("clarification_v2_trace"))
    serialized = record.message.split("trace=", 1)[1]

    # 1) json.dumps round-trip.
    payload = json.loads(serialized)
    re_dumped = json.dumps(payload)
    assert json.loads(re_dumped) == payload

    block = payload["clarification_v2"]
    assert block["enabled"] is True
    assert block["snapshot_source"] == "prior_turn"

    # 2) required sections present.
    assert block["pool"], "pool must be non-empty"
    for entry in block["pool"]:
        assert set(entry) == {"canonical_key", "node_type", "node_credit", "is_gray", "source"}

    assert block["ranked"], "ranked must be non-empty"
    for entry in block["ranked"]:
        assert set(entry) == {"canonical_key", "importance", "uncertainty", "voi"}

    assert block["questions"], "questions must be non-empty"
    for q in block["questions"]:
        assert set(q) == {"question_index", "topic_keys", "hint_dims"}
        assert len(q["hint_dims"]) == len(q["topic_keys"])
        # hint_dims are dimension TYPES only.
        for dim in q["hint_dims"]:
            assert dim in {"direction", "variable", "condition", "definition", "action",
                            "relationship", "general"}

    assert block["asked_dedup_skipped"] == ["cond.already_asked"]

    assert set(block["budget"]) == {"pair_count_this_turn", "pair_count_total", "budget_truncated"}
    assert block["budget"]["pair_count_this_turn"] == 4
    assert block["budget"]["pair_count_total"] == 9
    assert block["budget"]["budget_truncated"] is False

    assert block["seeded"] == ["cond.seeded"]

    # 3) leak-guard: no rendered hint text, no candidate display_name/content.
    # (canonical_key substrings like "bernoulli"/"venturi" ARE expected --
    # only the candidate's rendered display_name/content text is forbidden.)
    _no_leaked_text(
        serialized,
        extra_forbidden=(
            "SECRET PRESSURE CLAIM",
            "SECRET ALREADY ASKED CLAIM",
            "SECRET VENTURI EQUATION",
        ),
    )

    assert len(writes) >= 1


async def test_empty_pool_still_emits_a_valid_trace(monkeypatch, caplog):
    _patch_store(monkeypatch)
    snapshot = _snapshot({"cond.a": 0.95}, gray=frozenset())  # nothing gray/missing
    candidates = (_candidate("cond.a", "condition", "SECRET"),)

    caplog.set_level(logging.INFO, logger="apollo.clarification.v2_selection")

    hints = await v2_selection.select(
        snapshot,
        candidates,
        db=object(),
        attempt_id=1,
        session_id=1,
        user_id="u",
        search_space_id=1,
        concept_id=None,
        asked_turn=1,
    )
    assert hints == []

    record = next(r for r in caplog.records if r.message.startswith("clarification_v2_trace"))
    serialized = record.message.split("trace=", 1)[1]
    payload = json.loads(serialized)
    block = payload["clarification_v2"]
    assert block["pool"] == []
    assert block["ranked"] == []
    assert block["questions"] == []
    assert block["asked_dedup_skipped"] == []
    assert block["seeded"] == []
    _no_leaked_text(serialized, extra_forbidden=("SECRET",))


def test_trace_dir_set_appends_jsonl_line(monkeypatch, tmp_path):
    """When APOLLO_RESOLVER_V2_TRACE_DIR is set, the trace is appended (not
    overwritten) to this attempt's per-attempt file -- reuses the resolver_v2
    trace sink's directory, spec §7 "no new sink"."""
    monkeypatch.setenv("APOLLO_RESOLVER_V2_TRACE_DIR", str(tmp_path))

    trace = v2_selection.build_empty_trace(snapshot_source="none_v1_fallback")
    v2_selection.emit_trace(trace, attempt_id=99)
    v2_selection.emit_trace(trace, attempt_id=99)

    path = tmp_path / "attempt_99_clarification_v2.jsonl"
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    for line in lines:
        payload = json.loads(line)
        assert payload["clarification_v2"]["snapshot_source"] == "none_v1_fallback"


def test_trace_is_frozen_dataclasses():
    from dataclasses import is_dataclass

    trace = v2_selection.build_empty_trace(snapshot_source="none_v1_fallback")
    assert is_dataclass(trace)
    assert trace.__dataclass_params__.frozen
    assert trace.budget.__dataclass_params__.frozen


async def test_ranker_on_but_resolver_v2_off_emits_none_v1_fallback_trace(monkeypatch, caplog):
    """§8.2 row 3: no snapshot -> trace records snapshot_source='none_v1_fallback'
    and the existing clarification_v2_no_resolver_v2 log still fires."""
    monkeypatch.setenv("APOLLO_CLARIFICATION_V2_RANKER", "true")
    monkeypatch.setenv("APOLLO_CLARIFICATION_ENABLED", "true")
    monkeypatch.delenv("APOLLO_RESOLVER_V2", raising=False)

    async def fake_v1_select(**kw):
        return ["v1 hint"]

    monkeypatch.setattr(turn, "_v1_select", fake_v1_select)

    caplog.set_level(logging.INFO)

    hints = await turn.run_clarification_detection(
        db=object(),
        parsed_nodes=[object()],
        candidates=(_candidate("cond.a", "condition", "d"),),
        symbolic_mappings={},
        embedder=object(),
        cache=object(),
        attempt_id=5,
        session_id=1,
        user_id="u",
        search_space_id=1,
        concept_id=None,
        asked_turn=1,
        snapshot=None,
    )

    assert hints == ["v1 hint"]
    assert "clarification_v2_no_resolver_v2" in caplog.text

    record = next(r for r in caplog.records if r.message.startswith("clarification_v2_trace"))
    payload = json.loads(record.message.split("trace=", 1)[1])
    assert payload["clarification_v2"]["snapshot_source"] == "none_v1_fallback"
    assert payload["clarification_v2"]["enabled"] is True


async def test_v2_exception_fallback_log_fires_with_exception_class_no_transcript(
    monkeypatch, caplog
):
    """Fallback log contract (spec §7/§8.3): exception class + attempt_id,
    never transcript text."""
    monkeypatch.setenv("APOLLO_CLARIFICATION_V2_RANKER", "true")
    monkeypatch.setenv("APOLLO_RESOLVER_V2", "true")
    monkeypatch.setenv("APOLLO_CLARIFICATION_ENABLED", "true")

    async def boom(*a, **kw):
        raise ValueError("student said something secret")

    monkeypatch.setattr(turn.v2_selection, "select", boom)

    async def fake_v1_select(**kw):
        return ["v1 hint"]

    monkeypatch.setattr(turn, "_v1_select", fake_v1_select)

    caplog.set_level(logging.WARNING, logger="apollo.clarification.turn")

    snapshot = _snapshot({"cond.bernoulli": 0.3}, gray=frozenset({"cond.bernoulli"}))
    hints = await turn.run_clarification_detection(
        db=object(),
        parsed_nodes=[object()],
        candidates=(_candidate("cond.bernoulli", "condition", "d"),),
        symbolic_mappings={},
        embedder=object(),
        cache=object(),
        attempt_id=6,
        session_id=1,
        user_id="u",
        search_space_id=1,
        concept_id=None,
        asked_turn=1,
        snapshot=snapshot,
    )

    assert hints == ["v1 hint"]
    assert "clarification_v2_ranker_failed_falling_back_to_v1" in caplog.text
    assert "ValueError" in caplog.text
    assert "attempt_id=6" in caplog.text
    assert "student said something secret" not in caplog.text
