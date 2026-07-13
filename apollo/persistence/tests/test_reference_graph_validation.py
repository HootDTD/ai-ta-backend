"""Fast tests for the §6.1 reference-graph validation contract
(``apollo.persistence.learner_model_seed.validate_reference_graph``).

This is the executable form of "every reference node MUST carry an entity link
AND paths MUST be declared or grading is blocked" (spec §6.1). WU-4A imports
``validate_reference_graph`` and runs it at pipeline step 3; WU-3B's seed test
asserts every seeded bernoulli problem returns ``ok=True``. No DB, no LLM.
"""

from __future__ import annotations

import pytest

from apollo.persistence.learner_model_seed import (
    NormalizedPath,
    normalize_declared_paths,
    validate_reference_graph,
)


def _problem(steps, declared_paths) -> dict:
    return {
        "id": "demo",
        "reference_solution": steps,
        "declared_paths": declared_paths,
    }


def _step(node_id: str, entity_key: str | None, depends_on: list[str] | None = None) -> dict:
    step: dict = {
        "step": 1,
        "id": node_id,
        "entry_type": "equation",
        "content": {},
        "depends_on": depends_on or [],
    }
    if entity_key is not None:
        step["entity_key"] = entity_key
    return step


def _fully_annotated() -> dict:
    steps = [_step("n1", "eq.a"), _step("n2", "cond.b"), _step("n3", "proc.c")]
    return _problem(steps, [["n1", "n2", "n3"]])


def test_validate_passes_on_fully_annotated_problem():
    result = validate_reference_graph(_fully_annotated())
    assert result.ok is True
    assert result.missing_entity_links == ()
    assert result.undeclared_paths is False
    assert result.errors == ()


def test_validate_fails_missing_entity_link():
    problem = _fully_annotated()
    # Drop one step's entity_key.
    del problem["reference_solution"][1]["entity_key"]
    result = validate_reference_graph(problem)
    assert result.ok is False
    assert "n2" in result.missing_entity_links


def test_validate_fails_empty_entity_link_string():
    problem = _fully_annotated()
    problem["reference_solution"][0]["entity_key"] = ""
    result = validate_reference_graph(problem)
    assert result.ok is False
    assert "n1" in result.missing_entity_links


def test_validate_fails_empty_declared_paths():
    problem = _fully_annotated()
    problem["declared_paths"] = []
    result = validate_reference_graph(problem)
    assert result.ok is False
    assert result.undeclared_paths is True


def test_validate_fails_absent_declared_paths():
    problem = _fully_annotated()
    del problem["declared_paths"]
    result = validate_reference_graph(problem)
    assert result.ok is False
    assert result.undeclared_paths is True


def test_validate_fails_path_references_unknown_node():
    problem = _fully_annotated()
    problem["declared_paths"] = [["n1", "n2", "n3", "ghost"]]
    result = validate_reference_graph(problem)
    assert result.ok is False
    assert any("ghost" in e for e in result.errors)


def test_validate_fails_node_not_on_any_path():
    problem = _fully_annotated()
    # n3 is a real reference node but absent from every declared path.
    problem["declared_paths"] = [["n1", "n2"]]
    result = validate_reference_graph(problem)
    assert result.ok is False
    assert any("n3" in e for e in result.errors)


def test_validate_collects_multiple_failures():
    """A problem with both a missing link and undeclared paths reports both."""
    steps = [_step("n1", None), _step("n2", "cond.b")]
    problem = _problem(steps, [])
    result = validate_reference_graph(problem)
    assert result.ok is False
    assert "n1" in result.missing_entity_links
    assert result.undeclared_paths is True


def test_normalize_declared_paths_supports_legacy_object_and_mixed():
    assert normalize_declared_paths(
        [
            ["n1", "n2"],
            {"strategy_id": "alternate", "nodes": ["n1", "n3"], "milestones": ["n3"]},
        ]
    ) == (
        NormalizedPath("path_0", ("n1", "n2"), ()),
        NormalizedPath("alternate", ("n1", "n3"), ("n3",), is_object=True),
    )


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("not-a-list", "must be a list"),
        ([42], "legacy node list or a path object"),
        ([["n1", 2]], "legacy nodes"),
        ([{"strategy_id": 2, "nodes": ["n1"], "milestones": []}], "strategy_id"),
        ([{"strategy_id": "x", "nodes": "n1", "milestones": []}], ".nodes"),
        ([{"strategy_id": "x", "nodes": ["n1"], "milestones": "n1"}], "milestones"),
        ([{"strategy_id": "x", "nodes": ["n1"], "milestones": [], "extra": 1}], "extra"),
    ],
)
def test_normalize_declared_paths_rejects_bad_shapes(raw, message):
    with pytest.raises(ValueError, match=message):
        normalize_declared_paths(raw)


