"""The P3.2 seams that no single wave-1 slice could test alone.

Each wave-1 agent proved its own half of a contract against a hand-written
stand-in of the other half — which is exactly how two halves drift into
agreement with a fiction and disagreement with each other. These tests join the
REAL producers to the REAL consumers across slice boundaries:

* **S2 round trip** — `controller._evidence_entry` (the writer, W1-A) parsed by
  `wrongness.ledger_findings` (the reader, W1-C). Five key names have to match
  byte for byte, and the two modules never import each other.
* **S7 protocol conformance** — `wrongness.WrongnessFinding` (W1-C) against
  `topic_score.MisconceptionSpec` (W1-D). The protocol is STRUCTURAL on purpose
  (importing the wrongness types into the scorer's signature would close an
  import cycle), so nothing but a test can prove the shapes still line up.

If any of these fail, the failure belongs to the seam, not to either module.
"""

from __future__ import annotations

from typing import Any

import pytest

from apollo.overseer import topic_score, wrongness
from apollo.smart_questions.controller import _evidence_entry
from apollo.smart_questions.unified import Contradiction, EvidenceQuote, TallyUpdate

pytestmark = pytest.mark.unit

_NODE = "eq.demand"
_QUOTE = "raising the price raises demand too"
_CLAUSE = "a price rise lowers demand"
_KIND = "inverted relationship"


class _Row:
    """A `QuestionOpportunity`-shaped row carrying whatever the writer produced."""

    def __init__(self, evidence: list[dict[str, Any]], *, state: str = "conflicting") -> None:
        self.reference_node_id = _NODE
        self.state = state
        self.times_asked = 1
        self.last_asked_turn = 1
        self.evidence = evidence


def _tagged_update() -> TallyUpdate:
    return TallyUpdate(
        node_id=_NODE,
        status="conflicting",
        evidence=EvidenceQuote(2, _QUOTE),
        wrongness="contradicts_material",
        contradiction=Contradiction(reference_clause=_CLAUSE, kind=_KIND),
    )


def _clean_update() -> TallyUpdate:
    return TallyUpdate(node_id=_NODE, status="understood", evidence=EvidenceQuote(0, _CLAUSE))


# --------------------------------------------------------------------------- #
# S2 — writer/reader round trip
# --------------------------------------------------------------------------- #
def test_s2_tagged_entry_written_by_the_controller_parses_as_a_finding() -> None:
    """The seam, end to end: what the producer writes is what the core reads.

    The two modules agree on five bare string keys and share no constant, so
    renaming one on either side is a silent data loss (the entry keeps being
    written, the finding quietly stops existing). This is the test that fails.
    """
    entry = _evidence_entry(_tagged_update())

    findings = wrongness.ledger_findings([_Row([entry])])

    assert len(findings) == 1
    finding = findings[0]
    assert (finding.node_id, finding.turn_id, finding.quote) == (_NODE, 2, _QUOTE)
    assert (finding.wrongness, finding.contradicts, finding.kind) == (
        wrongness.WRONGNESS_MATERIAL,
        _CLAUSE,
        _KIND,
    )
    assert finding.is_latest_evidence is True


def test_s2_key_names_are_exactly_the_five_the_reader_consumes() -> None:
    """Pinned as a set, so an ADDED key is caught too — an extra key changes the
    dedup identity (`if serialized not in evidence`) and silently re-appends an
    entry the ledger already holds."""
    assert set(_evidence_entry(_tagged_update())) == {
        "turn_id",
        "quote",
        "wrongness",
        "contradicts",
        "kind",
    }


def test_s2_untagged_entry_round_trips_as_wrongness_none() -> None:
    """Level 0 writes two keys and the reader must still produce a finding row
    for it (with `wrongness == "none"`), because `candidate_quotes` and
    `select_findings` filter on the LABEL, never on the entry's shape."""
    findings = wrongness.ledger_findings(
        [_Row([_evidence_entry(_clean_update())], state="understood")]
    )

    assert len(findings) == 1
    assert findings[0].wrongness == wrongness.WRONGNESS_NONE
    assert (findings[0].contradicts, findings[0].kind) == ("", "")


