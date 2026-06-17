"""WU-4A2 Task 1 — the in-memory finding vocabulary (findings.py).

RED-first: these assert the §2 finding-kind set, the frozen ``Finding`` shape,
and the pure reducer helpers BEFORE the module exists. Every input is a
hand-built frozen dataclass; nothing here touches an LLM/DB/network.
"""

from __future__ import annotations

import dataclasses

import pytest

from apollo.graph_compare.findings import (
    Finding,
    FindingKind,
    alternative_path_finding,
    contradiction_finding,
    covered_finding,
    matched_edge_finding,
    missing_edge_finding,
    missing_finding,
    unresolved_finding,
    unsupported_extra_finding,
)
from apollo.ontology.edges import EdgeType
from apollo.persistence import models

from ._builders import cedge, cnode, rnode


def test_finding_kind_enum_matches_models_finding_kinds():
    assert {k.value for k in FindingKind} == set(models.FINDING_KINDS)


def test_covered_finding_carries_evidence_and_confidence():
    node = cnode(
        "eq.bernoulli",
        source_node_ids=("n1", "n2"),
        evidence_spans=("p1 + ...", "and again"),
        confidence=0.92,
    )
    f = covered_finding(node)
    assert f.kind is FindingKind.COVERED_NODE
    assert f.canonical_key == "eq.bernoulli"
    assert f.evidence_spans == ("p1 + ...", "and again")
    assert f.confidence == 0.92
    assert f.student_node_ids == ("n1", "n2")


def test_missing_finding_score_zero_no_event():
    ref = rnode("eq.continuity")
    f = missing_finding(ref)
    assert f.kind is FindingKind.MISSING_NODE
    assert f.score == 0.0
    assert f.reference_node_ids == ("ref_eq.continuity",)
    # No event field anywhere on the dataclass (sub-scores never produce events).
    field_names = {fld.name for fld in dataclasses.fields(Finding)}
    assert "event_kind" not in field_names
    assert "event" not in field_names


def test_contradiction_finding_carries_misconception_key():
    node = cnode(
        "misc.density_ignored",
        node_type="misconception",
        evidence_spans=("they ignored density",),
    )
    f = contradiction_finding(node)
    assert f.kind is FindingKind.CONTRADICTION
    assert f.score == 0.0
    assert f.canonical_key == "misc.density_ignored"
    assert f.evidence_spans == ("they ignored density",)


def test_unsupported_extra_finding_no_penalty_marker():
    node = cnode("eq.somethingelse", evidence_spans=("extra claim",))
    f = unsupported_extra_finding(node)
    assert f.kind is FindingKind.UNSUPPORTED_EXTRA
    assert f.score is None  # diagnostic only — no penalty marker
    assert f.evidence_spans == ("extra claim",)


def test_unresolved_finding_from_node_id_surface():
    f = unresolved_finding("n_raw_7", "some unparseable surface")
    assert f.kind is FindingKind.UNRESOLVED
    assert f.student_node_ids == ("n_raw_7",)
    assert f.evidence_spans == ("some unparseable surface",)


def test_matched_and_missing_edge_findings_diagnostic_only():
    edge = cedge(EdgeType.USES, "proc.apply", "eq.bernoulli", provenance="explicit")
    matched = matched_edge_finding(edge)
    missing = missing_edge_finding(edge)
    assert matched.kind is FindingKind.MATCHED_EDGE
    assert missing.kind is FindingKind.MISSING_EDGE
    for f in (matched, missing):
        assert "proc.apply" in f.message
        assert "eq.bernoulli" in f.message
        assert "USES" in f.message
        assert "explicit" in f.message
        # edges are keyed by from->to text in message; the id tuples stay empty.
        assert f.student_node_ids == ()
        assert f.reference_node_ids == ()


def test_alternative_path_finding_records_index_and_keys():
    f = alternative_path_finding(1, ("eq.alt1", "eq.alt2"))
    assert f.kind is FindingKind.ALTERNATIVE_PATH
    assert "1" in f.message
    # canonical_keys carried somewhere readable (reference_node_ids per schema).
    assert f.reference_node_ids == ("eq.alt1", "eq.alt2")


def test_finding_is_frozen():
    f = covered_finding(cnode("eq.x"))
    with pytest.raises(dataclasses.FrozenInstanceError):
        f.kind = FindingKind.MISSING_NODE  # type: ignore[misc]
