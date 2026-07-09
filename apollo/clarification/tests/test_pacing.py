from apollo.clarification.detector import FlaggedNode
from apollo.clarification.pacing import MAX_PROBES_PER_TURN, rubric_weight_for, select_probes
from apollo.resolution.candidates import Candidate
from apollo.resolution.tests.test_resolver import _node

_CONTENT_BY_TYPE = {
    "condition": {"applies_when": "t", "label": ""},
    "equation": {"symbolic": "x = 1", "label": "", "variables": []},
    "procedure_step": {"action": "t", "purpose": ""},
    "definition": {"concept": "t", "meaning": "t"},
}


def _flag(node_id, node_type, cos):
    n = _node(node_id, node_type, _CONTENT_BY_TYPE[node_type])
    c = Candidate(
        canonical_key="k",
        canon_key=1,
        node_type=node_type,
        is_misconception=False,
        symbolic=None,
        aliases=(),
        display_name="d",
        opposes_key=None,
        exact_aliases=(),
    )
    return FlaggedNode(node=n, candidate=c, cosine=cos)


def test_rubric_weight_prefers_graded_axes():
    assert rubric_weight_for("procedure_step") > rubric_weight_for("condition") > 0.0
    assert rubric_weight_for("equation") == 0.0  # ungraded axis -> falls back to cosine


def test_caps_at_three_and_orders_by_weight_then_cosine():
    flags = [
        _flag("a", "definition", 0.99),  # weight 0.0
        _flag("b", "procedure_step", 0.51),  # highest weight
        _flag("c", "condition", 0.80),
        _flag("d", "condition", 0.90),
    ]
    out = select_probes(flags)
    assert len(out) == MAX_PROBES_PER_TURN
    assert [f.node.node_id for f in out] == ["b", "d", "c"]  # proc > cond(0.90) > cond(0.80)


def test_one_per_idea_dedupe_by_node_id():
    flags = [_flag("a", "condition", 0.9), _flag("a", "condition", 0.95)]
    out = select_probes(flags)
    assert [f.node.node_id for f in out] == ["a"]
    assert out[0].cosine == 0.95  # keeps the stronger duplicate


def test_dedupe_keeps_first_when_first_is_stronger():
    flags = [_flag("a", "condition", 0.95), _flag("a", "condition", 0.90)]
    out = select_probes(flags)
    assert [f.node.node_id for f in out] == ["a"]
    assert out[0].cosine == 0.95  # keeps the stronger duplicate even when it comes first


def test_limit_override_is_respected():
    flags = [
        _flag("a", "procedure_step", 0.9),
        _flag("b", "condition", 0.8),
        _flag("c", "condition", 0.7),
    ]
    out = select_probes(flags, limit=2)
    assert len(out) == 2
    assert [f.node.node_id for f in out] == ["a", "b"]


def test_empty_input_returns_empty():
    assert select_probes([]) == []