def test_validate_rejects_duplicate_node_sets():
    problem = _fully_annotated()
    problem["declared_paths"] = [
        {"strategy_id": "one", "nodes": ["n1", "n2", "n3"], "milestones": ["n3"]},
        {"strategy_id": "two", "nodes": ["n3", "n2", "n1"], "milestones": ["n3"]},
    ]
    result = validate_reference_graph(problem)
    assert not result.ok
    assert any("identical node sets" in error for error in result.errors)


def test_validate_rejects_strict_subset_and_bad_milestone():
    problem = _fully_annotated()
    problem["declared_paths"] = [
        {"strategy_id": "full", "nodes": ["n1", "n2"], "milestones": ["ghost"]},
        {"strategy_id": "alternate", "nodes": ["n1", "n2", "n3"], "milestones": ["n3"]},
    ]
    result = validate_reference_graph(problem)
    assert not result.ok
    assert any("strict subset" in error for error in result.errors)
    assert any("milestones are not on the path" in error for error in result.errors)


def test_validate_rejects_duplicate_or_empty_strategy_ids():
    problem = _fully_annotated()
    problem["declared_paths"] = [
        {"strategy_id": "", "nodes": ["n1", "n2"], "milestones": []},
        {"strategy_id": "", "nodes": ["n1", "n3"], "milestones": []},
    ]
    result = validate_reference_graph(problem)
    assert not result.ok
    assert any("non-empty" in error for error in result.errors)
    assert any("duplicate" in error for error in result.errors)


def test_validate_rejects_empty_path_and_reverse_subset_order():
    problem = _fully_annotated()
    problem["declared_paths"] = [
        {"strategy_id": "wide", "nodes": ["n1", "n2", "n3"], "milestones": []},
        {"strategy_id": "empty", "nodes": [], "milestones": []},
    ]
    result = validate_reference_graph(problem)
    assert not result.ok
    assert any("nodes must be non-empty" in error for error in result.errors)
    assert any("path index 1 is a strict subset" in error for error in result.errors)


def test_validate_accepts_jointly_covering_object_paths_with_sink_milestones():
    problem = _problem(
        [
            _step("n1", "eq.n1"),
            _step("n2", "eq.n2"),
            _step("n3", "eq.n3", ["n1", "n2"]),
        ],
        [
            {"strategy_id": "left", "nodes": ["n1", "n3"], "milestones": ["n3"]},
            {"strategy_id": "right", "nodes": ["n2", "n3"], "milestones": ["n3"]},
        ],
    )
    assert validate_reference_graph(problem).ok


def test_validate_rejects_object_path_with_empty_milestones():
    problem = _fully_annotated()
    problem["declared_paths"] = [
        {"strategy_id": "empty", "nodes": ["n1", "n2", "n3"], "milestones": []}
    ]
    result = validate_reference_graph(problem)
    assert not result.ok
    assert any("milestones must be non-empty" in error for error in result.errors)


def test_validate_rejects_object_path_without_sink_milestone():
    problem = _problem(
        [
            _step("n1", "eq.n1"),
            _step("n2", "eq.n2", ["n1"]),
            _step("n3", "eq.n3", ["n2"]),
        ],
        [
            {
                "strategy_id": "no_final_result",
                "nodes": ["n1", "n2", "n3"],
                "milestones": ["n2"],
            }
        ],
    )
    result = validate_reference_graph(problem)
    assert not result.ok
    assert any("must include a reference graph sink" in error for error in result.errors)


def test_validate_leaves_legacy_list_paths_exempt_from_milestone_floor():
    problem = _problem(
        [_step("n1", "eq.n1"), _step("n2", "eq.n2", ["n1"])],
        [["n1", "n2"]],
    )
    assert validate_reference_graph(problem).ok