def test_s2_mixed_ledger_marks_only_the_last_entry_latest() -> None:
    """The controller appends in turn order and S2′ keys on recency, so a clean
    turn AFTER a wrongness turn must retire the finding — the ordering contract
    that makes "the student revised" work without a second write path."""
    entries = [_evidence_entry(_tagged_update()), _evidence_entry(_clean_update())]

    findings = wrongness.ledger_findings([_Row(entries)])

    assert [(f.wrongness, f.is_latest_evidence) for f in findings] == [
        (wrongness.WRONGNESS_MATERIAL, False),
        (wrongness.WRONGNESS_NONE, True),
    ]
    assert wrongness.candidate_quotes(findings, graded_node_ids={_NODE}) == {}


def test_s2_writer_output_survives_a_json_round_trip() -> None:
    """The column is JSONB, so the entry makes a Python -> JSON -> Python hop
    before the reader ever sees it. Nothing in the tagged shape may depend on a
    Python-only type."""
    import json

    entry = json.loads(json.dumps(_evidence_entry(_tagged_update())))

    assert wrongness.ledger_findings([_Row([entry])])[0].contradicts == _CLAUSE


# --------------------------------------------------------------------------- #
# S7 — structural protocol conformance
# --------------------------------------------------------------------------- #
def _finding(**overrides: Any) -> wrongness.WrongnessFinding:
    values: dict[str, Any] = {
        "node_id": _NODE,
        "quote": _QUOTE,
        "contradicts": _CLAUSE,
        "kind": _KIND,
        "corroborated": True,
        "resolved": False,
        "apollo_elicited": True,
        "would_ceiling": False,
    }
    values.update(overrides)
    return wrongness.WrongnessFinding(**values)


def test_wrongness_finding_satisfies_the_misconception_protocol() -> None:
    """S7 says `MisconceptionSpec` is "satisfied structurally by
    `WrongnessFinding`". Nothing enforced that claim: the scorer deliberately
    does not import the wrongness types, and W1-D's own tests used a local stub.
    """
    assert isinstance(_finding(), topic_score.MisconceptionSpec)


def test_the_protocol_asks_for_exactly_three_members() -> None:
    """A widened protocol is a silent break — `WrongnessFinding` would stop
    conforming with no import to flag it. Pinned so adding a member is a
    deliberate, two-slice edit.

    Read off the class body (`vars`) rather than `__protocol_attrs__`, which is
    a CPython implementation detail with no typed public surface.
    """
    members = {name for name in vars(topic_score.MisconceptionSpec) if not name.startswith("_")}
    assert members == {"node_id", "quote", "resolved"}


def test_a_real_finding_drives_the_scorer_containers() -> None:
    """Conformance that runs, not just conformance that type-checks: a real
    `WrongnessFinding` reaches `topics[].misconceptions` with its own quote."""
    nodes, centrality, coverage = _scorer_inputs()

    result = topic_score.compute_topic_score(
        coverage=coverage,
        reference_nodes=nodes,
        centrality=centrality,
        misconceptions={_NODE: _finding()},
    )

    populated = [topic for topic in result.topics if topic.misconceptions]
    assert len(populated) == 1
    assert populated[0].misconceptions[0].evidence_span == _QUOTE
    assert populated[0].misconceptions[0].canonical_key == _NODE
    assert populated[0].misconceptions[0].resolved is False


def test_level_three_shape_moves_no_number() -> None:
    """The S7 level-3 promise across the slice boundary: containers fill,
    `score`/`letter`/`coverage_component`/`misconception_dock` do not move."""
    nodes, centrality, coverage = _scorer_inputs()
    kwargs: dict[str, Any] = {
        "coverage": coverage,
        "reference_nodes": nodes,
        "centrality": centrality,
    }

    dark = topic_score.compute_topic_score(**kwargs)
    surfaced = topic_score.compute_topic_score(**kwargs, misconceptions={_NODE: _finding()})

    assert (surfaced.score, surfaced.letter) == (dark.score, dark.letter)
    assert surfaced.coverage_component == dark.coverage_component
    assert surfaced.misconception_dock == dark.misconception_dock == 0.0


def _scorer_inputs() -> tuple[list[Any], dict[str, float], dict[str, Any]]:
    from apollo.ontology import KGGraph, build_node

    nodes = [
        build_node(
            node_type="procedure_step",
            node_id=node_id,
            attempt_id=1,
            source="reference",
            content={"action": f"Do {node_id}", "purpose": ""},
        )
        for node_id in (_NODE, "eq.supply")
    ]
    coverage = {
        "per_step": {_NODE: True, "eq.supply": True},
        "procedure_scores": {_NODE: 1.0, "eq.supply": 1.0},
        "confidences": {},
        "negotiation_counts": {},
    }
    return nodes, topic_score.compute_centrality(KGGraph(nodes=nodes)), coverage
