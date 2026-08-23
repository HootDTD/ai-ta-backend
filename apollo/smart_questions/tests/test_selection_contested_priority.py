"""P3.2 L2a — contested nodes sort to the front of the graded block.

A node the questioning engine labelled with a wrongness is ALREADY askable (it
is not ``understood``); the signal only buys it priority. The ask cap stays 2 —
a contradiction consumes the second ask, it never creates a third — and an
empty ``contested_ids`` must produce a policy identical to the pre-P3.2 one.
"""

from __future__ import annotations

from apollo.ontology import KGGraph, NodeType, build_node
from apollo.smart_questions import selection
from apollo.smart_questions.unified import TallyState


def _node(node_id: str, node_type: NodeType):
    content: dict[NodeType, dict[str, str]] = {
        "definition": {"concept": node_id, "meaning": "meaning"},
        "variable_mapping": {"term": node_id, "symbol": "v"},
        "procedure_step": {"action": node_id, "purpose": "purpose"},
        "condition": {"applies_when": node_id},
        "simplification": {"transformation": node_id, "applies_when": "always"},
        "equation": {"symbolic": f"{node_id} = 1"},
    }
    return build_node(
        node_type=node_type,
        node_id=node_id,
        attempt_id=1,
        source="reference",
        content=content[node_type],
    )


def _graph(*pairs: tuple[str, NodeType]) -> KGGraph:
    return KGGraph(nodes=[_node(node_id, node_type) for node_id, node_type in pairs], edges=[])


def _mixed_graph() -> KGGraph:
    return _graph(
        ("def_a", "definition"),
        ("eq_b", "equation"),
        ("var_c", "variable_mapping"),
        ("step_d", "procedure_step"),
        ("cond_e", "condition"),
    )


def _policy(graph: KGGraph, *, state=(), contested=(), asked: int = 0, cap: int = 8):
    return selection.build_selection_policy(
        reference_graph=graph,
        tally_state=state,
        questions_asked=asked,
        cap=cap,
        contested_ids=contested,
    )


def test_contested_nodes_sort_first_within_probeable_graded():
    graph = _mixed_graph()
    baseline = _policy(graph)
    assert baseline.askable_ids == ("eq_b", "step_d", "cond_e", "def_a", "var_c")

    policy = _policy(graph, contested=("cond_e",))
    # The contested node moves to the head of the GRADED block only; the ungraded
    # tail keeps its place, and nothing new becomes askable.
    assert policy.askable_ids == ("cond_e", "eq_b", "step_d", "def_a", "var_c")
    assert set(policy.askable_ids) == set(baseline.askable_ids)


def test_contested_ordering_is_stable_among_contested_and_uncontested():
    policy = _policy(_mixed_graph(), contested=("cond_e", "eq_b"))
    # Reference order preserved WITHIN each partition: eq_b before cond_e.
    assert policy.askable_ids[:3] == ("eq_b", "cond_e", "step_d")


def test_empty_contested_ids_is_identical_policy():
    graph = _mixed_graph()
    state = (TallyState("eq_b", "eq_b", "tentative", times_asked=1),)
    for asked, cap in ((0, 8), (6, 8), (7, 8)):
        without = selection.build_selection_policy(
            reference_graph=graph, tally_state=state, questions_asked=asked, cap=cap
        )
        empty = _policy(graph, state=state, contested=(), asked=asked, cap=cap)
        assert without == empty


def test_contested_node_at_ask_cap_stays_unaskable():
    graph = _mixed_graph()
    state = (
        TallyState("cond_e", "cond_e", "conflicting", times_asked=selection.MAX_ASKS_PER_NODE),
    )
    policy = _policy(graph, state=state, contested=("cond_e",))
    # P2.4 confirm-once: the cap outranks the contradiction. A contradiction
    # consumes the SECOND ask; it never creates a third.
    assert "cond_e" not in policy.askable_ids
    assert selection.MAX_ASKS_PER_NODE == 2


def test_contested_understood_node_stays_unaskable():
    state = (TallyState("cond_e", "cond_e", "understood", times_asked=0),)
    policy = _policy(_mixed_graph(), state=state, contested=("cond_e",))
    assert "cond_e" not in policy.askable_ids


def test_contested_priority_never_changes_the_reservation_or_the_graded_gate():
    graph = _mixed_graph()
    plain = _policy(graph, asked=6, cap=8)
    contested = _policy(graph, contested=("cond_e",), asked=6, cap=8)
    assert plain.reserved_for_graded == contested.reserved_for_graded == 3
    assert plain.graded_only is contested.graded_only is True
    assert plain.graded_ids == contested.graded_ids


def test_unknown_contested_id_is_ignored():
    policy = _policy(_mixed_graph(), contested=("not_a_node",))
    assert policy.askable_ids == ("eq_b", "step_d", "cond_e", "def_a", "var_c")
