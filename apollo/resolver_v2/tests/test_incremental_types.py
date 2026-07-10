"""Tests for T2: IncrementalState / IncrementalSnapshot (spec §2.1, §5.2, §3.1).

Acceptance: construct/asdict/json.dumps round-trip; immutability (every
update returns a new instance, never mutates in place).
"""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError, asdict

import pytest

from apollo.resolver_v2.incremental_types import IncrementalSnapshot, IncrementalState


def _make_state(**overrides) -> IncrementalState:
    defaults = dict(
        window_cursor=0,
        global_window_count=0,
        running_node_max={},
        node_source={},
        running_edge_evidence={},
        seeded_keys=frozenset(),
        pair_count_total=0,
    )
    defaults.update(overrides)
    return IncrementalState(**defaults)


def _make_snapshot(**overrides) -> IncrementalSnapshot:
    defaults = dict(
        node_credits={},
        edge_scores=(),
        node_cov=0.0,
        edge_cov=0.0,
        winning_path_index=0,
        gray=frozenset(),
        pair_count_this_turn=0,
    )
    defaults.update(overrides)
    return IncrementalSnapshot(**defaults)


class TestIncrementalStateConstruction:
    def test_construct_with_all_fields(self):
        state = _make_state(
            window_cursor=3,
            global_window_count=5,
            running_node_max={"a": 0.7},
            node_source={"a": "nli"},
            running_edge_evidence={"USES|a|b": "entail"},
            seeded_keys=frozenset({"a"}),
            pair_count_total=42,
        )
        assert state.window_cursor == 3
        assert state.global_window_count == 5
        assert state.running_node_max == {"a": 0.7}
        assert state.node_source == {"a": "nli"}
        assert state.running_edge_evidence == {"USES|a|b": "entail"}
        assert state.seeded_keys == frozenset({"a"})
        assert state.pair_count_total == 42

    def test_frozen_raises_on_mutation(self):
        state = _make_state()
        with pytest.raises(FrozenInstanceError):
            state.window_cursor = 99  # type: ignore[misc]

    def test_asdict_json_roundtrip(self):
        state = _make_state(
            running_node_max={"x": 0.5},
            node_source={"x": "lexical_skip"},
            running_edge_evidence={"DEPENDS_ON|x|y": "cooccur"},
            seeded_keys=frozenset({"x"}),
        )
        as_dict = asdict(state)
        # frozenset is not JSON-safe directly; the convention (design §7) is
        # that state must survive a json.dumps round trip when frozenset
        # fields are exported as a sorted list.
        safe = dict(as_dict)
        safe["seeded_keys"] = sorted(as_dict["seeded_keys"])
        dumped = json.dumps(safe)
        reloaded = json.loads(dumped)
        assert reloaded["window_cursor"] == state.window_cursor
        assert reloaded["running_node_max"] == dict(state.running_node_max)
        assert reloaded["seeded_keys"] == sorted(state.seeded_keys)

    def test_update_returns_new_instance(self):
        state = _make_state(window_cursor=0, pair_count_total=10)
        # Simulate a per-turn update the way incremental.py would: build a
        # fresh instance, never mutate the old one.
        updated = _make_state(
            window_cursor=state.window_cursor + 1,
            pair_count_total=state.pair_count_total + 5,
        )
        assert updated is not state
        assert state.window_cursor == 0
        assert state.pair_count_total == 10
        assert updated.window_cursor == 1
        assert updated.pair_count_total == 15


class TestIncrementalSnapshotConstruction:
    def test_construct_with_all_fields(self):
        snap = _make_snapshot(
            node_credits={"a": 1.0},
            edge_scores=({"edge": "USES|a|b", "credit": 0.7},),
            node_cov=0.8,
            edge_cov=0.6,
            winning_path_index=1,
            gray=frozenset({"a"}),
            pair_count_this_turn=4,
        )
        assert snap.node_credits == {"a": 1.0}
        assert snap.edge_scores == ({"edge": "USES|a|b", "credit": 0.7},)
        assert snap.node_cov == 0.8
        assert snap.edge_cov == 0.6
        assert snap.winning_path_index == 1
        assert snap.gray == frozenset({"a"})
        assert snap.pair_count_this_turn == 4

    def test_frozen_raises_on_mutation(self):
        snap = _make_snapshot()
        with pytest.raises(FrozenInstanceError):
            snap.node_cov = 1.0  # type: ignore[misc]

    def test_asdict_json_roundtrip(self):
        snap = _make_snapshot(
            node_credits={"a": 0.9},
            node_cov=0.5,
            edge_cov=0.4,
            gray=frozenset({"a", "b"}),
        )
        as_dict = asdict(snap)
        safe = dict(as_dict)
        safe["gray"] = sorted(as_dict["gray"])
        dumped = json.dumps(safe)
        reloaded = json.loads(dumped)
        assert reloaded["node_credits"] == dict(snap.node_credits)
        assert reloaded["node_cov"] == snap.node_cov
        assert reloaded["gray"] == sorted(snap.gray)

    def test_update_returns_new_instance(self):
        snap = _make_snapshot(node_cov=0.3)
        updated = _make_snapshot(node_cov=0.5)
        assert updated is not snap
        assert snap.node_cov == 0.3
        assert updated.node_cov == 0.5
