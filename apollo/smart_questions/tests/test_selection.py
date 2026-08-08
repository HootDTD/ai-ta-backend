"""P1.2a + P2.4: the deterministic question-target policy.

Graded reference nodes are the only ones the grade is computed over, so the
questioning loop must probe every one of them before it spends budget on
ungraded (definition / variable_mapping) territory, and it must never probe a
node more than twice.
"""

from __future__ import annotations

import pytest

from apollo.ontology import KGGraph, NodeType, build_node
from apollo.smart_questions import selection
from apollo.smart_questions.unified import TallyState, TallyUpdate


def _node(node_id: str, node_type: NodeType):
    content = {
        "definition": {"concept": node_id, "meaning": "meaning"},
        "variable_mapping": {"term": node_id, "symbol": "v"},
        "procedure_step": {"action": node_id, "purpose": "purpose"},
        "condition": {"applies_when": node_id},
        "simplification": {"transformation": node_id, "applies_when": "always"},
        "equation": {"symbolic": f"{node_id} = 1"},
    }[node_type]
    return build_node(
        node_type=node_type,
        node_id=node_id,
        attempt_id=1,
        source="reference",
        content=content,
    )


def _graph(*pairs: tuple[str, NodeType]) -> KGGraph:
    return KGGraph(nodes=[_node(node_id, node_type) for node_id, node_type in pairs], edges=[])


def _mixed_graph() -> KGGraph:
    """Two ungraded nodes interleaved with two graded ones, ungraded first."""
    return _graph(
        ("def_a", "definition"),
        ("eq_b", "equation"),
        ("var_c", "variable_mapping"),
        ("step_d", "procedure_step"),
    )


def _policy(graph: KGGraph, *, state=(), updates=(), asked: int = 0, cap: int = 8):
    return selection.build_selection_policy(
        reference_graph=graph,
        tally_state=state,
        updates=updates,
        questions_asked=asked,
        cap=cap,
    )


def test_graded_types_match_the_grader_and_ordering_puts_them_first():
    # Single source of truth: the set the topic scorer grades on.
    assert selection.GRADED_NODE_TYPES == {
        "equation",
        "condition",
        "simplification",
        "procedure_step",
    }
    ordered = [node.node_id for node in selection.ordered_nodes(_mixed_graph())]
    assert ordered == ["eq_b", "step_d", "def_a", "var_c"]


def test_askable_is_graded_first_and_reserves_budget_for_open_graded_nodes():
    policy = _policy(_mixed_graph(), asked=6, cap=8)
    # 2 graded nodes still open, 2 questions left → nothing may go to ungraded.
    assert policy.graded_ids == ("eq_b", "step_d")
    assert policy.reserved_for_graded == 2
    assert policy.graded_only is True
    assert policy.askable_ids == ("eq_b", "step_d")


def test_ungraded_stay_askable_while_budget_exceeds_the_reservation():
    policy = _policy(_mixed_graph(), asked=5, cap=8)
    assert policy.graded_only is False
    assert policy.askable_ids == ("eq_b", "step_d", "def_a", "var_c")


def test_understood_and_twice_asked_nodes_leave_the_askable_set():
    state = (
        TallyState("eq_b", "eq", "understood"),
        TallyState("step_d", "step", "tentative", times_asked=selection.MAX_ASKS_PER_NODE),
        TallyState("def_a", "def", "missing", times_asked=1),
    )
    policy = _policy(_mixed_graph(), state=state)
    assert selection.MAX_ASKS_PER_NODE == 2
    assert policy.askable_ids == ("def_a", "var_c")
    # step_d is unmastered but out of asks, so it no longer reserves budget.
    assert policy.open_graded_ids == ("step_d",)
    assert policy.reserved_for_graded == 0


def test_this_turn_updates_override_the_prior_tally():
    state = (TallyState("eq_b", "eq", "missing"), TallyState("step_d", "step", "missing"))
    policy = _policy(
        _mixed_graph(),
        state=state,
        updates=(TallyUpdate("eq_b", "understood"),),
        asked=6,
        cap=8,
    )
    assert policy.askable_ids == ("step_d", "def_a", "var_c")
    assert policy.open_graded_topics == 1


def test_a_problem_with_no_graded_nodes_never_locks_the_askable_set():
    policy = _policy(_graph(("def_a", "definition")), asked=7, cap=8)
    assert policy.graded_ids == ()
    assert policy.graded_only is False
    assert policy.askable_ids == ("def_a",)


def test_fully_covered_graph_has_no_askable_target():
    state = (
        TallyState("eq_b", "eq", "understood"),
        TallyState("step_d", "step", "understood"),
        TallyState("def_a", "def", "understood"),
        TallyState("var_c", "var", "understood"),
    )
    policy = _policy(_mixed_graph(), state=state)
    assert policy.askable_ids == ()
    assert policy.open_graded_topics == 0


@pytest.mark.parametrize("status", ["missing", "tentative", "conflicting"])
def test_every_unmastered_status_counts_as_open_graded(status):
    policy = _policy(_mixed_graph(), state=(TallyState("eq_b", "eq", status),))
    assert policy.graded_topic_total == 2
    assert policy.open_graded_topics == 2
    assert "eq_b" in policy.askable_ids


def test_exhausted_budget_still_reserves_every_open_graded_node():
    policy = _policy(_mixed_graph(), asked=8, cap=8)
    assert policy.graded_only is True
    assert policy.askable_ids == ("eq_b", "step_d")
