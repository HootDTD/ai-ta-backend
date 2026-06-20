"""Fast tests for the §6.1 reference-graph validation contract
(``apollo.persistence.learner_model_seed.validate_reference_graph``).

This is the executable form of "every reference node MUST carry an entity link
AND paths MUST be declared or grading is blocked" (spec §6.1). WU-4A imports
``validate_reference_graph`` and runs it at pipeline step 3; WU-3B's seed test
asserts every seeded bernoulli problem returns ``ok=True``. No DB, no LLM.
"""

from __future__ import annotations

from apollo.persistence.learner_model_seed import validate_reference_graph


def _problem(steps, declared_paths) -> dict:
    return {
        "id": "demo",
        "reference_solution": steps,
        "declared_paths": declared_paths,
    }


def _step(node_id: str, entity_key: str | None) -> dict:
    step: dict = {"step": 1, "id": node_id, "entry_type": "equation", "content": {}}
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
